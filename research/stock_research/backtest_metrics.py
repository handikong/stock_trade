"""由模拟账本生成统一绩效和逐股归因，不修改成交或重新运行策略。"""
from decimal import Decimal, localcontext

from .backtest import dec, ZERO, ONE

ALGORITHM = "daily-equity-performance-v1"


def enrich_report(report, risk_free_annual=None, annualization_sessions=None):
    cfg = report["configuration"]
    saved = report.get("analytics", {}).get("methods", {})
    rf = dec(risk_free_annual if risk_free_annual is not None else saved.get("risk_free_annual", cfg.get("risk_free_annual", "0")))
    annual_days = annualization_sessions if annualization_sessions is not None else saved.get("annualization_sessions", cfg.get("annualization_sessions", 252))
    if not -ONE < rf <= ONE or type(annual_days) is not int or not 1 <= annual_days <= 366:
        raise ValueError("无风险年利率须大于-100%且不高于100%，年化交易日须为1～366的整数")
    with localcontext() as context:
        context.prec = 36
        analytics = _calculate(report, rf, annual_days)
    return {**report, "analytics": analytics}


def _calculate(report, rf, annual_days):
    initial = dec(report["summary"]["initial_cash"])
    curve = report["equity_curve"]
    if initial <= ZERO or not curve:
        raise ValueError("绩效计算需要正初始资金和完整日终净值")
    previous = initial; peak = initial; peak_date = None; worst = ZERO
    peak_at_worst = None; trough_at_worst = None; worst_index = None; worst_peak = initial
    returns = []; exposure = []; largest_amount = ZERO; previous_date = None
    daily_rf = (ONE + rf) ** (ONE / dec(annual_days)) - ONE
    series = []
    for index, row in enumerate(curve):
        if previous_date is not None and row["date"] <= previous_date:
            raise ValueError("绩效净值日期必须严格递增，不能重复")
        previous_date = row["date"]
        equity = dec(row["equity"])
        if equity <= ZERO:
            raise ValueError("净值非正时当前年化/夏普算法不适用，拒绝伪造绩效指标")
        daily_return = equity / previous - ONE
        returns.append(daily_return)
        series.append({"date": row["date"], "return": daily_return, "excess_return": daily_return - daily_rf})
        exposure.append(dec(row["market_value"]) / equity)
        if equity > peak:
            peak = equity; peak_date = row["date"]
        drawdown = ONE - equity / peak
        largest_amount = max(largest_amount, peak - equity)
        if drawdown > worst:
            worst = drawdown; peak_at_worst = peak_date; trough_at_worst = row["date"]
            worst_index = index; worst_peak = peak
        previous = equity
    count = len(returns)
    mean = sum(returns, ZERO) / dec(count)
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / dec(count - 1) if count > 1 else None
    sigma = variance.sqrt() if variance is not None else None
    sharpe = (mean - daily_rf) / sigma * dec(annual_days).sqrt() if sigma is not None and sigma > ZERO else None
    downside = (sum((min(ZERO, r - daily_rf) ** 2 for r in returns), ZERO) / dec(count)).sqrt()
    sortino = (mean - daily_rf) / downside * dec(annual_days).sqrt() if downside > ZERO and count > 1 else None
    annual_return = (previous / initial) ** (dec(annual_days) / dec(count)) - ONE
    recovery = next((row["date"] for row in curve[worst_index + 1:]
                     if dec(row["equity"]) >= worst_peak), None) if worst_index is not None else None
    trades = report["trades"]
    wins = [dec(t["net_pnl"]) for t in trades if dec(t["net_pnl"]) > ZERO]
    losses = [dec(t["net_pnl"]) for t in trades if dec(t["net_pnl"]) < ZERO]
    average_win = sum(wins, ZERO) / dec(len(wins)) if wins else None
    average_loss = sum(losses, ZERO) / dec(len(losses)) if losses else None
    winning_streak = losing_streak = max_wins = max_losses = 0
    for trade in sorted(trades, key=lambda t: (t["sell_date"], t["symbol"], t["buy_date"])):
        pnl = dec(trade["net_pnl"])
        winning_streak = winning_streak + 1 if pnl > ZERO else 0
        losing_streak = losing_streak + 1 if pnl < ZERO else 0
        max_wins = max(max_wins, winning_streak); max_losses = max(max_losses, losing_streak)
    stocks = []; details = []
    for symbol in report["configuration"]["symbols"]:
        closed = [t for t in trades if t["symbol"] == symbol]
        opened = [p for p in report["open_positions"] if p["symbol"] == symbol]
        orders = [o for o in report["orders"] if o["symbol"] == symbol]
        realized = sum((dec(t["net_pnl"]) for t in closed), ZERO)
        floating = sum((dec(p["quantity"]) * dec(p["last_close"]) - dec(p["cost"]) + dec(p["dividends"])
                        for p in opened), ZERO)
        dividends = sum((dec(t["dividend_receivable"]) for t in closed), ZERO) + sum((dec(p["dividends"]) for p in opened), ZERO)
        stocks.append({"symbol": symbol, "closed_count": len(closed), "open_quantity": sum((dec(p["quantity"]) for p in opened), ZERO),
            "closed_net_pnl": realized, "open_net_pnl": floating, "total_net_pnl": realized + floating,
            "dividend_receivable": dividends, "fees": sum((dec(o["fee"]) for o in orders), ZERO),
            "win_rate": dec(sum(dec(t["net_pnl"]) > ZERO for t in closed)) / dec(len(closed)) if closed else None})
        for trade in closed:
            details.append({**trade, "status": "CLOSED", "net_pnl": dec(trade["net_pnl"])})
        for p in opened:
            pnl = dec(p["quantity"]) * dec(p["last_close"]) - dec(p["cost"]) + dec(p["dividends"])
            details.append({"symbol": symbol, "strategy_id": p["strategy_id"], "status": "OPEN",
                "buy_signal_date": p["signal_date"], "buy_date": p["buy_date"], "buy_price": p["buy_price"],
                "quantity": p["quantity"], "sell_date": None, "sell_price": None, "sell_signal_date": None,
                "fees": p["buy_fee"], "net_pnl": pnl, "return_on_cost": pnl / dec(p["cost"]),
                "last_close": p["last_close"], "exit_reason": "NOT_CLOSED"})
    attributed = sum((s["total_net_pnl"] for s in stocks), ZERO)
    if abs(attributed - dec(report["summary"]["net_profit"])) > Decimal("0.00000001"):
        raise ValueError("逐股盈亏汇总与账户净收益不一致，拒绝生成报表")
    return {"algorithm": ALGORITHM,
        "methods": {"risk_free_annual": rf, "risk_free_daily": daily_rf,
            "annualization_sessions": annual_days, "standard_deviation": "sample_n_minus_1",
            "return_basis": "日终总净值，首日相对初始资金，含现金日和应收分红",
            "annual_return": "(期末净值/初始资金)^(年化交易日/样本交易日)-1，短样本年化估算",
            "sharpe": "(日收益均值-日无风险收益)/日收益样本标准差*sqrt(年化交易日)",
            "sortino": "日超额收益均值/全样本下行半偏差*sqrt(年化交易日)",
            "source": "https://web.stanford.edu/~wfsharpe/art/sr/SR.htm"},
        "performance": {"total_return": previous / initial - ONE, "annualized_return_estimate": annual_return,
            "annualized_volatility": sigma * dec(annual_days).sqrt() if sigma is not None else None,
            "sharpe_ratio": sharpe, "sortino_ratio": sortino,
            "calmar_ratio": annual_return / worst if worst > ZERO else None,
            "max_drawdown": worst, "max_drawdown_amount": largest_amount,
            "drawdown_peak_date": peak_at_worst, "drawdown_trough_date": trough_at_worst,
            "drawdown_recovery_date": recovery, "average_exposure": sum(exposure, ZERO) / dec(count),
            "maximum_exposure": max(exposure), "average_win": average_win, "average_loss": average_loss,
            "average_win_loss_ratio": average_win / abs(average_loss) if average_win is not None and average_loss is not None else None,
            "profit_factor": sum(wins, ZERO) / abs(sum(losses, ZERO)) if losses else None,
            "max_winning_streak": max_wins, "max_losing_streak": max_losses,
            "sample_sessions": count, "sharpe_unavailable_reason": None if sharpe is not None else "样本不足2日或日收益标准差为零"},
        "by_symbol": stocks, "trade_details": sorted(details, key=lambda t: (t["buy_date"], t["symbol"])),
        "daily_returns": series}

"""现金多头研究回测：独立模拟账本、Decimal仓位和逐日执行，不写真实业务库。"""
import json
import logging
from datetime import date
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from .config import ROOT
from .store import Session, Security, digest, latest, stamp
from .watchlist import normalize_symbol

logger = logging.getLogger(__name__)
ALGORITHM = "mg7-shared-cash-daily-replay-v1"
POLICY_FILE = ROOT / "config" / "backtest-policy.json"
ZERO = Decimal("0")
ONE = Decimal("1")


def dec(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("金额、数量和价格必须为有限十进制数")
    return number


class BacktestPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    version: str
    purpose: str
    initial_cash: Decimal = Field(gt=0)
    currency: str
    symbols: list[str] = Field(min_length=1, max_length=100)
    history_start: str
    start: str
    end: str | None = None
    max_positions: int = Field(ge=1, le=100)
    max_security_fraction: Decimal = Field(gt=0, le=1)
    minimum_cash_fraction: Decimal = Field(ge=0, lt=1)
    per_trade_risk_fraction: Decimal = Field(gt=0, le=1)
    total_planned_risk_fraction: Decimal = Field(gt=0, le=1)
    principal_loss_fraction: Decimal = Field(ge=0, lt=1)
    gap_reserve_fraction: Decimal = Field(ge=0, lt=1)
    fixed_fee: Decimal = Field(ge=0)
    fee_fraction: Decimal = Field(ge=0, lt=1)
    slippage_fraction: Decimal = Field(ge=0, lt=1)
    sale_settlement_sessions: int = Field(ge=1, le=10)
    stopout_cooldown_sessions: int = Field(ge=0, le=120)
    strategy_priority: list[str]
    risk_free_annual: Decimal = Field(default=ZERO, gt=-1, le=1)
    annualization_sessions: int = Field(default=252, ge=1, le=366)

    @field_validator("initial_cash", "max_security_fraction", "minimum_cash_fraction",
                     "per_trade_risk_fraction", "total_planned_risk_fraction", "principal_loss_fraction",
                     "gap_reserve_fraction", "fixed_fee", "fee_fraction", "slippage_fraction", "risk_free_annual", mode="before")
    @classmethod
    def decimal_values(cls, value):
        if isinstance(value, bool):
            raise ValueError("金额与比例不能使用布尔值")
        return dec(value)

    @model_validator(mode="after")
    def valid(self):
        if self.currency != "USD":
            raise ValueError("当前研究回测只支持USD同币种账户，其他币种须先接入有时点的汇率")
        self.symbols = [normalize_symbol(s) for s in self.symbols]
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("回测股票不能重复")
        if set(self.strategy_priority) != {"EMA_PULLBACK", "MACD_ICON1"} or len(self.strategy_priority) != 2:
            raise ValueError("strategy_priority须恰好包含EMA_PULLBACK和MACD_ICON1各一次")
        if self.per_trade_risk_fraction > self.total_planned_risk_fraction:
            raise ValueError("单笔风险上限不能超过组合计划风险上限")
        if self.gap_reserve_fraction > self.principal_loss_fraction:
            raise ValueError("缺口保留预算不能大于本金亏损预算")
        for value in (self.history_start, self.start, self.end):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("日期必须为YYYY-MM-DD")
        if self.history_start > self.start or (self.end and self.end < self.start):
            raise ValueError("需要满足预热起点≤回测起点≤终点")
        return self


def policy(path=None, overrides=None):
    chosen = Path(path or POLICY_FILE).expanduser()
    try:
        values = json.loads(chosen.read_text(encoding="utf-8"))
        values.update({k: v for k, v in (overrides or {}).items() if v is not None})
        return BacktestPolicy.model_validate(values)
    except (OSError, ValueError) as exc:
        raise ValueError(f"回测配置无效：{chosen.name}；{exc}") from exc


def _inputs(cfg, indicator_file=None, strategy_file=None):
    from . import band_strategies, futu_macd
    from .market import calendar_context
    from .trend import _bars, _calendar

    params = futu_macd.policy(indicator_file)
    params = {**params, "policy": {**params["policy"], "trend": None}}
    strategy_cfg = band_strategies.policy(strategy_file)
    if not strategy_cfg.exits.enabled:
        raise ValueError("收益回测需要启用退出规则，不把未启用退出当作永久持有策略")
    if strategy_cfg.breakout.enabled or strategy_cfg.rebound.enabled:
        raise ValueError("本轮回测仅支持EMA回踩和MACD图标1，其他策略须关闭")
    sources = {}; metadata = {}
    with Session() as session:
        for symbol in cfg.symbols:
            security = session.scalar(select(Security).where(Security.symbol == symbol))
            item = latest(session, security.id, "market") if security else None
            if item is None:
                raise ValueError(f"{symbol}缺少行情缓存；本命令不联网补齐，也不静默删出股票池")
            currency = item.payload.get("profile", {}).get("currency")
            if currency != cfg.currency:
                raise ValueError(f"{symbol}行情币种缺失或与账户不一致，不能猜测换算")
            bars = _bars(item.payload)
            if not bars:
                raise ValueError(f"{symbol}没有完整日线")
            sources[symbol] = bars
            metadata[symbol] = {"snapshot_id": item.id, "version": item.fingerprint,
                "fetched_at": item.payload.get("fetched_at"), "currency": currency,
                "available_start": bars[0]["date"], "available_end": bars[-1]["date"],
                "adjustment": item.payload.get("adjustment")}
    _, expected = calendar_context()
    common_end = min(expected, *(bars[-1]["date"] for bars in sources.values()))
    until = cfg.end or common_end
    cal = _calendar(date.fromisoformat(cfg.history_start), date.fromisoformat(until))
    until = cal.date_to_session(until, direction="previous").date().isoformat()
    if until > common_end or until < cfg.start:
        raise ValueError(f"回测终点超出共同完整窗口；MG7共同截止为{common_end}")
    sessions = [s.date().isoformat() for s in cal.sessions_in_range(cfg.start, until)]
    expected_dates = {s.date().isoformat() for s in cal.sessions_in_range(cfg.history_start, until)}
    data = {}; warnings = []
    if cfg.end is None and until < expected:
        warnings.append(f"缓存尚未覆盖最近已收盘日{expected}，本次使用共同截止{until}，未联网更新")
    for symbol, all_bars in sources.items():
        bars = [b for b in all_bars if cfg.history_start <= b["date"] <= until]
        if {b["date"] for b in bars} != expected_dates:
            raise ValueError(f"{symbol}预热或回测窗口存在缺交易日，拒绝使用错位价格继续回测")
        if any(dec(b.get("split", "0") or "0") != 0 for b in bars if b["date"] >= cfg.start):
            raise ValueError(f"{symbol}窗口存在拆合股，当前执行模型未支持，不能重复调整股数")
        for bar in bars:
            o, h, low, c = [dec(bar[k]) for k in ("open", "high", "low", "close")]
            if not ZERO < low <= min(o, c) <= max(o, c) <= h:
                raise ValueError(f"{symbol}原始OHLC无效，不能模拟执行")
            if dec(bar.get("dividend", "0") or "0") < ZERO:
                raise ValueError(f"{symbol}分红字段无效")
        rows = futu_macd.calculate(bars, params)
        evaluated = band_strategies.calculate(bars, rows, params, strategy_cfg)
        data[symbol] = {"bars": {b["date"]: b for b in bars},
                        "days": {d["as_of"]: d for d in evaluated}}
        logger.info("%s：缓存%s至%s，预热/计算%s根日线", symbol, bars[0]["date"], until, len(bars))
    return data, sessions, metadata, params, strategy_cfg, warnings


class PortfolioReplay:
    """共享一个模拟账户；真实账户、股票池信号历史和数据库均不写入。"""
    def __init__(self, cfg, data, sessions):
        self.cfg = cfg; self.data = data; self.sessions = sessions
        self.cash = cfg.initial_cash; self.dividends = ZERO; self.receivables = []
        self.positions = {}; self.pending_buys = []; self.pending_sells = {}
        self.orders = []; self.trades = []; self.skipped = []; self.curve = []; self.cooldowns = {}
        self.peak = cfg.initial_cash; self.max_drawdown = ZERO; self.total_fees = ZERO

    def fee(self, gross):
        return self.cfg.fixed_fee + gross * self.cfg.fee_fraction

    def equity(self, day, price_field):
        return (self.cash + sum((r["amount"] for r in self.receivables), ZERO) + self.dividends +
                sum((p["quantity"] * dec(self.data[s]["bars"][day][price_field])
                     for s, p in self.positions.items()), ZERO))

    def planned_risk(self, day, field):
        return sum((p["quantity"] * max(ZERO, dec(self.data[s]["bars"][day][field]) - p["stop"] *
            (ONE - self.cfg.slippage_fraction)) + self.fee(p["quantity"] * p["stop"])
            for s, p in self.positions.items()), ZERO)

    def skip(self, day, item, code, message):
        self.skipped.append({"date": day, "symbol": item["symbol"], "strategy_id": item["strategy_id"],
                             "signal_date": item["signal_date"], "reason": code, "message": message})

    def sell(self, symbol, day, index, reference, reason, signal_date):
        p = self.positions.pop(symbol)
        price = reference * (ONE - self.cfg.slippage_fraction)
        gross = price * p["quantity"]; fee = self.fee(gross)
        net = gross - fee
        if net < ZERO:
            raise ValueError("卖出费用超过收入，研究费用配置不适用于该交易")
        self.receivables.append({"amount": net, "available_index": index + self.cfg.sale_settlement_sessions})
        self.total_fees += fee
        order = {"side": "SELL", "date": day, "symbol": symbol, "strategy_id": p["strategy_id"],
            "signal_date": signal_date, "entry_signal_date": p["signal_date"], "price": price,
            "reference_price": reference, "quantity": p["quantity"], "fee": fee, "cash_change_pending": net,
            "reason": reason, "simulated": True}
        self.orders.append(order)
        pnl = net - p["cost"]
        self.trades.append({"symbol": symbol, "strategy_id": p["strategy_id"],
            "buy_signal_date": p["signal_date"], "buy_date": p["buy_date"], "buy_price": p["buy_price"],
            "quantity": p["quantity"], "initial_protection_price": p["stop"],
            "sell_signal_date": signal_date, "sell_date": day, "sell_price": price,
            "exit_reason": reason, "fees": p["buy_fee"] + fee, "cash_trading_pnl": pnl,
            "dividend_receivable": p["dividends"], "net_pnl": pnl + p["dividends"],
            "return_on_cost": (pnl + p["dividends"]) / p["cost"], "simulated": True})
        if reason in ("PROTECTION_GAP", "PROTECTION_INTRADAY"):
            self.cooldowns[symbol] = index + self.cfg.stopout_cooldown_sessions
        logger.info("模拟卖出 %s %s：%s股，%s，原因%s", day, symbol, p["quantity"], price, reason)

    def buy(self, day, index, item):
        cfg = self.cfg; symbol = item["symbol"]
        if symbol in self.positions:
            return self.skip(day, item, "ALREADY_HELD", "同股已有模拟持仓，不重复建仓或自动加仓")
        if index <= self.cooldowns.get(symbol, -1):
            return self.skip(day, item, "COOLDOWN", "止损后处于配置的交易日冷静期")
        if len(self.positions) >= cfg.max_positions:
            return self.skip(day, item, "POSITION_LIMIT", "模拟账户持股数量已达上限")
        opening = dec(self.data[symbol]["bars"][day]["open"])
        stop = item["stop"]
        if stop is None or not ZERO < stop < opening:
            return self.skip(day, item, "INVALID_PROTECTION", "缺少有效保护线，或开盘已经触及/跌破保护线")
        price = opening * (ONE + cfg.slippage_fraction)
        equity = self.equity(day, "open")
        floor = cfg.initial_cash * (ONE - cfg.principal_loss_fraction)
        reserve = cfg.initial_cash * cfg.gap_reserve_fraction
        existing = self.planned_risk(day, "open")
        budget = min(cfg.initial_cash * cfg.per_trade_risk_fraction,
                     cfg.initial_cash * cfg.total_planned_risk_fraction - existing,
                     equity - floor - reserve - existing)
        worst_exit = stop * (ONE - cfg.slippage_fraction)
        unit_risk = price - worst_exit + cfg.fee_fraction * (price + worst_exit)
        cash_budget = self.cash - cfg.initial_cash * cfg.minimum_cash_fraction
        risk_count = (budget - 2 * cfg.fixed_fee) / unit_risk
        cash_count = (cash_budget - cfg.fixed_fee) / (price * (ONE + cfg.fee_fraction))
        cap_count = (cfg.initial_cash * cfg.max_security_fraction - cfg.fixed_fee) / (price * (ONE + cfg.fee_fraction))
        quantity = min(risk_count, cash_count, cap_count).to_integral_value(rounding=ROUND_FLOOR)
        if quantity <= ZERO:
            reason = "RISK_BUDGET" if risk_count <= min(cash_count, cap_count) else "CASH_OR_SIZE"
            return self.skip(day, item, reason, "风险、可用现金或单股金额预算不足以买入一整股")
        gross = price * quantity; fee = self.fee(gross); cost = gross + fee
        self.cash -= cost; self.total_fees += fee
        self.positions[symbol] = {"strategy_id": item["strategy_id"], "signal_date": item["signal_date"],
            "quantity": quantity, "stop": stop, "buy_date": day, "buy_price": price,
            "cost": cost, "buy_fee": fee, "dividends": ZERO}
        self.orders.append({"side": "BUY", "date": day, "symbol": symbol,
            "strategy_id": item["strategy_id"], "signal_date": item["signal_date"], "quantity": quantity,
            "price": price, "reference_price": opening, "fee": fee, "cash_change": -cost,
            "initial_protection_price": stop, "planned_risk": quantity * unit_risk + 2 * cfg.fixed_fee,
            "simulated": True})
        logger.info("模拟买入 %s %s：%s股，%s；冻结保护线%s", day, symbol, quantity, price, stop)

    def run(self):
        for index, day in enumerate(self.sessions):
            for receivable in list(self.receivables):
                if receivable["available_index"] <= index:
                    self.cash += receivable["amount"]; self.receivables.remove(receivable)
            # 分红按隔夜持股计应收；缺支付日，不伪造可用现金，不重复计调整价收益。
            for symbol, p in self.positions.items():
                amount = p["quantity"] * dec(self.data[symbol]["bars"][day].get("dividend", "0") or "0")
                self.dividends += amount; p["dividends"] += amount
            for symbol, reason in self.pending_sells.items():
                if symbol in self.positions:
                    self.sell(symbol, day, index, dec(self.data[symbol]["bars"][day]["open"]), reason[0], reason[1])
            self.pending_sells = {}
            for symbol, p in list(self.positions.items()):
                opening = dec(self.data[symbol]["bars"][day]["open"])
                if opening < p["stop"]:
                    self.sell(symbol, day, index, opening, "PROTECTION_GAP", day)
            for item in self.pending_buys:
                self.buy(day, index, item)
            self.pending_buys = []
            for symbol, p in list(self.positions.items()):
                if dec(self.data[symbol]["bars"][day]["low"]) < p["stop"]:
                    self.sell(symbol, day, index, p["stop"], "PROTECTION_INTRADAY", day)
            candidates = []
            for symbol in self.cfg.symbols:
                daily = self.data[symbol]["days"][day]
                if symbol in self.positions and daily["exit_market_state"]["confirmed"]:
                    self.pending_sells[symbol] = ("COMMON_THREE_STATE_CONFIRMED", day)
                for signal in daily["strategies"]:
                    if not signal["buy_signal"] or signal["strategy_id"] not in self.cfg.strategy_priority:
                        continue
                    plan = signal.get("exit_plan") or {}
                    stop = plan.get("initial_protection_price")
                    bar = self.data[symbol]["bars"][day]
                    # 调整价用于技术判断；以信号日已知比例换回报价尺度后冻结，不能每天用新比例移动止损。
                    factor = dec(bar["adjusted_close"]) / dec(bar["close"])
                    candidates.append({"symbol": symbol, "strategy_id": signal["strategy_id"],
                        "signal_date": day, "stop": dec(stop) / factor if stop is not None else None})
            candidates.sort(key=lambda item: (self.cfg.symbols.index(item["symbol"]),
                                             self.cfg.strategy_priority.index(item["strategy_id"])))
            chosen = set()
            for item in candidates:
                if item["symbol"] in self.positions:
                    self.skip(day, item, "ALREADY_HELD", "收盘产生信号时已经持仓，不自动加仓或预排卖后重买")
                elif item["symbol"] in chosen:
                    self.skip(day, item, "DUPLICATE_SYMBOL_SIGNAL", "同股同日多策略按预定优先级只保留一个入场请求")
                else:
                    self.pending_buys.append(item); chosen.add(item["symbol"])
            equity = self.equity(day, "close")
            self.peak = max(self.peak, equity)
            drawdown = ONE - equity / self.peak
            self.max_drawdown = max(self.max_drawdown, drawdown)
            self.curve.append({"date": day, "cash_available": self.cash,
                "cash_unsettled": sum((r["amount"] for r in self.receivables), ZERO),
                "dividend_receivable": self.dividends,
                "market_value": sum((p["quantity"] * dec(self.data[s]["bars"][day]["close"])
                                     for s, p in self.positions.items()), ZERO),
                "equity": equity, "drawdown": drawdown, "position_count": len(self.positions),
                "principal_loss": max(ZERO, self.cfg.initial_cash - equity),
                "principal_floor_breached": equity <= self.cfg.initial_cash * (ONE - self.cfg.principal_loss_fraction)})
        return self.report()

    def report(self):
        final = self.curve[-1]
        trading_pnl = sum((t["cash_trading_pnl"] for t in self.trades), ZERO)
        open_pnl = sum((p["quantity"] * dec(self.data[s]["bars"][self.sessions[-1]]["close"]) - p["cost"]
                        for s, p in self.positions.items()), ZERO)
        # 使用明确会计恒等式做运行时一致性校验，不用独立断言脚本/测试替代。
        if abs(final["equity"] - self.cfg.initial_cash - trading_pnl - open_pnl - self.dividends) > Decimal("0.00000001"):
            raise ValueError("模拟账本净值不平，拒绝输出收益结论")
        winners = [t for t in self.trades if t["net_pnl"] > ZERO]
        losers = [t for t in self.trades if t["net_pnl"] < ZERO]
        gross_profit = sum((t["net_pnl"] for t in winners), ZERO)
        gross_loss = -sum((t["net_pnl"] for t in losers), ZERO)
        return {"summary": {"initial_cash": self.cfg.initial_cash, "final_equity": final["equity"],
            "net_profit": final["equity"] - self.cfg.initial_cash,
            "total_return": final["equity"] / self.cfg.initial_cash - ONE,
            "max_drawdown": self.max_drawdown, "closed_trade_count": len(self.trades),
            "win_rate": dec(len(winners)) / dec(len(self.trades)) if self.trades else None,
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "total_fees": self.total_fees, "closed_cash_trading_pnl": trading_pnl,
            "open_mark_to_market_pnl": open_pnl, "dividend_receivable": self.dividends,
            "cash_available": self.cash, "cash_unsettled": final["cash_unsettled"],
            "open_position_count": len(self.positions), "skipped_count": len(self.skipped),
            "principal_breach_days": sum(d["principal_floor_breached"] for d in self.curve)},
            "orders": self.orders, "trades": self.trades, "skipped": self.skipped, "equity_curve": self.curve,
            "open_positions": [{"symbol": s, **p, "last_close": dec(self.data[s]["bars"][self.sessions[-1]]["close"])}
                               for s, p in self.positions.items()],
            "pending_entries": self.pending_buys,
            "pending_exits": [{"symbol": s, "reason": r[0], "signal_date": r[1]} for s, r in self.pending_sells.items()]}


def run(cfg, indicator_file=None, strategy_file=None):
    from . import band_strategies
    data, sessions, sources, params, strategy_cfg, warnings = _inputs(cfg, indicator_file, strategy_file)
    if not sessions:
        raise ValueError("回测窗口没有完整交易日")
    logger.info("MG7共享模拟资金%s %s；实际窗口%s至%s", cfg.initial_cash, cfg.currency, sessions[0], sessions[-1])
    with localcontext() as context:
        context.prec = 36
        result = PortfolioReplay(cfg, data, sessions).run()
    configuration = cfg.model_dump(mode="json")
    report = {"algorithm": ALGORITHM, "mode": "HISTORICAL_SIMULATION", "generated_at": stamp(),
        "currency": cfg.currency, "configuration": configuration, "configuration_version": digest(configuration),
        "strategy_algorithm": band_strategies.ALGORITHM,
        "strategy_configuration": strategy_cfg.model_dump(), "indicator_parameters": params,
        "source_snapshots": sources, "source_version": digest(sources),
        "actual_start": sessions[0], "actual_end": sessions[-1], "session_count": len(sessions),
        "warnings": warnings, "simulated": True, "real_orders_allowed": False,
        "limitations": ["研究模板只模拟USD现金多头整股，不代表实盘仓位或成交",
            "信号收盘确认，下一交易日以开盘为基准加滑点；开盘重新校验并可减量，不保证能在真实开盘集合竞价执行",
            "保护线为已知报价尺度固定价格；日内跌破按止损基准加滑点，跳空按开盘加滑点，非保证成交模型",
            "分红按隔夜持仓计应收，缺支付日不计可用现金；未模拟税收、利息、拆合股或停牌执行",
            "仅按日终净值计算最大回撤，未包括日内回撤；期末不强行平仓，浮盈亏计入净值",
            "MG7为当前固定成分，现有复权档案不是当时完整可得快照，有幸存者与复权口径限制",
            "无财报/证券质量/行业相关性和实际购买力风控，不是完整账户风险许可；5%为研究预算，不保证实际亏损上限",
            "实盘人工决定退出，当前报告只执行明确的模拟规则，不把人工择时收益算入策略"], **result}
    from .backtest_metrics import enrich_report
    return enrich_report(report, cfg.risk_free_annual, cfg.annualization_sessions)

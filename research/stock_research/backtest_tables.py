"""回测终端报表：账户绩效、逐股归因、买卖配对，说明默认折叠到--details。"""
from collections import Counter

from .backtest import dec
from .console_tables import table


def number(value, signed=False):
    return "—" if value is None else format(dec(value), "+,.2f" if signed else ",.2f")


def percent(value):
    return "—" if value is None else f"{dec(value) * 100:.2f}%"


def render(report, details=False):
    s = report["summary"]; a = report["analytics"]; p = a["performance"]
    rows = [
        ["初始资金", number(s["initial_cash"]), "期末净值", number(s["final_equity"])],
        ["净收益", number(s["net_profit"], True), "区间收益率", percent(p["total_return"])],
        ["最大日终回撤", percent(p["max_drawdown"]), "夏普率（年化）", number(p["sharpe_ratio"])],
        ["最大回撤金额", number(p["max_drawdown_amount"]), "统计交易日", str(p["sample_sessions"])],
        ["年化收益估算", percent(p["annualized_return_estimate"]), "年化波动率", percent(p["annualized_volatility"])],
        ["索提诺率", number(p["sortino_ratio"]), "卡玛率", number(p["calmar_ratio"])],
        ["已结束 / 未平仓", f"{s['closed_trade_count']} / {s['open_position_count']}", "胜率", percent(s["win_rate"])],
        ["盈利因子", number(p["profit_factor"]), "平均盈亏比", number(p["average_win_loss_ratio"])],
        ["平均盈利", number(p["average_win"]), "平均亏损", number(p["average_loss"])],
        ["最大连胜 / 连亏", f"{p['max_winning_streak']} / {p['max_losing_streak']}", "累计手续费", number(s["total_fees"])],
        ["平均资金投入", percent(p["average_exposure"]), "最高资金投入", percent(p["maximum_exposure"])],
        ["可用现金", number(s["cash_available"]), "未结算卖出款", number(s["cash_unsettled"])],
        ["应收分红", number(s["dividend_receivable"]), "跳过机会", str(s["skipped_count"])],
    ]
    lines = ["组合回测报表 · 模拟账户", "股票：" + " / ".join(report["configuration"]["symbols"]),
        f"{report['actual_start']} → {report['actual_end']} | {report['session_count']}个交易日 | 金额单位 {report['currency']}",
        "", "账户绩效", table(["指标", "数值", "指标", "数值"], rows, numeric=(1, 3))]
    peak = p["drawdown_peak_date"] or "初始资金"
    if p["drawdown_trough_date"]:
        recovery = p["drawdown_recovery_date"] or "截止日尚未修复"
        lines.append(f"最大回撤区间：高点 {peak} → 低点 {p['drawdown_trough_date']}；修复：{recovery}")
    stocks = [[r["symbol"], str(r["closed_count"]), number(r["closed_net_pnl"], True), number(r["open_net_pnl"], True),
               number(r["total_net_pnl"], True), number(r["fees"]), percent(r["win_rate"])] for r in a["by_symbol"]]
    stocks.append(["合计", str(s["closed_trade_count"]),
        number(sum(dec(r["closed_net_pnl"]) for r in a["by_symbol"]), True),
        number(sum(dec(r["open_net_pnl"]) for r in a["by_symbol"]), True),
        number(s["net_profit"], True), number(s["total_fees"]), percent(s["win_rate"])])
    lines.extend(["", "各股票累计盈亏", table(["股票", "已平笔数", "已平仓净利", "未平仓盈亏", "总净利", "手续费", "胜率"], stocks, numeric=(1, 2, 3, 4, 5, 6))])
    names = {"EMA_PULLBACK": "回踩", "MACD_ICON1": "MACD"}
    trades = []
    for t in a["trade_details"]:
        quantity = format(dec(t["quantity"]), "f").rstrip("0").rstrip(".") if "." in str(t["quantity"]) else str(t["quantity"])
        trades.append([t["symbol"], names.get(t["strategy_id"], t["strategy_id"]), t["buy_date"], number(t["buy_price"]), quantity,
            t["sell_date"] or "未退出", number(t["sell_price"]), number(t["fees"]),
            number(t["net_pnl"], True), percent(t["return_on_cost"]), "已平" if t["status"] == "CLOSED" else "未平"])
    lines.extend(["", "买卖明细（模拟成交日期；未平仓按期末收盘估值）",
        table(["股票", "策略", "买入日", "买入价", "股数", "卖出日", "卖出价", "费用", "净盈亏", "收益率", "状态"], trades, numeric=(3, 4, 6, 7, 8, 9))])
    if not trades:
        lines.append("本窗口没有模拟成交。")
    methods = a["methods"]
    lines.extend(["", f"夏普口径：无风险年利率 {percent(methods['risk_free_annual'])}（假设），{methods['annualization_sessions']}个交易日年化，日收益样本标准差；年化收益是短样本估算。",
        "净盈亏含已扣费用和应收分红；未平仓不预扣未来卖费。各行独立四舍五入，合计按未舍入值计算；— 表示不适用。",
        f"末日待执行：入场{len(report['pending_entries'])}条 / 退出{len(report['pending_exits'])}条；完整说明用 --details。"])
    for warning in report.get("warnings", []):
        lines.append("数据提示：" + warning)
    if details:
        lines.extend(["", "参数与统计口径", f"回测算法：{report['algorithm']}；指标算法：{a['algorithm']}",
            f"配置版本：{report['configuration_version']}", f"净值收益基准：{methods['return_basis']}",
            "平均盈亏比=平均盈利/平均亏损绝对值；盈利因子=已结束盈利合计/亏损绝对值合计。",
            "未设置比较基准，未报告Alpha/Beta或超额收益；回撤只包含日终值。",
            "", "跳过原因"])
        lines.extend(f"{message}：{count}次" for message, count in Counter(t["message"] for t in report["skipped"]).items())
        lines.extend(["", "模型限制", *report["limitations"]])
    if report.get("output_file"):
        lines.append("结果文件：" + report["output_file"])
    return "\n".join(lines)

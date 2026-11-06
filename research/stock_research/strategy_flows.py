"""将同一股票的买点、预警、退出配对展示，不推导成交或收益。"""


def build_flows(days, display_start):
    """先从完整预热数据配对，再保留与展示窗口相交的过程，防止丢失原买点。"""
    if not days:
        return []
    flows = {}
    for day in days:
        for signal in day["strategies"]:
            if not signal["buy_signal"]:
                continue
            key = (signal["strategy_id"], day["as_of"])
            flows[key] = {"strategy_id": key[0], "name": signal["name"],
                "entry_signal_date": key[1], "entry_reference": (signal.get("levels") or {}).get("entry_reference"),
                "entry_signal": {"as_of": day["as_of"], **signal},
                "exit_plan": signal.get("exit_plan"), "alerts": [], "exit_signal": None,
                "technical_only": True, "order_allowed": False}
        for alert in day.get("exit_alerts", []):
            key = (alert["strategy_id"], alert["entry_signal_date"])
            if key not in flows:
                raise ValueError("预警缺少对应买点，不能拼接完整过程")
            flows[key]["alerts"].append(alert)
        for event in day.get("exit_signals", []):
            key = (event["strategy_id"], event["entry_signal_date"])
            if key not in flows or flows[key]["exit_signal"] is not None:
                raise ValueError("退出缺少唯一对应买点，不能拼接完整过程")
            flows[key]["exit_signal"] = event
    active = {(s["strategy_id"], s["entry_signal_date"]): s for s in days[-1].get("exit_tracking", [])}
    result = []
    for key, flow in sorted(flows.items(), key=lambda pair: (pair[0][1], pair[0][0])):
        event = flow["exit_signal"]
        # 展示前已完整结束的过程不列入；跨越起点的过程带回原始买点及预警。
        if event is not None and event["as_of"] < display_start:
            continue
        if flow["entry_signal_date"] < display_start and event is None and key not in active:
            continue
        track = active.get(key)
        flow.update(as_of=days[-1]["as_of"], entry_before_display_start=key[1] < display_start,
                    display_start=display_start, latest_tracking=track,
                    status="EXIT_SIGNAL" if event else "TRACKING" if track else
                           "NOT_TRACKED" if flow["exit_plan"] is None else "UNKNOWN")
        result.append(flow)
    return result


def render_flows(flows, *, symbol):
    from .cli import decimal_text

    lines = []
    for index, flow in enumerate(flows, 1):
        event = flow["exit_signal"]
        ending = event["as_of"] if event else "跟踪中" if flow["status"] == "TRACKING" else "退出跟踪未启用/未支持" if flow["status"] == "NOT_TRACKED" else "退出状态未知"
        lines.extend(["", f"【{symbol} · 过程{index} · {flow['name']}】",
                      f"买入信号 {flow['entry_signal_date']} → {'卖出信号 ' if event else ''}{ending}",
                      f"  {flow['entry_signal_date']} 买入信号，收盘参考 {decimal_text(flow['entry_reference'])}"])
        if flow["entry_before_display_start"]:
            lines.append("    原买点早于展示起点，已补回以展示完整过程。")
        plan = flow.get("exit_plan")
        if plan:
            lines.append(f"    冻结保护线 {decimal_text(plan['initial_protection_price'])}")
            if plan.get("warning"):
                lines.append("    " + plan["warning"])
        for alert in flow["alerts"]:
            prefix = "（展示起点前）" if alert["as_of"] < flow["display_start"] else ""
            lines.append(f"  {alert['as_of']} {alert['status_label']}{prefix}")
        if event:
            lines.append(f"  {event['as_of']} 卖出信号，收盘参考 {decimal_text(event['close_reference'])}；{event['status_label']}")
            if event["reason"] == "INITIAL_PROTECTION_BROKEN":
                evidence = event["evidence"]
                lines.append(f"    日内破位，收盘后识别：开盘{decimal_text(evidence['open'])}，最低{decimal_text(evidence['low'])}；保护线不是成交价。")
        elif flow.get("latest_tracking"):
            lines.append(f"  截至{flow['as_of']} 尚未出现卖出信号：{flow['latest_tracking']['status_label']}")
        else:
            lines.append(f"  截至{flow['as_of']} {ending}，不能据此认为应当继续持有。")
    if not flows:
        lines.append("本窗口没有可展示的买入—退出过程。")
    return lines

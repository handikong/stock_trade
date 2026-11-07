"""当前信号读取每周估值及网页更正、技术结构。"""
from .cli import decimal_text
from .service import build_report, security_for
from .notification_cards import summarize


def prepare(symbol, cached_only=True):
    # 行情由通知扫描步骤处理；研究本身始终只读，cached_only保留调用兼容。
    report = build_report(security_for(symbol))
    valuation = report.get("automatic_valuation_reference") or {}
    warnings = []
    lines = [valuation.get("message","基金不适用公司EPS估值")]
    attempt = valuation.get("attempt") or {}
    if attempt:
        lines.append(f"Stock Analysis 最近采集尝试：{attempt['date']}（北京时间）")
    if valuation.get("stale"):
        lines.append("含一周前取得的估值资料")
    if valuation.get("corrected_fields"):
        lines.append("当前估值包含网页更正")
    for row in valuation.get("scenarios") or []:
        lines.append(f"{row['name']}：EPS {row['eps']} × PE {row['pe']} = {decimal_text(row['price'])} USD")
    if valuation and not valuation["scenarios"]:
        warnings.append({"stage":"valuation","message":valuation["message"]})
    technical = (report.get("market") or {}).get("technical") or {}
    combined = report.get("fundamental_technical") or {}
    if report["cache"].get("state") == "fresh" and not technical.get("stale"):
        zones = [(i,z) for i,z in enumerate(technical.get("zones", [])) if z.get("kind") in ("support", "resistance")]
        comparisons = {z["zone_index"]:z for z in combined.get("zones") or []}
        for index, zone in zones[:3]:
            quoted = zone.get("quote_low") is not None and zone.get("quote_high") is not None
            low = zone.get("quote_low") if quoted else zone.get("low")
            high = zone.get("quote_high") if quoted else zone.get("high")
            lines.append(f"{'支撑' if zone['kind']=='support' else '压力'} {zone.get('strength_label', '强弱待核对')}："
                         f"{decimal_text(low)}～{decimal_text(high)}（{'报价尺度' if quoted else '技术调整价'}）")
            evaluation = comparisons.get(index)
            if evaluation:
                lines.append("基本面配合：" + evaluation["label"] + ("；技术与估值研究共振" if evaluation["confluence"] else "；未标共振"))
                lines.extend(evaluation["reasons"][:2])
    else:
        lines.append("行情或技术结构待更新，当前通知不发布旧支撑压力。")
    for issue in combined.get("issues") or []:
        lines.append("综合判断缺项：" + issue)
    for excluded in combined.get("excluded_models") or []:
        lines.append(f"{excluded['model']}未参与结合：{excluded['message']}")
    current_market = report.get("market") or {}
    return {"lines":lines, "warnings":warnings,"brief":summarize(report),
            "context":{"symbol":report["symbol"],"close":current_market.get("last_price"),
                       "data_as_of":current_market.get("last_complete_session"),
                       "currency":report["profile"].get("currency"),
                       "cache_state":report["cache"].get("state"),"cache_message":report["cache"].get("message")},
            "versions":{"weekly_valuation":valuation.get("id"),"market":current_market.get("id"),
                        "fundamental_technical":combined.get("version")}}

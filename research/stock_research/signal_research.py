"""当前信号读取手工研究数据和技术结构，不抓财报/电话会/预期/简析。"""
from .cli import decimal_text
from .service import build_report, security_for
from .notification_cards import summarize


def prepare(symbol, cached_only=True):
    # 行情由通知扫描步骤处理；研究本身始终只读，cached_only保留调用兼容。
    report = build_report(security_for(symbol))
    manual = report["manual_valuation"]
    lines = ["研究来源：本人确认的手工财务输入；估值由程序计算，技术结构来自日线。",
             "不读取旧财报/电话会议；新财报是否已发布需本人复核，未自动获知。"]
    warnings = []
    if manual["status"] in ("missing", "needs_review"):
        lines.append(manual["message"])
        warnings.append({"stage":"manual_valuation", "message":manual["message"]})
    else:
        p = manual["input"]; target = p.get("target") or {}
        lines.append(f"手工资料日{p['as_of']}；币种{p['currency']}；目标日{target.get('date') or '未填写'}")
        if p.get("schema_version") == "manual-eps-pe-v4":
            e = p["eps"]
            lines[-1] = f"EPS及PE均为手工输入；录入日{p['as_of']}；{p['currency']}；NTM约定{e['period_start']}～{e['period_end']}"
            for group in manual.get("valuation_sets") or []:
                lines.append(group["label"])
                lines.extend(f"{r['name']}：EPS {r['eps']} × PE {r['pe']} = {decimal_text(r['price'])}" for r in group["scenarios"])
            lines.extend((manual.get("reference") or {}).get("warnings") or [])
        if p.get("schema_version") == "manual-eps-v3":
            e = p["eps"]
            lines[-1] = f"EPS录入日{p['as_of']}；币种{p['currency']}；NTM约定期间{e['period_start']}～{e['period_end']}；{e['basis']}"
            for group in manual.get("valuation_sets") or []:
                pe = group["pe"]
                lines.append(group["label"] + (f"，资料月份{pe['data_month']}；{pe['mapping_note']}" if group["kind"]=="industry" else f"，{pe['metric']}，实际{pe['actual_start']}～{pe['actual_end']}，样本{pe['count']}"))
                lines.extend(f"{r['name']}：EPS {r['eps']} × PE {r['pe']} = {decimal_text(r['price'])}" for r in group["scenarios"])
                lines.append("PE来源：" + pe["source_url"])
            lines.extend((manual.get("reference") or {}).get("warnings") or [])
            lines.extend((manual.get("automatic_pe") or {}).get("warnings") or [])
        if p.get("schema_version") == "manual-valuation-v2":
            lines[-1] = f"手工资料日{p['as_of']}；币种{p['currency']}；前瞻估值参考，不是一年后目标价"
            eps = p["eps"]; pe = p["historical_pe"]
            lines.append(f"EPS期间{eps['period_start']}～{eps['period_end']}（{eps['period_type']}，{eps['basis']}）；历史PE{pe['period_start']}～{pe['period_end']}，{pe['eps_reference']}，{pe['statistic']}")
            lines.extend((manual.get("reference") or {}).get("warnings") or [])
        for row in ([] if p.get("schema_version") in ("manual-eps-v3","manual-eps-pe-v4") else manual["scenarios"]):
            lines.append(f"{row['name']}：EPS {row['eps']} × PE {row['pe']} = {decimal_text(row['price'])}")
        dcf = manual.get("dcf_result")
        if dcf:
            lines.append(f"DCF资料日{dcf['as_of']}价值：{decimal_text(dcf['price'])} {p['currency']}")
            if dcf.get("target"):
                lines.append(f"DCF目标日{dcf['target_date']}：{decimal_text(dcf['target']['price'])} {p['currency']}")
        for key, component in manual["components"].items():
            if component["status"] != "available":
                lines.append(f"{key}：" + ("EPS非正，倍数估值不适用" if component["status"] == "not_applicable" else "缺少手工输入，未自动补齐"))
        for source in (p.get("sources") or [])[:3]:
            lines.append(f"手工来源：{source['title']}；{source['url']}")
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
    for model in combined.get("models") or []:
        if model["model"]=="eps":
            lines.append(f"EPS结合口径：{model['eps_period_type']}，{model['eps_period_start']}～{model['eps_period_end']}；" +
                         ("历史Forward PE对应的前瞻估值参考，不是一年后目标价。" if model.get("reference_kind") in ("forward_pe_reference","automatic_pe_reference","manual_pe_reference") else "按手工要求收益率折算终点股价，不含期间分红。"))
        elif model.get("date_proxy"):
            lines.append("DCF时点限制：" + model["date_note"])
    lines.append("基本面配合不改技术强弱；共振仅为研究参考，情景计数不是概率。")
    current_market = report.get("market") or {}
    return {"lines":lines, "warnings":warnings,"brief":summarize(report),
            "context":{"symbol":report["symbol"],"close":current_market.get("last_price"),
                       "data_as_of":current_market.get("last_complete_session"),
                       "currency":report["profile"].get("currency"),
                       "cache_state":report["cache"].get("state"),"cache_message":report["cache"].get("message")},
            "versions":{"manual_valuation":manual.get("id"),"market":(report.get("market") or {}).get("id"),
                        "manual_derived_version":manual.get("derived_version"),
                        "fundamental_technical":combined.get("version")}}

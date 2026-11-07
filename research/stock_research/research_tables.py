"""个股研究的紧凑终端视图；只读报告，不计算策略、不请求来源或模型。"""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .console_tables import table, width


def text(value):
    if value is None or value == "":
        return "—"
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()


def number(value, places=2, divisor=1):
    try:
        result = Decimal(str(value)) / Decimal(str(divisor))
        return f"{result:,.{places}f}" if result.is_finite() else "—"
    except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
        return "—"


def wrapped(value, columns=104):
    """按终端列宽换行，中文按两列计；长警告不把整张表撑开。"""
    lines = []; line = ""; used = 0
    for char in text(value):
        size = width(char)
        if used + size > columns and line:
            lines.append(line); line = ""; used = 0
        line += char; used += size
    return "\n".join([*lines, line])


def _timestamp(value, zone):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return text(value)


def _technical_table(report):
    technical = ((report.get("market") or {}).get("technical") or {})
    if not technical:
        return ["暂无可用技术结构。"]
    if technical.get("stale"):
        return ["技术算法或参数已变化，旧区间不作为当前技术结果；普通查询可复用行情重算，--details保留旧档案。"]
    zones = technical.get("zones") or []
    chosen = []
    for kind, limit in (("inside", 1), ("support", 3), ("resistance", 3)):
        candidates = sorted([z for z in zones if z.get("kind") == kind], key=lambda z: abs(float(z.get("distance_pct") or 0)))
        visible = candidates[:limit]
        confirmed = next((z for z in candidates if z.get("role_status") == "confirmed"), None)
        if confirmed is not None and confirmed not in visible:
            visible.append(confirmed)
        chosen.extend(visible)
    rows = []
    for zone in chosen:
        quoted = zone.get("quote_low") is not None and zone.get("quote_high") is not None
        low = zone.get("quote_low") if quoted else zone.get("low")
        high = zone.get("quote_high") if quoted else zone.get("high")
        rows.append([text(zone.get("strength_label")), f"{number(low)}～{number(high)}",
                     "报价" if quoted else "调整价", number(zone.get("distance_pct")) + "%",
                     text(zone.get("touches")), {"confirmed": "已确认", "pending_retest": "待反抽/回踩", "inside": "方向待确认"}.get(zone.get("role_status"), "未确认")])
    lines = [table(["角色与强弱", "价格区间", "尺度", "距收盘", "触及次数", "角色状态"], rows)] if rows else ["暂无区间；不补造支撑或压力。"]
    for kind, label in (("support", "支撑"), ("resistance", "压力")):
        if not any(z.get("kind") == kind and z.get("role_status") == "confirmed" for z in zones):
            lines.append(f"当前没有已确认的{label}；候选区间仍需验证。")
    factor = technical.get("quote_conversion_factor")
    divisor = factor if factor else 1
    lines.append("均线/ATR（" + ("最近收盘报价尺度" if factor else "技术调整价") + "）：" + "；".join(
        [f"MA{k} {number(v, divisor=divisor)}" for k, v in (technical.get("averages") or {}).items()] +
        ["ATR " + number(technical.get("atr"), divisor=divisor)]))
    lines.append(f"共{len(zones)}个区间；展示现价区间、最近各3个支撑/压力候选，并保留最近已确认角色；强弱不代表成功概率。")
    return lines


def _valuation_table(report):
    if (report.get("profile") or {}).get("type") == "ETF":
        return ["ETF不套用公司EPS或DCF模型。"]
    manual = report.get("manual_valuation") or {}
    if manual.get("status") in (None, "missing", "needs_review"):
        lines = [text(manual.get("message") or "缺少手工估值资料，请录入并确认；不自动补齐。")]
        if manual.get("draft"):
            lines.append("已有未确认草稿，尚不能作为当前估值。")
            gaps = manual["draft"].get("missing_fields") or []
            if gaps:
                lines.append(wrapped("草稿待补：" + "、".join(gaps[:10]) + ("等，完整清单见--details" if len(gaps)>10 else "")))
        return lines
    p = manual["input"]
    if p.get("schema_version") == "manual-eps-pe-v4":
        from .manual_multiples import render_lines
        return [f"本人确认版本；确认时间{text(manual.get('confirmed_at'))}", *render_lines(manual)]
    if p.get("schema_version") == "manual-eps-v3":
        from .eps_only import render_lines
        return [f"本人确认版本；确认时间{text(manual.get('confirmed_at'))}", *render_lines(manual)]
    if p.get("schema_version") == "manual-valuation-v2":
        from .compact_valuation import render_lines
        return [f"本人确认版本；确认时间{text(manual.get('confirmed_at'))}", *render_lines(manual)]
    target = p["target"]
    lines = [f"人工确认的研究假设 · 资料日{text(p['as_of'])} · 确认时间{text(manual.get('confirmed_at'))} · {p['currency']}"]
    components = manual["components"]
    current = p["current_ttm"]; pe = p["observed_pe"]
    if components["current_ttm"]["status"] == "available":
        lines.append(f"手工TTM EPS {number(current['eps'],4)}；期间{current['period_start']}～{current['period_end']}；{current['eps_basis']}")
    if components["pe"]["status"] == "available":
        lines.append(f"观察PE {number(pe['value'])}倍（{pe['as_of']}，{pe['basis']}），仅为原时点，不改写为当前实时PE。")
    if manual.get("scenarios"):
        lines.append(f"目标估值日{text(target['date'])}；EPS期间{target['eps_period_start']}～{target['eps_period_end']}；{target['eps_period_type']}；{target['eps_basis']}")
        lines.append(table(["情景","手工EPS","手工目标PE","程序计算EPS×PE"],
                           [[r['name'],number(r['eps'],4),number(r['pe']),number(r['price'])] for r in manual['scenarios']],numeric=(1,2,3)))
    dcf = manual.get("dcf_result")
    if dcf:
        lines.append(f"DCF：资料日{dcf['as_of']}每股价值{number(dcf['price'])} {p['currency']}，终值占比{number(Decimal(dcf['terminal_share'])*100)}%。")
        lines.append(f"DCF目标日{dcf['target_date']}每股价值{number(dcf['target']['price'])} {p['currency']}。" if dcf.get('target') else "未输入一年后资产负债/股数，不生成DCF一年目标价。")
    for key, part in components.items():
        if part['status'] == 'not_applicable':
            lines.append("EPS非正，倍数估值不适用，不生成正目标价。")
        elif part['status'] != 'available':
            lines.append(wrapped(f"{key}待补：" + "、".join(part.get('missing_fields') or [])))
    lines.append(wrapped("盈利质量/正常化说明：" + text(target.get('normalization_note'))))
    lines.extend(wrapped("手工风险说明：" + risk) for risk in p.get('risks') or [])
    if p.get('missing_fields'):
        lines.append(wrapped("输入者另列缺项：" + "、".join(p['missing_fields'])))
    for source in p['sources']:
        lines.append(wrapped(f"来源[{source['id']}] {source['title']} · {source['publisher']} · {source['url']}"))
    lines.append("输入由本人核对，程序只做确定性计算；估值情景与技术支撑压力分别展示，不代表已通过账户风控。")
    return lines


def _combined_table(report):
    combined = report.get("fundamental_technical") or {}
    lines = [f"比较日{text(combined.get('comparison_date'))}；技术截至{text(combined.get('technical_as_of'))}；币种{text(combined.get('currency'))}。"]
    compact = ((report.get("manual_valuation") or {}).get("input") or {}).get("schema_version") in ("manual-valuation-v2", "manual-eps-v3", "manual-eps-pe-v4")
    lines.append(f"参与模型{combined.get('model_count',0)}/{1 if compact else 2}；" +
                 ("精简模式仅使用EPS方法，DCF不属于必填项。" if compact else
                  "当前仅单模型参考，其他模型缺项见下方。" if combined.get('model_count')==1 else "各模型独立判断，不对冲突取平均。"))
    lines.extend(wrapped("结合条件未满足：" + reason) for reason in combined.get("issues") or [])
    for model in combined.get("models") or []:
        lines.append(wrapped(f"可比依据：{model['label']} {number(model['low'])}～{number(model['high'])}；{model['basis']}"))
        if model.get("reference_kind") in ("forward_pe_reference", "automatic_pe_reference", "manual_pe_reference"):
            lines.append(wrapped(f"资料日{model['as_of']}；固定EPS期间{model['eps_period_start']}～{model['eps_period_end']}（{model['eps_period_type']}）；{model['snapshot_note']}"))
            lines.extend(wrapped(x) for x in model.get("warnings") or [])
        if model.get("date_proxy"):
            lines.append(wrapped("时点限制：" + model["date_note"]))
    for excluded in combined.get("excluded_models") or []:
        lines.append(wrapped(f"{excluded['model']}未参与结合：{excluded['message']}"))
    zones = ((report.get("market") or {}).get("technical") or {}).get("zones") or []
    rows = []
    for item in combined.get("zones") or []:
        index = item["zone_index"]
        zone = zones[index] if index < len(zones) else {}
        low, high = zone.get("quote_low"), zone.get("quote_high")
        overlap = item.get("overlap")
        rows.append([text(zone.get("strength_label")), f"{number(low)}～{number(high)}",
                     item["label"], "研究共振" if item["confluence"] else "未标共振",
                     f"{number(overlap['low'])}～{number(overlap['high'])}" if overlap else "—"])
    if rows:
        lines.append(table(["技术角色/原强弱", "技术报价区间", "基本面配合", "综合标记", "共同交集"], rows))
    else:
        lines.append("暂无技术区间，不会用估值数字凭空创建支撑压力。")
    for item in combined.get("zones") or []:
        if item.get("kind") not in ("support", "resistance"):
            continue
        explanations = "；".join(item.get("reasons") or [])
        evidence = "；".join(f"{e['model']}：{e['label']}，冲突{e['conflicting_scenarios']}/{e['scenario_count']}个情景" for e in item.get("model_evidence") or [])
        lines.append(wrapped(f"区间#{item['zone_index']+1}依据：{explanations}" + ("；" + evidence if evidence else "")))
    lines.append("支持/冲突按支撑或压力角色分别解释；情景计数不是概率。原技术强弱不变，研究共振不等于买入或退出许可。")
    return lines


def _weekly_valuation_table(report, zone):
    value = report.get("automatic_valuation_reference")
    if not value:
        return ["尚无已保存的每周估值参考。"]
    attempt = value["attempt"]
    lines = [f"Stock Analysis · 最近尝试 {attempt['date']}（北京时间）"]
    if value["stale"]:
        lines.append("含一周前取得的资料，留作历史参考。")
    eps = value.get("eps")
    if eps:
        lines.append(f"财年截至 {eps['period_end']}；三档EPS {number(eps['low'])} / {number(eps['mean'])} / {number(eps['high'])}"
                     f" {eps['currency_per_share']}；取得 {_timestamp(eps['fetched_at'],zone)}")
    pe = value.get("forward_pe")
    if pe:
        lines.append(f"历史Forward PE P25 / P50 / P75 {number(pe['p25'])} / {number(pe['p50'])} / {number(pe['p75'])}"
                     f"；{pe['sample_count']}个季度样本；取得 {_timestamp(pe['fetched_at'],zone)}")
    if value["scenarios"]:
        lines.append("情景估值（USD） " + " / ".join(f"{s['name']} {number(s['price'])}" for s in value["scenarios"]))
    lines.extend([value["message"],*attempt["issues"]])
    return lines


def render_summary(report, display_timezone="Asia/Shanghai"):
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("RESEARCH_DISPLAY_TIMEZONE不是有效时区，例如Asia/Shanghai或UTC") from None
    market = report.get("market") or {}; cache = report.get("cache") or {}; profile = report.get("profile") or {}
    manual = report.get("manual_valuation") or {}
    currency = profile.get('currency') or (manual.get('input') or {}).get('currency')
    lines = [f"{report['symbol']} · {text(profile.get('name'))} · {text(currency)}",
             f"完整日线 {text(market.get('last_complete_session'))}，收盘 {number(market.get('last_price'))}；应有最近收盘日 {text(cache.get('expected_session'))}",
             f"报告生成 {_timestamp(report.get('generated_at'), zone)}；行情取得 {_timestamp(market.get('fetched_at'), zone)}（时间均为{display_timezone}）",
             "已确认估值来自手工输入；每周自动估值单独展示。", "", "【数据概览与缺口】"]
    state = {"missing":"未录入/未确认","needs_review":"待复核","available":"已确认，EPS可计算","partial":"已确认，部分模型有缺项"}.get(manual.get('status'),'缺失')
    lines.append(table(["项目","状态","说明"], [
        ["行情/技术",{"fresh":"有效","stale":"待更新","unknown":"待核对"}.get(cache.get('state'),'缺失'),text(cache.get('message'))],
        ["财务/估值",state,text(manual.get('message'))],
        ["财报/电话会议/外部研究","自动链路停用","不抓取、不读取旧材料补值；手工缺项明确提示"],
    ]))
    warnings = [w.get('message','资料未齐') if isinstance(w,dict) else str(w) for w in (report.get('command') or {}).get('warnings') or []]
    if market.get('missing_sessions'):
        warnings.append(f"行情缺失{len(market['missing_sessions'])}个交易日，详细日期见--details。")
    for warning in dict.fromkeys(warnings):
        lines.append(wrapped("注意：" + warning))
    for title, contents in (("技术支撑压力",_technical_table(report)), ("手工输入与程序估值",_valuation_table(report)),
                            ("每周自动估值参考",_weekly_valuation_table(report,zone)),
                            ("技术与基本面综合判断",_combined_table(report))):
        lines.extend(["",f"【{title}】",*contents])
    lines.extend(["", "--details 展开手工来源、假设和技术依据；--json导出当前结构化结果。",
                  "K线仅后台计算；旧研究档案保留但不作为当前输入，缺资料不阻塞技术与退出提示。"])
    return "\n".join(lines) + "\n"


def render_levels(report, display_timezone="Asia/Shanghai", details=False):
    """独立比对命令复用同一技术/综合表，避免另算一套价位。"""
    import json
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("显示时区无效") from None
    market = report.get("market") or {}; cache = report.get("cache") or {}
    lines = [f"{report['symbol']} · 支撑压力独立比对",
             f"完整日线{market.get('last_complete_session') or '缺失'}；收盘{number(market.get('last_price'))}；币种{text(report['profile'].get('currency'))}",
             f"行情状态{text(cache.get('state'))}；取得时间{_timestamp(market.get('fetched_at'),zone)}（{display_timezone}）",
             f"行情版本{text(market.get('id'))}；手工版本{text(report.get('manual_valuation_id'))}"]
    lines.extend(wrapped("提示：" + w["message"]) for w in report["command"].get("warnings") or [])
    lines.extend(["", "【技术支撑压力】", *_technical_table(report)])
    if report.get("manual_valuation"):
        lines.extend(["", "【EPS与PE估值参考】", *_valuation_table(report)])
    lines.extend(["", "【技术与基本面配合】", *_combined_table(report)])
    if details:
        lines.extend(["", "【完整区间、技术证据与结合计算】", json.dumps({"technical":market.get("technical"),
                      "manual_valuation":report.get("manual_valuation"),
                      "fundamental_technical":report.get("fundamental_technical")},ensure_ascii=False,indent=2,allow_nan=False)])
    lines.append("本命令不扫描买卖策略、不发送消息；基本面缺失不阻止查看技术区间。")
    return "\n".join(lines)+"\n"

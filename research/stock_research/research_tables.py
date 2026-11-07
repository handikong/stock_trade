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


def _combined_table(report):
    combined = report.get("fundamental_technical") or {}
    lines = [f"比较日{text(combined.get('comparison_date'))}；技术截至{text(combined.get('technical_as_of'))}；币种{text(combined.get('currency'))}。"]
    lines.append(f"参与模型 {combined.get('model_count',0)}")
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
        return ["基金不适用公司EPS估值。"]
    attempt = value.get("attempt") or {}
    lines = [f"Stock Analysis · 最近尝试 {attempt['date']}（北京时间）"] if attempt else []
    if value.get("corrected_at"):
        lines.append(f"含网页更正 · {_timestamp(value['corrected_at'],zone)}")
    if value["stale"]:
        lines.append("含一周前取得的资料，留作历史参考。")
    eps = value.get("eps")
    if eps:
        lines.append(f"财年截至 {eps['period_end']}；三档EPS {number(eps['low'])} / {number(eps['mean'])} / {number(eps['high'])}"
                     f" {eps['currency_per_share']}；取得 {_timestamp(eps.get('fetched_at'),zone)}")
    pe = value.get("forward_pe")
    if pe:
        samples = f"；源表{pe['sample_count']}个季度样本" if pe.get("sample_count") is not None else ""
        lines.append(f"Forward PE 低 / 中 / 高 {number(pe['p25'])} / {number(pe['p50'])} / {number(pe['p75'])}"
                     f"{samples}；源数据 {_timestamp(pe.get('fetched_at'),zone)}")
    if value["scenarios"]:
        lines.append("情景估值（USD） " + " / ".join(f"{s['name']} {number(s['price'])}" for s in value["scenarios"]))
    lines.extend([value["message"],*(attempt.get("issues") or [])])
    return lines


def render_summary(report, display_timezone="Asia/Shanghai"):
    try:
        zone = ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("RESEARCH_DISPLAY_TIMEZONE不是有效时区，例如Asia/Shanghai或UTC") from None
    market = report.get("market") or {}; cache = report.get("cache") or {}; profile = report.get("profile") or {}
    valuation = report.get("automatic_valuation_reference")
    currency = profile.get("currency")
    lines = [f"{report['symbol']} · {text(profile.get('name'))} · {text(currency)}",
             f"完整日线 {text(market.get('last_complete_session'))}，收盘 {number(market.get('last_price'))}；应有最近收盘日 {text(cache.get('expected_session'))}",
             f"报告生成 {_timestamp(report.get('generated_at'), zone)}；行情取得 {_timestamp(market.get('fetched_at'), zone)}（时间均为{display_timezone}）",
             "估值每周六采集，网页更正后同步用于研究展示。", "", "【数据概览与缺口】"]
    lines.append(table(["项目","状态","说明"], [
        ["行情/技术",{"fresh":"有效","stale":"待更新","unknown":"待核对"}.get(cache.get('state'),'缺失'),text(cache.get('message'))],
        ["估值","可试算" if valuation and valuation["scenarios"] else "资料待补" if valuation else "不适用",
         valuation["message"] if valuation else "基金不适用公司EPS估值"],
    ]))
    warnings = [w.get('message','资料未齐') if isinstance(w,dict) else str(w) for w in (report.get('command') or {}).get('warnings') or []]
    if market.get('missing_sessions'):
        warnings.append(f"行情缺失{len(market['missing_sessions'])}个交易日，详细日期见--details。")
    for warning in dict.fromkeys(warnings):
        lines.append(wrapped("注意：" + warning))
    for title, contents in (("技术支撑压力",_technical_table(report)), ("估值参考",_weekly_valuation_table(report,zone)),
                            ("技术与基本面综合判断",_combined_table(report))):
        lines.extend(["",f"【{title}】",*contents])
    lines.extend(["", "--details 展开估值来源、假设和技术依据；--json导出当前结构化结果。",
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
    valuation = report.get("automatic_valuation_reference") or {}
    lines = [f"{report['symbol']} · 支撑压力独立比对",
             f"完整日线{market.get('last_complete_session') or '缺失'}；收盘{number(market.get('last_price'))}；币种{text(report['profile'].get('currency'))}",
             f"行情状态{text(cache.get('state'))}；取得时间{_timestamp(market.get('fetched_at'),zone)}（{display_timezone}）",
             f"行情版本{text(market.get('id'))}；估值版本{text(valuation.get('id'))}"]
    lines.extend(wrapped("提示：" + w["message"]) for w in report["command"].get("warnings") or [])
    lines.extend(["", "【技术支撑压力】", *_technical_table(report)])
    lines.extend(["", "【估值参考】", *_weekly_valuation_table(report,zone)])
    lines.extend(["", "【技术与基本面配合】", *_combined_table(report)])
    if details:
        lines.extend(["", "【完整区间、技术证据与结合计算】", json.dumps({"technical":market.get("technical"),
                      "automatic_valuation_reference":valuation,
                      "fundamental_technical":report.get("fundamental_technical")},ensure_ascii=False,indent=2,allow_nan=False)])
    lines.append("本命令不扫描买卖策略、不发送消息；基本面缺失不阻止查看技术区间。")
    return "\n".join(lines)+"\n"

"""把手工估值与已有技术区间结合；不改技术评分、不联网、不产生交易许可。"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from .config import settings
from .store import digest

ALGORITHM = "fundamental-technical-current-research-v4"
LABELS = {"SUPPORTED":"估值支持", "CONFLICT":"估值冲突", "DIVERGENT":"情景分歧", "UNKNOWN":"资料不足"}


def policy():
    cfg = json.loads(Path(settings.fundamental_technical_policy_file).expanduser().read_text())
    if not isinstance(cfg, dict) or cfg.get("version") != "fundamental-technical-v1" or type(cfg.get("enabled")) is not bool:
        raise ValueError("基本面技术结合配置需要有效版本和enabled布尔值")
    if type(cfg.get("dcf_max_age_days")) is not int or not 0 <= cfg["dcf_max_age_days"] <= 365:
        raise ValueError("DCF允许资料日间隔需要0至365天；0表示必须同日")
    if cfg.get("confluence_min_strength") not in ("weak", "medium", "strong"):
        raise ValueError("共振技术强度门槛只支持weak/medium/strong")
    if type(cfg.get("confluence_min_models")) is not int or not 1 <= cfg["confluence_min_models"] <= 2:
        raise ValueError("共振需1至2个可比独立模型")
    return cfg


def _positive(value):
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError("缺少有效价格") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("价格须为有限正数")
    return result


def _model(key, label, points, basis, metadata):
    values = [Decimal(p["price"]) for p in points]
    return {"model":key, "label":label, "low":str(min(values)), "high":str(max(values)),
            "points":points, "basis":basis, **metadata}


def evaluate(market, cache, manual, profile, *, today=None, weekly=None):
    """当前研究的只读派生层，输入仅是已存行情视图及确认后的手工估值。"""
    today = today or datetime.now(ZoneInfo(settings.research_display_timezone)).date()
    market = market or {}; technical = market.get("technical") or {}; manual = manual or {}
    cache = cache or {}; profile = profile or {}
    cfg = None; issues = []; excluded = []; models = []
    try:
        cfg = policy()
        if not cfg["enabled"]:
            issues.append("基本面与技术综合判断已在配置中关闭")
    except (ValueError, OSError, TypeError):
        issues.append("基本面与技术结合配置不可用，保留原技术结果")
    if cache.get("state") != "fresh":
        issues.append("行情不是完整有效状态，不发布当前综合判断")
    if technical.get("stale") or technical.get("status") != "ready":
        issues.append("技术算法/资料待更新，不能据旧区间发布当前综合判断")
    try:
        technical_day = date.fromisoformat(technical.get("as_of", ""))
        if technical_day > today or technical.get("as_of") != market.get("last_complete_session"):
            raise ValueError
    except (ValueError, TypeError):
        issues.append("技术区间日期与最近完整日线不一致或日期无效")
    p = manual.get("input") or {}
    currency = profile.get("currency")
    if profile.get("type") in ("ETF", "MUTUALFUND"):
        issues.append("基金不适用公司EPS/DCF综合判断")
    elif weekly is not None:
        if weekly.get("scenarios"):
            issues.append("财年EPS与Forward PE分母口径待核对，当前估值只作情景参考")
        else:
            issues.append("当前估值资料未齐，技术区间单独显示")
    else:
        if manual.get("status") not in ("available", "partial") or not manual.get("id") or not manual.get("confirmed_at"):
            issues.append("缺少有效已确认手工估值，未确认/待复核数据不能参与结合")
        if not currency or p.get("currency") != currency:
            issues.append("证券与估值币种不明确或不一致，不自动换汇比较")

    if not issues:
        manual_pe = p.get("schema_version") == "manual-eps-pe-v4"
        automatic = p.get("schema_version") == "manual-eps-v3"
        compact = manual_pe or automatic or p.get("schema_version") == "manual-valuation-v2"
        if manual_pe:
            rows = manual.get("scenarios") or []; ref = manual.get("reference") or {}
            if rows and ref.get("comparable"):
                forecast = p["eps"]
                models.append(_model("eps", "手工EPS×手工历史PE", [{"name":r["name"],"price":r["price"]} for r in rows],
                    "按本人确认的NTM及盈利口径计算前瞻倍数参照，不是未来终点股价", {
                    "reference_kind":"manual_pe_reference", "as_of":p["as_of"], "eps_period_type":"ntm",
                    "eps_period_start":forecast["period_start"], "eps_period_end":forecast["period_end"],
                    "warnings":ref.get("warnings", []), "snapshot_note":"固定录入日的手工EPS/PE情景；来源由本人核对，不自动获取或更新"}))
            else:
                excluded.append({"model":"eps", "message":"手工PE不完整或简式输入口径尚未明确；估值假设仍单独展示，不提升技术强弱"})
        elif automatic:
            excluded.append({"model":"eps", "message":"旧档案只有EPS，请补录手工PE；自动PE获取及旧缓存回退均已停用"})
        elif compact:
            eps = manual.get("scenarios") or []
            ref = manual.get("reference") or {}
            if manual.get("components", {}).get("eps", {}).get("status") == "available" and eps and ref.get("comparable"):
                forecast = p["eps"]; history = p["historical_pe"]
                points = [{"name":r["name"], "price":str(_positive(r["price"]))} for r in eps]
                models.append(_model("eps", "预测EPS×历史Forward PE参考", points,
                    "查询日预测与历史同口径Forward PE的研究参照，不是未来终点股价，不作终点折现", {
                        "reference_kind":"forward_pe_reference", "as_of":p["as_of"], "comparison_date":today.isoformat(),
                        "eps_period_type":forecast["period_type"], "eps_period_start":forecast["period_start"],
                        "eps_period_end":forecast["period_end"], "historical_pe":history,
                        "source_ids":list(dict.fromkeys(forecast["source_ids"] + history["source_ids"])),
                        "warnings":ref.get("warnings", []), "snapshot_note":"使用已确认资料日的固定预测期间，不冒充今天重新滚动/更新的预测"}))
            else:
                excluded.append({"model":"eps", "message":"精简EPS/PE输入不完整、不适用，或历史PE与预测EPS期间口径不同；不发布基本面确认"})
        # DCF的企业现金流折现率不能用来把普通股价格跨期滚动，默认仅同日直接比较。
        dcf = manual.get("dcf_result")
        if not compact and (manual.get("components", {}).get("dcf", {}).get("status") == "available") and dcf:
            try:
                dcf_day = date.fromisoformat(dcf["as_of"])
                age = (today - dcf_day).days
                if dcf.get("currency") != currency or dcf["as_of"] != p.get("as_of"):
                    raise ValueError("DCF币种或资料日不一致")
                if not 0 <= age <= cfg["dcf_max_age_days"]:
                    raise ValueError("DCF资料日与当前比较日未对齐；不擅自用WACC滚动股价")
                value = _positive(dcf["price"])
                models.append(_model("dcf", "DCF资料日内在价值", [{"name":"手工路径", "price":str(value)}],
                    "手工完整路径的基础情景；敏感性表不是额外独立模型，不作为投票", {"as_of":dcf["as_of"],
                    "comparison_date":today.isoformat(), "age_days":age, "date_proxy":age>0,
                    "source_ids":(p.get("dcf") or {}).get("source_ids", []),
                    "date_note":"同日估值" if age==0 else "配置允许的旧资料日代理，未桥接经营变化"}))
            except (ValueError, KeyError, TypeError) as exc:
                excluded.append({"model":"dcf", "message":str(exc)})
        elif not compact:
            excluded.append({"model":"dcf", "message":"未取得有效完整的手工DCF输入与结果"})
        eps = manual.get("scenarios") or []
        target = p.get("target") or {}; alignment = p.get("technical_alignment") or {}
        if not compact and manual.get("components", {}).get("eps", {}).get("status") == "available" and eps:
            try:
                if manual.get("alignment_status", {}).get("status") != "available":
                    raise ValueError("未来EPS目标缺少有依据的股权要求收益率/价格分红口径，不能直接当成当前支撑压力")
                target_day = date.fromisoformat(target["date"])
                if target_day <= today:
                    raise ValueError("EPS目标日已到或已过")
                rate = Decimal(alignment["annual_required_return"])
                if not rate.is_finite() or not 0 <= rate < 1 or alignment.get("cash_flow_treatment") != "terminal_price_only_excluding_dividends":
                    raise ValueError("折算要求收益率或现金流口径无效")
                if len(eps) != 3:
                    raise ValueError("EPS三情景不完整")
                with localcontext() as ctx:
                    ctx.prec = 40
                    years = Decimal((target_day - today).days) / Decimal("365.25")
                    factor = (1 + rate) ** years
                    points = [{"name":r["name"], "price":str(_positive(r["price"]) / factor), "target_price":r["price"]} for r in eps]
                models.append(_model("eps", "EPS目标价格折算参考", points,
                    "目标EPS×PE按手工股权要求收益率折算，仅终点价格、不含期间分红；不是总回报现值或统计置信区间", {
                        "as_of":p.get("as_of"), "comparison_date":today.isoformat(), "target_date":target["date"],
                        "eps_period_type":target.get("eps_period_type"), "eps_period_start":target.get("eps_period_start"),
                        "eps_period_end":target.get("eps_period_end"), "annual_required_return":str(rate),
                        "day_count":"ACT/365.25", "source_ids":alignment.get("source_ids", []), "source_note":alignment.get("source_note")}))
            except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                excluded.append({"model":"eps", "message":str(exc)})
        elif not compact:
            excluded.append({"model":"eps", "message":"EPS模型缺失或不适用，未参与比较"})

    zones = []
    for index, zone in enumerate(technical.get("zones") or []):
        item = {"zone_index":index, "kind":zone.get("kind"), "technical_strength":zone.get("strength"),
                "role_status":zone.get("role_status"), "status":"UNKNOWN", "label":LABELS["UNKNOWN"],
                "model_evidence":[], "confluence":False, "overlap":None, "reasons":[]}
        if issues or not models:
            item["reasons"] = list(issues) or ["没有时点和币种可比的基本面估值；技术强度仍单独保留"]
        elif zone.get("kind") not in ("support", "resistance"):
            item["reasons"] = ["现价区间方向未定，不用估值把它强行认定为支撑或压力"]
        else:
            try:
                # 估值是每股报价币种，绝不与Adj Close调整价区间直接比较。
                low, high = (_positive(zone[k]) for k in ("quote_low", "quote_high"))
                if high < low:
                    raise ValueError
                item.update(quote_low=str(low), quote_high=str(high))
                for model in models:
                    values = [Decimal(v["price"]) for v in model["points"]]
                    conflict = sum(v < low if zone["kind"] == "support" else v > high for v in values)
                    status = "CONFLICT" if conflict == len(values) else "DIVERGENT" if conflict else "SUPPORTED"
                    a, b = max(low, Decimal(model["low"])), min(high, Decimal(model["high"]))
                    item["model_evidence"].append({"model":model["model"], "status":status, "label":LABELS[status],
                        "model_low":model["low"], "model_high":model["high"], "conflicting_scenarios":conflict,
                        "scenario_count":len(values), "overlap":{"low":str(a),"high":str(b)} if a<=b else None})
                statuses = {e["status"] for e in item["model_evidence"]}
                status = next(iter(statuses)) if len(statuses)==1 else "DIVERGENT"
                item.update(status=status, label=LABELS[status])
                a = max([low] + [Decimal(m["low"]) for m in models])
                b = min([high] + [Decimal(m["high"]) for m in models])
                item["overlap"] = {"low":str(a), "high":str(b)} if a<=b else None
                strengths = {"weak":0,"medium":1,"strong":2}
                item["confluence"] = (status=="SUPPORTED" and a<=b and zone.get("role_status")=="confirmed"
                    and strengths.get(zone.get("strength"),-1)>=strengths[cfg["confluence_min_strength"]]
                    and len(models)>=cfg["confluence_min_models"])
                item["reasons"].append({"SUPPORTED":"可比估值未与该技术角色冲突，不代表价格一定守住或遇阻",
                    "CONFLICT":"估值情景与该技术角色全部冲突：支撑偏贵或压力偏低",
                    "DIVERGENT":"同模型情景或不同模型判断不一致，不平均消除分歧"}[status])
                if a>b:
                    item["reasons"].append("技术区间与所有可比模型没有共同交集，不标共振")
                if zone.get("role_status")!="confirmed":
                    item["reasons"].append("技术角色尚未确认，估值不能替代回踩/反抽确认")
                if len(models)<cfg["confluence_min_models"]:
                    item["reasons"].append("可比模型数量不足配置的共振门槛")
                if strengths.get(zone.get("strength"),-1)<strengths[cfg["confluence_min_strength"]]:
                    item["reasons"].append("原技术强度未达到配置的共振门槛，不因估值提高原评级")
                if item["confluence"]:
                    item["reasons"].append("技术强度及角色满足配置，可比模型无冲突且存在共同区间，仅作研究共振参考")
            except (ValueError, KeyError, TypeError):
                item["reasons"] = ["技术区间缺少有效报价尺度边界，不拿调整价猜测换算"]
        zones.append(item)
    sources = {"market":market.get("id")}
    if weekly is not None:
        sources["weekly_valuation"] = weekly.get("id")
    else:
        sources.update(manual_valuation=manual.get("id"),manual_version=manual.get("version"),
                       manual_derived_version=manual.get("derived_version"))
    result = {"algorithm":ALGORITHM, "evaluation_date":today.isoformat(), "comparison_date":today.isoformat(),
        "technical_as_of":technical.get("as_of"), "currency":currency, "policy":cfg, "sources":sources,
        "status":"unavailable" if issues or not models else "partial" if excluded else "available",
        "issues":issues, "excluded_models":excluded, "model_count":len(models), "models":models, "zones":zones,
        "message":"当前研究对照，不是历史当日可知结论；不修改技术区间/强弱、策略/回测或交易许可，不新建估值支撑位"}
    result["version"] = digest(result)
    return result

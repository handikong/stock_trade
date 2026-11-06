"""手工估值公共入口：只校验/计算/存档，不联网、不调用模型、不回退自动估值。"""
from datetime import date, datetime, timedelta, timezone
from datetime import date as Date
from decimal import Decimal, localcontext
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import fcntl
import re

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator
from sqlalchemy import select

from .config import settings
from .store import Security, Session, Snapshot, digest, latest, save_snapshot, serialize_snapshot, stamp
from .valuation import calculate, number

POLICY = "manual-eps-pe-v4"
SCHEMA = "manual-valuation-v1"
MISSING = "缺少已确认的手工估值资料，请录入EPS、PE、期间和来源；不会自动补齐"
NAMES = ("保守", "基准", "乐观")


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def dates_are_iso_strings(cls, value, info):
        if value is not None and info.field_name in ("date", "as_of", "period_start", "period_end", "eps_period_start", "eps_period_end", "latest_report_period_end"):
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("日期使用YYYY-MM-DD字符串")
        return value


class CurrentTTM(InputModel):
    period_start: date | None = None
    period_end: date | None = None
    eps: StrictStr | None = None
    eps_basis: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)


class ObservedPE(InputModel):
    value: StrictStr | None = None
    as_of: date | None = None
    basis: StrictStr | None = None
    price_reference: StrictStr | None = None
    eps_reference: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)


class Scenario(InputModel):
    name: Literal["保守", "基准", "乐观"]
    eps: StrictStr | None = None
    pe: StrictStr | None = None
    eps_origin: Literal["analyst_consensus", "company_guidance", "research_assumption"] | None = None
    eps_reason: StrictStr | None = None
    pe_reason: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)


class Target(InputModel):
    date: Date | None = None
    eps_period_start: Date | None = None
    eps_period_end: Date | None = None
    eps_period_type: Literal["target_ttm", "ntm", "fy1"] | None = None
    eps_basis: StrictStr | None = None
    normalization_note: StrictStr | None = None
    scenarios: list[Scenario] = Field(default_factory=list, max_length=3)


class Source(InputModel):
    id: StrictStr = Field(min_length=1, max_length=100)
    title: StrictStr = Field(min_length=1, max_length=500)
    publisher: StrictStr = Field(min_length=1, max_length=500)
    url: StrictStr = Field(min_length=1, max_length=8192)
    published_at: StrictStr | None = None
    accessed_at: datetime | None = None
    supports: list[StrictStr] = Field(default_factory=list, max_length=100)


class DCFYear(InputModel):
    ebit: StrictStr | None = None
    tax_rate: StrictStr | None = None
    da: StrictStr | None = None
    capex: StrictStr | None = None
    delta_wc: StrictStr | None = None


class DCFState(InputModel):
    cash: StrictStr | None = None
    debt: StrictStr | None = None
    other_claims: StrictStr | None = None
    shares: StrictStr | None = None


class DCFInput(InputModel):
    nonfinancial_confirmed: bool = Field(default=False, strict=True)
    amount_scale: Literal["units", "millions"] | None = None
    shares_scale: Literal["units", "millions"] | None = None
    wacc: StrictStr | None = None
    terminal_growth: StrictStr | None = None
    cash: StrictStr | None = None
    debt: StrictStr | None = None
    other_claims: StrictStr | None = None
    shares: StrictStr | None = None
    years: list[DCFYear] = Field(default_factory=list, max_length=15)
    year_one_state: DCFState | None = None
    source_note: StrictStr | None = None
    terminal_policy_note: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)


class TechnicalAlignment(InputModel):
    # 股权要求收益率仅用于未来股价情景折算，不可暗用企业FCFF的WACC代替。
    annual_required_return: StrictStr | None = None
    cash_flow_treatment: Literal["terminal_price_only_excluding_dividends"] | None = None
    source_note: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=50)


class ManualInput(InputModel):
    schema_version: Literal["manual-valuation-v1"]
    symbol: StrictStr
    as_of: date | None = None
    currency: StrictStr
    security_type: Literal["EQUITY"] | None = None
    latest_report_period_end: date | None = None
    current_ttm: CurrentTTM = Field(default_factory=CurrentTTM)
    observed_pe: ObservedPE = Field(default_factory=ObservedPE)
    target: Target = Field(default_factory=Target)
    dcf: DCFInput | None = None
    technical_alignment: TechnicalAlignment | None = None
    sources: list[Source] = Field(default_factory=list, max_length=50)
    missing_fields: list[StrictStr] = Field(default_factory=list, max_length=200)
    risks: list[StrictStr] = Field(default_factory=list, max_length=200)


def _anniversary(value, years=1):
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def evaluate(data, symbol, profile, *, today=None):
    """纯输入评估；缺失可留草稿，结构冲突拒绝，GPT不能传confirmed字段。"""
    today = today or datetime.now(ZoneInfo(settings.research_display_timezone)).date()
    if not isinstance(data, dict) or len(str(data)) > 1_000_000:
        raise ValueError("手工估值需要不超过1MB的JSON对象")
    from . import eps_only, manual_multiples
    if manual_multiples.is_simple(data):
        data = manual_multiples.normalize(data, symbol, profile, today=today)
    if data.get("schema_version") == manual_multiples.SCHEMA:
        return manual_multiples.evaluate(data, symbol, profile, today=today)
    if eps_only.is_simple(data):
        # 原始三值按本机显示时区封装一次；存档重读使用固定日期。
        data = eps_only.normalize(data, symbol, profile, today=today)
    if data.get("schema_version") == eps_only.SCHEMA:
        return eps_only.evaluate(data, symbol, profile, today=today)
    if data.get("schema_version") == "manual-valuation-v2":
        from .compact_valuation import evaluate as evaluate_compact
        return evaluate_compact(data, symbol, profile, today=today)
    try:
        model = ManualInput.model_validate(data)
    except ValidationError as exc:
        locations = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)[:8]]
        raise ValueError("手工估值字段、类型或枚举不正确：" + "、".join(locations)) from None
    if model.symbol != symbol or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", model.symbol):
        raise ValueError("手工估值股票代码必须与当前查询股票一致，使用大写代码")
    if not re.fullmatch(r"[A-Z]{3}", model.currency):
        raise ValueError("手工估值币种需要三位大写代码")
    if profile.get("currency") and profile["currency"] != model.currency:
        raise ValueError("手工估值币种与已知证券报价币种冲突")
    if profile.get("type") in ("ETF", "MUTUALFUND"):
        raise ValueError("ETF或基金不适用公司EPS/PE估值")
    p = model.model_dump(mode="json")
    base = []
    for key in ("as_of", "latest_report_period_end"):
        if not p[key]:
            base.append(key)
        elif date.fromisoformat(p[key]) > today:
            raise ValueError(f"{key}不能晚于今天")
    if model.as_of and model.latest_report_period_end and model.latest_report_period_end > model.as_of:
        raise ValueError("已披露财报期末不能晚于资料查询日")
    effective_profile = dict(profile)
    if not effective_profile.get("type") or effective_profile.get("type") == "UNKNOWN":
        effective_profile["type"] = model.security_type
    if not effective_profile.get("currency"):
        effective_profile["currency"] = model.currency
    if effective_profile.get("type") != "EQUITY":
        base.append("security_type：请明确公司股票适用性，不能把未知证券当成普通公司")
    sources = {s.id: s for s in model.sources}
    if len(sources) != len(model.sources):
        raise ValueError("来源id不能重复")
    for source in model.sources:
        url = urlsplit(source.url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("来源必须是无用户名/口令的HTTPS公开链接；导入不下载链接")
        if not source.title.strip() or not source.publisher.strip() or not all(x.strip() for x in source.supports):
            raise ValueError("来源标题、发布者与supports不能填写空白")
        if source.accessed_at and (source.accessed_at.tzinfo is None or source.accessed_at > datetime.now(timezone.utc)):
            raise ValueError("来源访问时间必须含时区且不能晚于当前时间")
        if source.published_at:
            try:
                if len(source.published_at) == 10:
                    published = date.fromisoformat(source.published_at)
                else:
                    dt = datetime.fromisoformat(source.published_at.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        raise ValueError
                    published = dt.date()
            except ValueError:
                raise ValueError("来源发布时间须为日期或含时区ISO时间，未知请填null") from None
            if published > today or (model.as_of and published > model.as_of):
                raise ValueError("来源发布时间不能晚于资料查询日或今天")

    def refs(row, path, gaps):
        ids = row.get("source_ids") or []
        if not ids:
            gaps.append(path + ".source_ids")
        for sid in ids:
            if sid not in sources:
                raise ValueError(f"{path}引用了不存在的来源id")
            source = sources[sid]
            if not source.accessed_at or not source.supports:
                gaps.append(f"sources.{sid}.accessed_at/supports")

    def required(row, keys, path):
        unknown = ("", "unknown", "n/a", "未知", "待核对", "—")
        return [f"{path}.{k}" for k in keys if row.get(k) is None or (isinstance(row[k], str) and row[k].strip().lower() in unknown)]

    def period(row, start_key, end_key, path):
        if row.get(start_key) and row.get(end_key):
            start, end = (date.fromisoformat(row[k]) for k in (start_key, end_key))
            if not 330 <= (end - start).days + 1 <= 380:
                raise ValueError(f"{path}须明确接近完整12个月的EPS期间，不能用单季或半年值")

    current = p["current_ttm"]
    cg = required(current, ("period_start", "period_end", "eps", "eps_basis"), "current_ttm")
    refs(current, "current_ttm", cg)
    period(current, "period_start", "period_end", "current_ttm")
    if current["period_end"] and model.as_of and date.fromisoformat(current["period_end"]) > model.as_of:
        raise ValueError("实际TTM期末不能晚于资料查询日")
    if current["period_end"] and p["latest_report_period_end"] and current["period_end"] != p["latest_report_period_end"]:
        cg.append("current_ttm.period_end：需与手工声明的最新已核对财报期末一致")
    if current["eps"] is not None:
        number(current["eps"], "实际TTM EPS")

    observed = p["observed_pe"]
    pg = required(observed, ("value", "as_of", "basis", "price_reference", "eps_reference"), "observed_pe")
    refs(observed, "observed_pe", pg)
    if model.observed_pe.as_of and (model.observed_pe.as_of > today or (model.as_of and model.observed_pe.as_of > model.as_of)):
        raise ValueError("观察PE日期不能晚于资料查询日或今天")
    for key in ("value", "price_reference", "eps_reference"):
        if observed[key] is not None and number(observed[key], "观察PE及其股价/EPS") <= 0:
            raise ValueError("有效观察PE及其股价/EPS必须为正；不适用请填null并说明")

    target = p["target"]
    eg = required(target, ("date", "eps_period_start", "eps_period_end", "eps_period_type", "eps_basis", "normalization_note"), "target")
    period(target, "eps_period_start", "eps_period_end", "target")
    if model.target.date and model.as_of and model.target.date <= model.as_of:
        raise ValueError("目标估值日必须晚于资料查询日")
    if model.target.eps_period_end and model.as_of and model.target.eps_period_end <= model.as_of:
        raise ValueError("预测EPS期末必须晚于资料查询日")
    if model.target.eps_period_type == "target_ttm" and model.target.eps_period_end != model.target.date:
        eg.append("target：目标TTM期末必须等于目标估值日")
    if model.target.eps_period_type == "ntm" and model.as_of and (
            model.target.eps_period_start != model.as_of + timedelta(days=1) or
            model.target.eps_period_end != _anniversary(model.as_of)):
        eg.append("target：NTM必须对应查询日之后12个月，不能将FY1直接改名")
    names = [s.name for s in model.target.scenarios]
    if len(set(names)) != len(names):
        raise ValueError("保守、基准、乐观情景名称不能重复")
    for name in NAMES:
        if name not in names:
            eg.append(f"target.scenarios.{name}")
    rows = []; nonpositive = False
    for row in target["scenarios"]:
        path = f"target.scenarios.{row['name']}"
        eg.extend(required(row, ("eps", "pe", "eps_origin", "eps_reason", "pe_reason"), path))
        refs(row, path, eg)
        for key in ("eps", "pe"):
            if row[key] is not None:
                value = number(row[key], path + "." + key)
                if key == "pe" and value <= 0:
                    raise ValueError("目标PE必须为正，不适用请填null")
                if key == "eps" and value <= 0:
                    nonpositive = True
        if row["eps"] is not None and row["pe"] is not None and not eg and not base and not nonpositive:
            with localcontext() as ctx:
                ctx.prec = 48
                rows.append({"name": row["name"], "eps": row["eps"], "pe": row["pe"],
                             "price": str(Decimal(row["eps"]) * Decimal(row["pe"]))})
    # 三情景是一个完整模型；一项缺失或不适用时不能发布残缺价格区间。
    if eg or base or nonpositive or len(rows) != 3:
        rows = []
    dg = []; dcf_result = None
    dcf = p["dcf"]
    if dcf is None:
        dg.append("dcf：未录入手工DCF假设")
    else:
        dg.extend(required(dcf, ("amount_scale", "shares_scale", "wacc", "terminal_growth", "cash", "debt", "other_claims", "shares", "source_note", "terminal_policy_note"), "dcf"))
        if dcf["amount_scale"] and dcf["shares_scale"] and dcf["amount_scale"] != dcf["shares_scale"]:
            raise ValueError("DCF金额与股数必须声明相同数量级：units或millions")
        refs(dcf, "dcf", dg)
        if not dcf["nonfinancial_confirmed"]:
            dg.append("dcf.nonfinancial_confirmed：需确认适用非金融经营公司FCFF")
        if not 2 <= len(dcf["years"]) <= 15:
            dg.append("dcf.years：需要2至15年的完整预测路径")
        for index, year in enumerate(dcf["years"]):
            dg.extend(required(year, ("ebit", "tax_rate", "da", "capex", "delta_wc"), f"dcf.years.{index}"))
            for key, value in year.items():
                if value is not None:
                    number(value, f"dcf.years.{index}.{key}")
        if dcf["year_one_state"] is not None:
            dg.extend(required(dcf["year_one_state"], ("cash", "debt", "other_claims", "shares"), "dcf.year_one_state"))
            for key, value in dcf["year_one_state"].items():
                if value is not None:
                    number(value, "dcf.year_one_state." + key)
        for key in ("wacc", "terminal_growth", "cash", "debt", "other_claims", "shares"):
            if dcf[key] is not None:
                number(dcf[key], "dcf." + key)
        if not dg and not base:
            payload = {k: v for k, v in dcf.items() if k != "source_ids"}
            payload.update({"method": "dcf", "currency": model.currency, "valuation_date": p["as_of"]})
            dcf_result = calculate(payload, effective_profile)
            # 现金流路径从手工资料日开始，不把旧假设静默改成今天的估值。
            dcf_result["as_of"] = p["as_of"]
            dcf_result["target_date"] = _anniversary(model.as_of).isoformat() if dcf_result.get("target") else None
    alignment = p["technical_alignment"]
    ag = []
    if alignment is None:
        ag.append("未填写technical_alignment，未来目标价不能直接作为当前基本面参照")
    else:
        ag.extend(required(alignment, ("annual_required_return", "cash_flow_treatment", "source_note"), "technical_alignment"))
        refs(alignment, "technical_alignment", ag)
        if alignment["annual_required_return"] is not None:
            rate = number(alignment["annual_required_return"], "股权要求收益率")
            if not 0 <= rate < 1:
                raise ValueError("股权要求收益率需要0至1之间的小数（不含1），不自动使用WACC")
    components = {
        "current_ttm": {"status": "missing" if cg or base else "available", "missing_fields": cg},
        "pe": {"status": "missing" if pg or base else "available", "missing_fields": pg},
        "eps": {"status": "missing" if eg or base else "not_applicable" if nonpositive else "available", "missing_fields": eg},
        "dcf": {"status": "missing" if dg or base else "available", "missing_fields": dg},
    }
    return {"input": p, "input_hash": digest(p), "base_missing_fields": base, "components": components,
            "missing_fields": list(dict.fromkeys(base + cg + pg + eg + dg)), "scenarios": rows, "dcf_result": dcf_result,
            "alignment_status": {"status":"missing" if ag or base else "available", "missing_fields":ag},
            "message": "手工研究情景；来源内容与预测合理性仍需本人核对，不代表公司指引或账户风控通过"}


def preview(security_id, data):
    with Session() as session:
        security = session.get(Security, security_id)
        if security is None:
            raise ValueError("股票档案不存在")
        from .eps_only import attach
        return attach(evaluate(data, security.symbol, security.profile), session, security)


def save(security_id, data, *, confirm=False, expected_hash=None):
    """确认来自调用参数，不能信任输入JSON。相同状态/内容去重，变更只追加。"""
    if type(confirm) is not bool:
        raise ValueError("confirm必须是显式布尔值")
    if not re.fullmatch(r"[0-9a-f]{32}", security_id):
        raise ValueError("股票档案id无效")
    folder = settings.storage / "manual-valuation-locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (security_id + ".lock")).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with Session() as session:
            security = session.get(Security, security_id)
            if security is None:
                raise ValueError("股票档案不存在")
            result = evaluate(data, security.symbol, security.profile)
            if expected_hash is not None and expected_hash != result["input_hash"]:
                raise ValueError("输入与预览版本不同，请重新核对后确认")
            usable = any(c["status"] in ("available", "not_applicable") for c in result["components"].values())
            if confirm and (result["base_missing_fields"] or not usable):
                raise ValueError("手工输入尚无完整可确认项目，请先补齐资料或保存为未确认草稿")
            current_day = datetime.now(ZoneInfo(settings.research_display_timezone)).date()
            if confirm and (result["input"].get("target") or {}).get("date") and date.fromisoformat(result["input"]["target"]["date"]) <= current_day:
                raise ValueError("目标日期已过，请更新后再确认")
            eps_end = (result["input"].get("eps") or {}).get("period_end")
            if confirm and eps_end and eps_end <= current_day.isoformat():
                raise ValueError("预测EPS期间已结束，请更新后再确认")
            key = digest({"input_hash": result["input_hash"], "confirm": confirm})
            # 仅与该状态的最新版本去重；重新选择更早内容也应追加新的当前版本。
            records = session.scalars(select(Snapshot).where(Snapshot.security_id == security_id,
                Snapshot.kind == "manual_valuation").order_by(Snapshot.created_at.desc(), Snapshot.id.desc()))
            state = "confirmed" if confirm else "draft"
            existing = next((r for r in records if r.payload.get("status") == state), None)
            if existing and existing.fingerprint == key:
                from .eps_only import attach
                return attach({**serialize_snapshot(existing), "cached": True}, session, security)
            payload = {**result, "policy": POLICY, "status": "confirmed" if confirm else "draft",
                       "confirmed_at": stamp() if confirm else None}
            item = save_snapshot(session, security_id, "manual_valuation", payload)
            item.fingerprint = key
            session.commit()
            from .eps_only import attach
            return attach(serialize_snapshot(item), session, security)


def read(session, security, *, today=None):
    """只读最新确认版本；新草稿不取代确认版本，新确认失效也不回退更老版本。"""
    today = today or datetime.now(ZoneInfo(settings.research_display_timezone)).date()
    records = session.scalars(select(Snapshot).where(Snapshot.security_id == security.id,
        Snapshot.kind == "manual_valuation").order_by(Snapshot.created_at.desc(), Snapshot.id.desc()))
    active = next((r for r in records if r.payload.get("status") == "confirmed"), None)
    last = latest(session, security.id, "manual_valuation")
    draft = serialize_snapshot(last) if last and last.payload.get("status") == "draft" else None
    if not active:
        return {"policy": POLICY, "status": "missing", "message": MISSING, "input": None,
                "components": {}, "scenarios": [], "dcf_result": None, "draft": draft, "id": None, "review_reasons": []}
    reasons = []
    try:
        checked = evaluate(active.payload["input"], security.symbol, security.profile, today=today)
        if checked["base_missing_fields"]:
            reasons.append("股票适用性或资料基本字段尚未明确")
    except (ValueError, KeyError, TypeError):
        checked = {"input": active.payload.get("input", {}), "components": {}, "scenarios": [], "dcf_result": None, "missing_fields": [], "message": MISSING}
        reasons.append("已存输入与当前校验规则或证券口径不一致")
    p = checked["input"]
    target = p.get("target") or {}
    eps_end = (p.get("eps") or {}).get("period_end")
    if eps_end and eps_end <= today.isoformat():
        reasons.append("预测EPS期间已结束，请重新核对")
    if target.get("date") and target["date"] <= today.isoformat():
        reasons.append("目标估值日已到或已过，请重新核对")
    dcf_target = (checked.get("dcf_result") or {}).get("target_date")
    if dcf_target and dcf_target <= today.isoformat():
        reasons.append("DCF目标日已到或已过，请重新核对")
    age = settings.manual_valuation_review_days
    if age is not None and p.get("as_of") and (today - date.fromisoformat(p["as_of"])).days > age:
        reasons.append("资料超过配置的人工复核间隔")
    # 不再读取旧SEC/电话会议或资料日历。新财报是否发布由用户复核更新，不能假装已自动获知。
    reasons = list(dict.fromkeys(reasons))
    complete_eps = checked["components"].get("eps", {}).get("status") == "available"
    if p.get("schema_version") in ("manual-eps-v3", "manual-eps-pe-v4"):
        complete_eps = complete_eps and checked["components"].get("pe", {}).get("status") == "available"
    result = {**checked, "id": active.id, "version": active.fingerprint, "policy": POLICY,
            "confirmed_at": active.payload.get("confirmed_at"), "draft": draft,
            "status": "needs_review" if reasons else "available" if complete_eps else "partial",
            "review_reasons": reasons, "scenarios": [] if reasons else checked["scenarios"],
            "dcf_result": None if reasons else checked["dcf_result"],
            "message": "；".join(reasons) if reasons else checked["message"]}
    from .eps_only import attach
    return attach(result, session, security)


def check(security_id, force=False):
    """编排步骤兼容force参数；force永远不会抓取、推算或重置手工输入。"""
    with Session() as session:
        security = session.get(Security, security_id)
        if security.profile.get("type") == "ETF":
            return {"skipped": "ETF不套用公司EPS/PE手工估值"}
        result = read(session, security)
    warnings = []
    if result["status"] in ("missing", "needs_review"):
        warnings.append({"message": result["message"]})
    else:
        for key, component in result["components"].items():
            if component["status"] != "available":
                warnings.append({"message": f"手工{key}：" + ("EPS非正，倍数估值不适用" if component["status"] == "not_applicable" else "、".join(component["missing_fields"]))})
        warnings.extend({"message":x} for x in (result.get("automatic_pe") or {}).get("warnings") or [])
    return {"cached": True, "policy": POLICY, "status": result["status"], "warnings": warnings}

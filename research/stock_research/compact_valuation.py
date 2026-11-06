"""精简手工估值：预测EPS×历史PE，只计算研究参照，不生成未来价格承诺。"""
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Literal
from urllib.parse import urlsplit
import re

from pydantic import Field, StrictStr, ValidationError, field_validator

from .manual_valuation import InputModel, NAMES, _anniversary
from .store import digest
from .valuation import number

SCHEMA = "manual-valuation-v2"
Basis = Literal["gaap_diluted", "adjusted_diluted", "normalized_diluted"]


class PeriodInput(InputModel):
    period_start: date | None = None
    period_end: date | None = None


class ForecastEPS(PeriodInput):
    period_type: Literal["fy1", "ntm"] | None = None
    basis: Basis | None = None
    low: StrictStr | None = None
    mean: StrictStr | None = None
    high: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=10)


class HistoricalPE(PeriodInput):
    eps_reference: Literal["fy1", "ntm", "ttm"] | None = None
    earnings_basis: Basis | None = None
    statistic: Literal["quartiles", "min_median_max"] | None = None
    low: StrictStr | None = None
    median: StrictStr | None = None
    high: StrictStr | None = None
    note: StrictStr | None = None
    source_ids: list[StrictStr] = Field(default_factory=list, max_length=10)


class CompactSource(InputModel):
    id: StrictStr = Field(min_length=1, max_length=50)
    title: StrictStr = Field(min_length=1, max_length=500)
    url: StrictStr = Field(min_length=1, max_length=8192)
    updated_at: date | None = None

    @field_validator("updated_at", mode="before")
    @classmethod
    def iso_date(cls, value):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
            raise ValueError("来源日期使用YYYY-MM-DD，未知填null")
        return value


class CompactInput(InputModel):
    schema_version: Literal["manual-valuation-v2"]
    symbol: StrictStr
    as_of: date | None = None
    currency: StrictStr
    security_type: Literal["EQUITY"] | None = None
    eps: ForecastEPS = Field(default_factory=ForecastEPS)
    historical_pe: HistoricalPE = Field(default_factory=HistoricalPE)
    sources: list[CompactSource] = Field(default_factory=list, max_length=20)
    notes: list[StrictStr] = Field(default_factory=list, max_length=10)


def evaluate(data, symbol, profile, *, today):
    try:
        model = CompactInput.model_validate(data)
    except ValidationError as exc:
        locations = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)[:8]]
        raise ValueError("精简估值字段、类型或枚举不正确：" + "、".join(locations)) from None
    if model.symbol != symbol or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", model.symbol):
        raise ValueError("JSON股票代码必须与命令一致，使用大写代码")
    if not re.fullmatch(r"[A-Z]{3}", model.currency):
        raise ValueError("估值币种需要三位大写代码")
    if profile.get("currency") and profile["currency"] != model.currency:
        raise ValueError("估值币种与已知证券报价币种冲突")
    if profile.get("type") in ("ETF", "MUTUALFUND"):
        raise ValueError("ETF或基金不适用公司EPS×PE估值")
    p = model.model_dump(mode="json"); base = []; gaps = []; warnings = []
    if model.as_of is None:
        base.append("as_of")
    elif model.as_of > today:
        raise ValueError("查询截止日期不能晚于今天")
    security_type = profile.get("type")
    if not security_type or security_type == "UNKNOWN":
        security_type = model.security_type
    if security_type != "EQUITY":
        base.append("security_type：请明确公司股票适用性")
    sources = {s.id: s for s in model.sources}
    if len(sources) != len(model.sources):
        raise ValueError("来源id不能重复")
    for s in model.sources:
        url = urlsplit(s.url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("来源需要无用户名/口令的HTTPS链接；导入不会访问链接")
        if not s.id.strip() or not s.title.strip():
            raise ValueError("来源id和标题不能为空白")
        if s.updated_at and (s.updated_at > today or (model.as_of and s.updated_at > model.as_of)):
            raise ValueError("来源更新时间不能晚于查询截止日或今天")

    def required(row, keys, path):
        for k in keys:
            if row.get(k) is None or (isinstance(row[k], str) and not row[k].strip()):
                gaps.append(path + "." + k)
        ids = row.get("source_ids") or []
        if not ids:
            gaps.append(path + ".source_ids")
        if len(set(ids)) != len(ids):
            raise ValueError(path + "的来源引用不能重复")
        for sid in ids:
            if sid not in sources:
                raise ValueError(path + "引用了不存在的来源id")

    eps = p["eps"]; pe = p["historical_pe"]
    required(eps, ("period_type", "period_start", "period_end", "basis", "mean"), "eps")
    required(pe, ("period_start", "period_end", "eps_reference", "earnings_basis", "statistic", "low", "median", "high", "note"), "historical_pe")
    if model.eps.period_start and model.eps.period_end:
        if not 330 <= (model.eps.period_end - model.eps.period_start).days + 1 <= 380:
            raise ValueError("EPS必须对应接近完整12个月的期间，不能填单季值")
        if model.as_of and model.eps.period_end <= model.as_of:
            raise ValueError("预测EPS期间必须晚于查询截止日结束")
        if model.eps.period_type == "ntm" and model.as_of and (
                model.eps.period_start != model.as_of + timedelta(days=1) or model.eps.period_end != _anniversary(model.as_of)):
            raise ValueError("NTM期间必须对应查询日之后12个月；财年预测请填fy1")
    if model.eps.period_end and model.eps.period_end <= today:
        gaps.append("eps.period_end：预测期间已结束，请更新后再作为当前估值")
    if model.historical_pe.period_start and model.historical_pe.period_end:
        if model.historical_pe.period_start >= model.historical_pe.period_end:
            raise ValueError("历史PE统计开始日必须早于结束日")
        if model.historical_pe.period_end > today or (model.as_of and model.historical_pe.period_end > model.as_of):
            raise ValueError("历史PE统计期末不能晚于查询截止日或今天")
    if eps["basis"] and pe["earnings_basis"] and eps["basis"] != pe["earnings_basis"]:
        raise ValueError("预测EPS与历史PE盈利口径不一致，不能混用GAAP和调整后盈利")
    if (eps["low"] is None) != (eps["high"] is None):
        gaps.append("eps.low/high：请成对提供范围；只有平均值时两项都填null")
    values = {}
    for prefix, row, keys in (("eps", eps, ("low", "mean", "high")), ("pe", pe, ("low", "median", "high"))):
        for k in keys:
            if row[k] is not None:
                values[prefix + "." + k] = number(row[k], prefix + "." + k)
                if prefix == "pe" and values[prefix + "." + k] <= 0:
                    raise ValueError("历史PE必须为正，无法使用的值请填null并说明")
        middle = "mean" if prefix == "eps" else "median"
        ordered = [values[prefix + "." + k] for k in ("low", middle, "high") if prefix + "." + k in values]
        if ordered != sorted(ordered):
            raise ValueError(prefix + "需要低值≤中值≤高值")
    nonpositive = any(v <= 0 for k, v in values.items() if k.startswith("eps."))
    fallback = eps["low"] is None and eps["high"] is None
    if fallback and eps["mean"] is not None:
        warnings.append("EPS仅有平均预测：三情景共用该值，估值范围只反映PE变化，未补造分析师高低预测")
    comparable = pe["eps_reference"] == eps["period_type"] and pe["eps_reference"] in ("ntm", "fy1")
    if not comparable:
        warnings.append("历史PE与预测EPS期间口径不同：仅显示跨口径倍数假设，不参与技术区间的基本面确认")
    rows = []
    if not base and not gaps and not nonpositive:
        with localcontext() as ctx:
            ctx.prec = 48
            for name, ekey, pkey in zip(NAMES, ("low", "mean", "high"), ("low", "median", "high")):
                e = eps["mean"] if fallback else eps[ekey]
                rows.append({"name":name, "eps":e, "pe":pe[pkey], "price":str(Decimal(e) * Decimal(pe[pkey])),
                             "eps_mean_fallback":fallback and ekey != "mean"})
    missing = list(dict.fromkeys(base + gaps))
    return {"input":p, "input_hash":digest(p), "base_missing_fields":base,
            "components":{"eps":{"status":"missing" if missing else "not_applicable" if nonpositive else "available", "missing_fields":gaps}},
            "missing_fields":missing, "scenarios":rows, "dcf_result":None,
            "reference":{"kind":"forward_pe_reference", "comparable":comparable, "as_of":p["as_of"],
                         "eps_mean_only":fallback, "warnings":warnings},
            "message":"预测EPS×历史PE的前瞻估值参考；不是一年后目标价或未来最高最低价，不是技术支撑压力"}


def render_lines(view):
    """录入、研究及通知共用口径；保持精简，详细结构由JSON另行展示。"""
    from .console_tables import table
    from .research_tables import number as fmt, wrapped
    p = view.get("input") or {}; eps = p.get("eps") or {}; pe = p.get("historical_pe") or {}
    lines = [wrapped(f"前瞻估值参考 · 资料日{p.get('as_of')} · {p.get('currency')}；EPS期间{eps.get('period_start')}～{eps.get('period_end')}（{eps.get('period_type')}，{eps.get('basis')}）"),
             wrapped(f"历史PE：{pe.get('period_start')}～{pe.get('period_end')}；{pe.get('eps_reference')}；{pe.get('statistic')}；{pe.get('earnings_basis')}")]
    if view.get("scenarios"):
        lines.append(table(["情景", "预测EPS", "历史PE参照", "程序计算估值"],
            [[r["name"],fmt(r["eps"],4),fmt(r["pe"]),fmt(r["price"])] for r in view["scenarios"]],numeric=(1,2,3)))
        lines.append("估值区间：" + fmt(view["scenarios"][0]["price"]) + "～" + fmt(view["scenarios"][-1]["price"]) + " " + p["currency"])
    if view.get("components", {}).get("eps", {}).get("status") == "not_applicable":
        lines.append("EPS非正，倍数估值不适用，不生成正估值范围。")
    lines.extend(wrapped("待补：" + x) for x in view.get("missing_fields") or [])
    lines.extend(wrapped(x) for x in (view.get("reference") or {}).get("warnings") or [])
    if pe.get("note"):
        lines.append(wrapped("PE依据：" + pe["note"]))
    lines.extend(wrapped("备注：" + x) for x in p.get("notes") or [])
    lines.extend(wrapped(f"来源[{s['id']}] {s['title']}；更新日{s.get('updated_at') or '未知'}；{s['url']}") for s in p.get("sources") or [])
    lines.append(view.get("message", "情景估值不是未来价格保证，不代表交易许可。"))
    return lines

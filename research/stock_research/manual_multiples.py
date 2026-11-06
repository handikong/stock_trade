"""七字段手工EPS/PE：按固定约定存档，估值使用Decimal，不调用任何PE来源。"""
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal
import json
import re

from pydantic import StrictStr, ValidationError

from .config import settings
from .eps_only import SavedEPS, Values, evaluate as evaluate_eps
from .manual_valuation import InputModel, NAMES, _anniversary
from .store import digest
from .valuation import number

SCHEMA = "manual-eps-pe-v4"
PE_KEYS = {"pe_low", "pe_median", "pe_high", "industry_pe"}
EPS_KEYS = ("eps_conservative", "eps_base", "eps_optimistic")


def policy():
    p = json.loads(Path(settings.manual_input_policy_file).expanduser().read_text())
    if not isinstance(p, dict) or p.get("version") != "manual-input-v1":
        raise ValueError("手工输入约定配置无效")
    if not re.fullmatch(r"[A-Z]{3}", str(p.get("currency", ""))) or p.get("period_type") != "ntm":
        raise ValueError("手工输入约定需明确币种和ntm期间")
    if p.get("earnings_basis") not in ("unspecified", "gaap_diluted", "adjusted_diluted", "normalized_diluted") or p.get("pe_eps_reference") not in ("unspecified", "ntm", "fy1", "ttm"):
        raise ValueError("手工EPS/PE口径配置无效")
    if p.get("pe_statistic") not in ("quartiles", "min_median_max"):
        raise ValueError("手工PE统计约定需要quartiles或min_median_max")
    return p


class Numbers(Values):
    pe_low: StrictStr | None = None
    pe_median: StrictStr | None = None
    pe_high: StrictStr | None = None
    industry_pe: StrictStr | None = None


class PEContext(InputModel):
    eps_reference: Literal["unspecified", "ntm", "fy1", "ttm"]
    statistic: Literal["quartiles", "min_median_max"]
    origin: Literal["manual_research_input"] = "manual_research_input"


class SavedMultiples(SavedEPS):
    schema_version: Literal["manual-eps-pe-v4"]
    values: Numbers
    pe_context: PEContext


def is_simple(data):
    return isinstance(data, dict) and not data.get("schema_version") and bool(PE_KEYS.intersection(data))


def normalize(data, symbol, profile, *, today):
    try:
        values = Numbers.model_validate(data).model_dump(mode="json")
    except ValidationError:
        raise ValueError("手工JSON只接受三档EPS、pe_low/pe_median/pe_high及可选industry_pe；数值用字符串或null") from None
    c = policy()
    return {"schema_version":SCHEMA, "symbol":symbol, "as_of":today.isoformat(),
            "currency":profile.get("currency") or c["currency"], "values":values,
            "eps":{"period_type":"ntm", "period_start":(today + timedelta(days=1)).isoformat(),
                   "period_end":_anniversary(today).isoformat(), "basis":c["earnings_basis"], "origin":"user_research_scenarios"},
            "pe_context":{"eps_reference":c["pe_eps_reference"], "statistic":c["pe_statistic"], "origin":"manual_research_input"}}


def evaluate(data, symbol, profile, *, today):
    try:
        model = SavedMultiples.model_validate(data)
    except ValidationError as exc:
        fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)[:8]]
        raise ValueError("手工EPS/PE档案格式无效：" + "、".join(fields)) from None
    p = model.model_dump(mode="json"); values = p["values"]
    # 共用EPS日期、证券、金额与顺序校验，不另写相互分歧的规则。
    eps_input = {k:v for k,v in p.items() if k != "pe_context"}
    eps_input.update(schema_version="manual-eps-v3", values={k:values[k] for k in EPS_KEYS})
    result = evaluate_eps(eps_input, symbol, profile, today=today)
    pe_gaps = [k for k in ("pe_low", "pe_median", "pe_high") if values[k] is None]
    pe_values = []
    for k in ("pe_low", "pe_median", "pe_high", "industry_pe"):
        if values[k] is not None:
            value = number(values[k], k)
            if value <= 0:
                raise ValueError("PE必须为正，缺值请填null，不能用0代替")
            if k != "industry_pe":
                pe_values.append(value)
    if pe_values != sorted(pe_values):
        raise ValueError("PE需要低位≤中位≤高位")
    result.update(input=p, input_hash=digest(p), valuation_sets=[], scenarios=[], dcf_result=None)
    result["components"]["pe"] = {"status":"missing" if pe_gaps else "available", "missing_fields":pe_gaps}
    # 行业参照是可选比较，不作为缺项，也不是第二个独立估值方法。
    result["missing_fields"] = list(dict.fromkeys(result["missing_fields"] + pe_gaps))
    if result["components"]["eps"]["status"] == "available":
        with localcontext() as ctx:
            ctx.prec = 48
            for kind, multiples in (("history", [values[k] for k in ("pe_low", "pe_median", "pe_high")]),
                                    ("industry", [values["industry_pe"]] * 3)):
                if any(x is None for x in multiples):
                    continue
                rows = [{"name":n,"eps":values[e],"pe":pe,"price":str(Decimal(values[e]) * Decimal(pe))}
                        for n,e,pe in zip(NAMES, EPS_KEYS, multiples)]
                result["valuation_sets"].append({"kind":kind,"label":"手工历史PE估值" if kind=="history" else "手工行业PE比较估值", "scenarios":rows})
                if kind == "history":
                    result["scenarios"] = rows
    comparable = model.pe_context.eps_reference == "ntm" and model.eps.basis != "unspecified"
    result["reference"] = {"kind":"manual_pe_reference", "comparable":comparable,
        "warnings":[] if comparable else ["简式输入未说明完整来源及口径；价格按手工倍数假设计算，技术强弱独立保留"]}
    result["message"] = "EPS×PE前瞻估值参考，手工数值及来源由本人核对；不是未来一年最高最低价或交易许可"
    return result


def render_lines(view):
    from .console_tables import table
    from .research_tables import number as fmt, wrapped
    p = view["input"]; v = p["values"]; e = p["eps"]
    lines = [wrapped(f"手工EPS＋PE · {p['symbol']} · {p['currency']}；录入日{p['as_of']}；固定NTM约定{e['period_start']}～{e['period_end']}")]
    if not view.get("scenarios"):
        lines.append(table(["情景","EPS","历史PE参照"], [[n,fmt(v[k],4),fmt(v[pk])] for n,k,pk in zip(NAMES,EPS_KEYS,("pe_low","pe_median","pe_high"))],numeric=(1,2)))
    for group in view.get("valuation_sets") or []:
        lines.append(group["label"])
        lines.append(table(["情景","EPS","PE","计算估值"], [[r["name"],fmt(r["eps"],4),fmt(r["pe"]),fmt(r["price"])] for r in group["scenarios"]],numeric=(1,2,3)))
    if view.get("components", {}).get("eps", {}).get("status") == "not_applicable":
        lines.append("EPS存在非正情景，倍数估值不适用。")
    lines.extend(wrapped("待补：" + x) for x in view.get("missing_fields") or [])
    lines.extend(wrapped(x) for x in (view.get("reference") or {}).get("warnings") or [])
    lines.append(view["message"])
    return lines

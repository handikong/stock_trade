"""旧三档EPS兼容入口：保留原输入，PE改为手工提供，不回退自动来源。"""
from datetime import date, timedelta
from typing import Literal
import re

from pydantic import StrictStr, ValidationError

from .manual_valuation import InputModel, NAMES, _anniversary
from .store import digest
from .valuation import number

SCHEMA = "manual-eps-v3"
KEYS = {"eps_conservative", "eps_base", "eps_optimistic"}


class Values(InputModel):
    eps_conservative: StrictStr | None
    eps_base: StrictStr | None
    eps_optimistic: StrictStr | None


class EPSContext(InputModel):
    period_type: Literal["ntm"]
    period_start: date
    period_end: date
    basis: Literal["unspecified", "gaap_diluted", "adjusted_diluted", "normalized_diluted"]
    origin: Literal["user_research_scenarios"]


class SavedEPS(InputModel):
    schema_version: Literal["manual-eps-v3"]
    symbol: StrictStr
    as_of: date
    currency: StrictStr
    values: Values
    eps: EPSContext


def is_simple(data):
    return isinstance(data, dict) and not data.get("schema_version") and bool(KEYS.intersection(data))


def normalize(data, symbol, profile, *, today):
    from .manual_multiples import policy
    # 原始三值只用于本次导入，保存时封装固定日期；后续读取不得再次滚动一年。
    try:
        values = Values.model_validate(data).model_dump(mode="json")
    except ValidationError:
        raise ValueError("仅支持eps_conservative、eps_base、eps_optimistic三个字段；数值用字符串，缺值用null") from None
    convention = policy()
    currency = profile.get("currency") or convention["currency"]
    return {"schema_version":SCHEMA, "symbol":symbol, "as_of":today.isoformat(), "currency":currency, "values":values,
            "eps":{"period_type":"ntm", "period_start":(today + timedelta(days=1)).isoformat(),
                   "period_end":_anniversary(today).isoformat(), "basis":convention["earnings_basis"], "origin":"user_research_scenarios"}}


def evaluate(data, symbol, profile, *, today):
    try:
        model = SavedEPS.model_validate(data)
    except ValidationError as exc:
        fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False)[:8]]
        raise ValueError("系统EPS档案格式无效：" + "、".join(fields)) from None
    if model.symbol != symbol or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
        raise ValueError("EPS档案股票代码与当前股票不一致")
    if not re.fullmatch(r"[A-Z]{3}", model.currency) or (profile.get("currency") and profile["currency"] != model.currency):
        raise ValueError("EPS币种无效或与证券报价币种冲突")
    if profile.get("type") in ("ETF", "MUTUALFUND"):
        raise ValueError("基金不适用公司EPS倍数估值")
    if model.as_of > today or model.eps.period_start != model.as_of + timedelta(days=1) or model.eps.period_end != _anniversary(model.as_of):
        raise ValueError("EPS档案日期无效，不能把财年或旧预测移动为新NTM")
    p = model.model_dump(mode="json"); gaps = []; base = []
    if profile.get("type") != "EQUITY":
        base.append("security_type：需先由行情响应确认普通股票，不把未知证券默认为公司股票")
    vals = []
    for k in ("eps_conservative", "eps_base", "eps_optimistic"):
        if p["values"][k] is None:
            gaps.append(k)
        else:
            vals.append(number(p["values"][k], k))
    if vals != sorted(vals):
        raise ValueError("EPS需要保守值≤基准值≤乐观值")
    if model.eps.period_end <= today:
        gaps.append("预测EPS期间已结束，请重新录入")
    status = "missing" if base or gaps else "not_applicable" if any(v <= 0 for v in vals) else "available"
    return {"input":p, "input_hash":digest(p), "base_missing_fields":base,
            "components":{"eps":{"status":status,"missing_fields":gaps}}, "missing_fields":base + gaps,
            "scenarios":[], "dcf_result":None, "valuation_sets":[],
            "reference":{"kind":"manual_pe_required", "comparable":False},
            "message":"旧三值格式只录入EPS；请补充手工PE，不再自动获取或读取旧自动PE补齐"}


def attach(view, session, security):
    """旧EPS-only档案只补缺项说明，不读取自动PE缓存。"""
    if (view.get("input") or {}).get("schema_version") != SCHEMA:
        return view
    # 用户已改为手工PE：旧来源缓存只留档，不在任何读取路径补齐当前估值。
    result = {**view, "scenarios":[], "valuation_sets":[],
              "components":{**view.get("components", {}), "pe":{"status":"missing", "missing_fields":["请录入pe_low、pe_median、pe_high"]}}}
    result["missing_fields"] = list(dict.fromkeys([*(view.get("missing_fields") or []), "手工PE未录入"]))
    result["reference"] = {"kind":"manual_pe_required", "comparable":False, "warnings":[]}
    return result


def render_lines(view):
    from .console_tables import table
    from .research_tables import number as fmt, wrapped
    p = view["input"]; e = p["eps"]
    lines = [wrapped(f"三档EPS研究情景 · {p['symbol']} · {p['currency']} · 录入日{p['as_of']}；NTM约定期间{e['period_start']}～{e['period_end']}；盈利口径{e['basis']}"),
             table(["保守EPS", "基准EPS", "乐观EPS"], [[fmt(p["values"][k],4) for k in ("eps_conservative","eps_base","eps_optimistic")]], numeric=(0,1,2))]
    for part in view.get("valuation_sets") or []:
        lines.append(part["label"])
        pe = part["pe"]
        if part["kind"]=="history":
            lines.append(wrapped(f"{pe['metric']} PE；请求{pe['years']}年窗口，实际样本{pe['actual_start']}～{pe['actual_end']}，{pe['count']}个季度样本；分位25/50/75；" + ("历史窗口未完整覆盖" if pe["partial_window"] else "覆盖起点符合窗口容差")))
        else:
            lines.append(wrapped(f"行业代理{pe['industry']}；{pe['firms']}家公司；{pe['metric']} PE；资料月份{pe['data_month']}；{pe['mapping_note']}；{pe['definition_note']}"))
        lines.append(table(["情景","EPS","PE","计算估值"], [[r["name"],fmt(r["eps"],4),fmt(r["pe"]),fmt(r["price"])] for r in part["scenarios"]], numeric=(1,2,3)))
        lines.append(wrapped("PE来源：" + pe["source_url"]))
    if not view.get("valuation_sets"):
        lines.append("当前只有EPS，请改用EPS＋PE七字段JSON重新录入；查询不会自动获取PE。")
    lines.extend(wrapped("待补：" + x) for x in view.get("missing_fields") or [])
    lines.extend(wrapped(x) for x in (view.get("automatic_pe") or {}).get("warnings") or [])
    lines.extend(wrapped(x) for x in (view.get("reference") or {}).get("warnings") or [])
    lines.append(view["message"])
    return lines

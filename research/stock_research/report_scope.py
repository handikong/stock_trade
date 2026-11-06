"""按财报期间限制研究范围；两期财报不等于任意两份SEC披露。"""
from .config import settings

PERIODIC_FORMS = {"10-Q", "10-K", "20-F", "40-F"}


def scope_config() -> dict:
    return {"version":"recent-report-periods-v2", "report_count":settings.financial_report_count}


def fact_periods(facts: list[dict]) -> list[str]:
    # 先依据经营业绩的期间定位，不能用单个滞后的余额指标另补旧期。
    ends = {f["end"] for f in facts if f.get("end") and f.get("period_type") != "instant"}
    if not ends:
        ends = {f["end"] for f in facts if f.get("end")}
    return sorted(ends, reverse=True)[:settings.financial_report_count]


def scoped_facts(facts: list[dict], periods: list[str] | None = None) -> list[dict]:
    selected = set(fact_periods(facts) if periods is None else periods)
    return sorted((f for f in facts if f.get("end") in selected),
        key=lambda f:(f["end"], f.get("filed") or ""), reverse=True)


def recent_filings(recent: dict) -> list[dict]:
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        if form.removesuffix("/A") not in PERIODIC_FORMS:
            continue
        period = recent.get("reportDate", [""] * len(recent["form"]))[i]
        if not period:
            continue
        rows.append({"form":form, "accession":recent["accessionNumber"][i],
            "filed":recent["filingDate"][i], "period_end":period,
            "accepted_at":recent.get("acceptanceDateTime", [""] * len(recent["form"]))[i],
            "primary_document":recent["primaryDocument"][i]})
    periods = sorted({r["period_end"] for r in rows}, reverse=True)[:settings.financial_report_count]
    selected = []
    for period in periods:
        versions = sorted((r for r in rows if r["period_end"] == period),
            key=lambda r:(r["filed"], r["accepted_at"], r["accession"]), reverse=True)
        # 若存在修订，同时保留原报告与最新修订，仍算同一期而非两期。
        base = next((r for r in versions if not r["form"].endswith("/A")), None)
        amendment = next((r for r in versions if r["form"].endswith("/A")), None)
        if base:
            selected.append(base)
        if amendment and (not base or (amendment["filed"], amendment["accepted_at"]) >= (base["filed"], base["accepted_at"])):
            selected.append(amendment)
    return selected


def default_documents(documents, periods: list[str] | None = None):
    """默认研究只用选中财报期；既有8-K封面和旧报告仍保留在原文档案。"""
    if periods is None:
        periods = sorted({d.period_end for d in documents if d.period_end
            and d.metadata_json.get("form", "").removesuffix("/A") in PERIODIC_FORMS},
            reverse=True)[:settings.financial_report_count]
    result = []
    for doc in documents:
        form = doc.metadata_json.get("form")
        if doc.kind == "filing" and form:
            if form.removesuffix("/A") not in PERIODIC_FORMS or doc.period_end not in periods:
                continue
        if doc.kind in ("call","release","presentation") and doc.period_end and doc.period_end not in periods:
            continue
        result.append(doc)
    return result

"""研究编排：行情、技术与可更正的每周估值。"""
import logging
import re
from collections.abc import Callable
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from . import fundamental_technical, market, technical, weekly_valuation
from .financial_reports import run_steps
from .documents import document_summary
from .store import Document, Job, Security, Session, Snapshot, latest, serialize_snapshot

logger = logging.getLogger(__name__)
RESEARCH_POLICY = "weekly-valuation-technical-v1"


def security_for(symbol: str, create: bool = False) -> Security:
    symbol = symbol.strip().upper().removeprefix("$")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
        raise ValueError("请输入有效美股代码，例如AAPL、MSFT或BRK-B")
    with Session() as session:
        item = session.scalar(select(Security).where(Security.symbol == symbol))
        if not item and create:
            item = Security(symbol=symbol, profile={})
            session.add(item)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                item = session.scalar(select(Security).where(Security.symbol == symbol))
                if not item:
                    raise
        if not item:
            raise LookupError("尚无该股票档案，请先执行一次普通查询或导入手工资料")
        return item


def job_json(job):
    return {"id":job.id,"kind":job.kind,"status":job.status,"progress":job.progress,
        "message":job.message,"result":job.result,"created_at":job.created_at,"updated_at":job.updated_at}


def refresh_sources(security_id: str, force: bool = False,
                    on_progress: Callable[[int, str], None] | None = None, *, analyze_reports=False) -> dict:
    # 日常研究刷新行情；估值采集由周六任务统一处理。
    return run_steps(security_id, force, [
        (10, "正在读取行情并计算技术支撑压力", "market", market.refresh_market),
    ], on_progress)


def build_report(security: Security, *, include_history=False) -> dict:
    with Session() as session:
        security = session.get(Security, security.id)
        market_snapshot = latest(session, security.id, "market")
        automatic = (None if security.profile.get("type") in ("ETF","MUTUALFUND")
                     else weekly_valuation.read(session,security.id))
        market_view = None if market_snapshot is None else {
            k:v for k,v in serialize_snapshot(market_snapshot).items() if k not in ("bars", "raw_file")}
        if market_view and market_view.get("technical"):
            market_view["technical"]["stale"] = not technical.is_current(market_view["technical"])
        cache = market.cache_state(market_snapshot)
        combined = fundamental_technical.evaluate(market_view, cache, None, security.profile, weekly=automatic or {})
        # 普通查询不读取旧财报/电话会议正文，不扫描旧分析，不把旧结果回填为当前数据。
        keys = ("financials", "filings", "estimates", "earnings", "preview", "guidance", "analysis", "discovery",
                "earnings_calendar", "eps_revisions", "eps_trend", "materials", "guidance_draft", "valuation_draft", "external_briefs")
        payload = {key:None for key in keys}
        history = {}; docs = []; valuations = []
        if include_history:
            for key in keys:
                item = serialize_snapshot(latest(session, security.id, key))
                if item:
                    history[key] = {**item, "historical_only":True}
            docs = list(session.scalars(select(Document).where(Document.security_id == security.id)
                .order_by(Document.acquired_at.desc())))
            valuations = [{**serialize_snapshot(v), "historical_only":True} for v in session.scalars(
                select(Snapshot).where(Snapshot.security_id == security.id, Snapshot.kind == "valuation")
                .order_by(Snapshot.created_at.desc()).limit(20))]
        jobs = list(session.scalars(select(Job).where(Job.security_id == security.id)
            .order_by(Job.created_at.desc()).limit(8)))
        data_status = {
            "market":{"status":"available" if market_snapshot else "missing", "message":"日线及技术支撑压力仍由程序获取和计算，新鲜度见cache"},
            "valuation":{"status":"not_applicable" if automatic is None else "available" if automatic["scenarios"] else "missing",
                         "message":automatic["message"] if automatic else "基金不适用公司EPS估值"},
            "fundamental_technical":{"status":combined["status"], "message":combined["message"]},
        }
        for key in ("financials", "estimates", "calls", "guidance", "preview", "calendar", "analysis", "external_briefs"):
            data_status[key] = {"status":"disabled", "message":"当前研究使用每周估值和日线技术数据"}
        return {"symbol":security.symbol, "security_id":security.id, "profile":security.profile,
            "research_policy":RESEARCH_POLICY, "analysis_policy":"weekly-valuation-no-llm-v1",
            "cache":cache, "market":market_view, **payload,
            "fundamental_technical":combined,
            "automatic_valuation_reference":automatic,
            "valuation_draft_history":history.get("valuation_draft"),
            "legacy_history":history, "valuations":valuations, "documents":[document_summary(d) for d in docs],
            "jobs":[job_json(j) for j in jobs], "data_status":data_status,
            "coverage":{"calls":0,"call_periods":[],"missing_call_periods":[],
                        "archived_documents":len(docs),"calls_required":False,"frozen_preview":False,
                        "official_financials":False,"legacy_history_included":include_history},
            "research_scope":{"mode":"weekly_valuation", "report_periods":[], "selected_document_ids":[],
                "message":"估值读取每周采集与网页更正后的当前版本"}}

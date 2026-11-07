from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from urllib.parse import urlparse
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictStr
from sqlalchemy import select
from .config import ROOT, settings
from .documents import archive
from .earnings import freeze_preview, record_event, record_guidance
from .jobs import Worker, queue
from .service import build_report, job_json, security_for as find_security
from .store import Document, Job, Security, Session, Snapshot, initialize, save_snapshot, serialize_snapshot
from . import intraday, portfolio, push_history, weekly_valuation, workspace


worker=Worker()


@asynccontextmanager
async def lifespan(app):
    from .cli import configure_console_logging
    configure_console_logging()
    initialize()
    settings.averages  # 仅在用户启动服务时校验配置，不在开发阶段自动运行。
    worker.start()
    intraday.monitor.start()
    try:
        yield
    finally:
        intraday.monitor.shutdown()
        worker.shutdown()


app=FastAPI(title="个股研究",version="0.1.0",lifespan=lifespan,docs_url="/api/docs",openapi_url="/api/openapi.json")


@app.middleware("http")
async def access(request: Request, call_next):
    if request.url.path.startswith("/api"):
        origin=request.headers.get("origin")
        if origin and urlparse(origin).netloc!=request.headers.get("host"):
            return JSONResponse({"detail":"只接受同一站点发起的请求"},status_code=403)
    response=await call_next(request)
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Referrer-Policy"]="same-origin"
    response.headers["X-Frame-Options"]="DENY"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request,exc):
    # 不回显密钥、上传正文或完整请求体。
    return JSONResponse({"detail":"填写内容不完整或格式不正确，请核对字段与数值范围"},status_code=422)


@app.exception_handler(ValueError)
async def domain_error(request,exc):
    return JSONResponse({"detail":str(exc)},status_code=422)


def security_for(symbol: str,create=False):
    try:
        return find_security(symbol,create)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


class Query(BaseModel):
    symbol: str = Field(min_length=1,max_length=15)


class PositionInput(BaseModel):
    model_config = {"extra":"forbid"}
    symbol: str = Field(min_length=1,max_length=15)
    name: str = Field(default="",max_length=100)
    quantity: StrictStr = Field(min_length=1,max_length=60)
    average_cost: StrictStr = Field(min_length=1,max_length=60)
    notes: str = Field(default="",max_length=1000)
    expected_version: str | None = Field(default=None,min_length=32,max_length=32)
    tactical: StrictBool | None = None


class PortfolioAccountInput(BaseModel):
    model_config = {"extra":"forbid"}
    principal: StrictStr = Field(min_length=1,max_length=35)
    total_cash: StrictStr = Field(min_length=1,max_length=35)
    available_cash: StrictStr = Field(min_length=1,max_length=35)
    net_flows: StrictStr = Field(default="0",max_length=35)
    loss_percent: StrictStr = Field(default="5",max_length=20)
    expected_version: str | None = Field(default=None,min_length=32,max_length=32)


class ManualPlanInput(BaseModel):
    model_config = {"extra":"forbid"}
    quantity: int = Field(ge=1,le=1_000_000,strict=True)
    price: StrictStr = Field(min_length=1,max_length=35)
    stop: StrictStr = Field(min_length=1,max_length=35)
    reason: StrictStr = Field(min_length=1,max_length=240)
    tactical: bool = Field(default=False,strict=True)


class PortfolioEvaluation(Query):
    model_config = {"extra":"forbid"}
    manual: ManualPlanInput | None = None


class PortfolioPlanInput(PortfolioEvaluation):
    expected_version: str = Field(min_length=64,max_length=64)


class PlanVersion(BaseModel):
    model_config = {"extra":"forbid"}
    expected_version: str = Field(min_length=32,max_length=32)


class PortfolioFillInput(PlanVersion):
    stage: Literal["entry","left","right","manual"]
    quantity: int = Field(ge=1,le=1_000_000,strict=True)
    price: StrictStr = Field(min_length=1,max_length=35)
    fee: StrictStr = Field(default="0",max_length=35)
    execution_session: date
    cash_already_recorded: bool = Field(default=False,strict=True)
    reason: StrictStr = Field(default="",max_length=240)


class PoolScan(BaseModel):
    model_config = {"extra":"forbid"}
    refresh: bool = Field(default=False,strict=True)


@app.exception_handler(workspace.Conflict)
async def workspace_conflict(request,exc):
    return JSONResponse({"detail":str(exc)},status_code=409)


@app.get("/api/v1/workspace")
def workspace_overview():
    return {"market":"US","markets":[{"id":"US","name":"美股","currency":"USD","enabled":True},
            {"id":"CN","name":"A股","currency":"CNY","enabled":False}],
            "holdings":workspace.holdings(),"watchlist":workspace.pool()}


@app.get("/api/v1/portfolio")
def portfolio_overview():
    return portfolio.overview()


@app.get("/api/v1/intraday")
def intraday_overview():
    return intraday.monitor.read()


@app.put("/api/v1/portfolio/account")
def portfolio_account(p: PortfolioAccountInput):
    return portfolio.save_account(p.model_dump())


@app.post("/api/v1/portfolio/evaluate")
def portfolio_evaluate(p: PortfolioEvaluation):
    return portfolio.evaluate(p.symbol,p.manual.model_dump() if p.manual else None)


@app.post("/api/v1/portfolio/plans")
def portfolio_plan(p: PortfolioPlanInput):
    return portfolio.create_plan(p.symbol,p.expected_version,p.manual.model_dump() if p.manual else None)


@app.get("/api/v1/portfolio/plans/{plan_id}/check")
def portfolio_plan_check(plan_id: str,stage: Literal["entry","left","right","manual"]):
    try:
        return portfolio.check_plan(plan_id,stage)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@app.post("/api/v1/portfolio/plans/{plan_id}/cancel")
def portfolio_plan_cancel(plan_id: str,p: PlanVersion):
    try:
        return portfolio.cancel_plan(plan_id,p.expected_version)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@app.post("/api/v1/portfolio/plans/{plan_id}/fills")
def portfolio_plan_fill(plan_id: str,p: PortfolioFillInput):
    try:
        return portfolio.record_fill(plan_id,p.model_dump())
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@app.post("/api/v1/workspace/research")
def workspace_open_research(p: Query):
    item=security_for(workspace.symbol(p.symbol),create=True)
    return {"symbol":item.symbol,"id":item.id}


@app.post("/api/v1/holdings")
def position_add(p: PositionInput):
    return workspace.save_position(p.model_dump())


@app.post("/api/v1/holdings/refresh")
def positions_refresh():
    positions=workspace.holdings()["items"]
    if not positions:
        raise ValueError("尚无持仓，请先添加股票")
    anchor=security_for(positions[0]["symbol"])
    return {"job_id":queue(anchor.id,"holdings_refresh",{})}


@app.put("/api/v1/holdings/{position_id}")
def position_edit(position_id: str,p: PositionInput):
    try:
        return workspace.save_position(p.model_dump(),position_id)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@app.delete("/api/v1/holdings/{position_id}")
def position_remove(position_id: str,version: str):
    try:
        return workspace.remove_position(position_id,version)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@app.get("/api/v1/watchlist")
def workspace_watchlist():
    return workspace.pool()


@app.post("/api/v1/watchlist")
def pool_add(p: Query):
    return workspace.change_pool(p.symbol,True)


@app.delete("/api/v1/watchlist/{symbol}")
def pool_remove(symbol: str):
    return workspace.change_pool(symbol,False)


@app.post("/api/v1/watchlist/scan")
def pool_scan(p: PoolScan):
    members=workspace.pool()["items"]
    if not members:
        raise ValueError("扫描名单为空，请先添加股票")
    for member in members:
        workspace.symbol(member["symbol"])
    anchor=security_for(members[0]["symbol"],create=True)
    return {"job_id":queue(anchor.id,"watchlist_scan",p.model_dump())}


@app.get("/api/v1/workspace/jobs")
def workspace_jobs():
    with Session() as session:
        return [{k:v for k,v in job_json(j).items() if k!="result"} for j in session.scalars(select(Job).where(Job.kind.in_(("watchlist_scan","holdings_refresh")))
            .order_by(Job.created_at.desc()).limit(5))]


@app.get("/api/v1/schedule")
def schedule_status():
    from . import post_close
    return post_close.status()


@app.get("/api/v1/push-history")
def push_history_list(category: push_history.Category | None = None, day: date | None = None,
                      limit: int = 50, cursor: str | None = None):
    return push_history.listing(category=category, day=day, limit=limit, cursor=cursor)


class Refresh(BaseModel):
    force: bool = False


class AnalyzeRequest(BaseModel):
    document_ids: list[str] | None = Field(default=None,max_length=100)


class Material(BaseModel):
    kind: Literal["call","filing","release","presentation"]
    title: str = Field(min_length=1,max_length=500)
    url: str = Field(default="",max_length=8192)
    text: str = Field(default="",max_length=1_000_000)
    period_end: str = ""
    published_at: str = ""


class Event(BaseModel):
    period_end: str
    scheduled_at: str
    confirmed: bool = False
    source_url: str = Field(min_length=1,max_length=8192)
    questions: str = Field(default="",max_length=5000)


class Guidance(BaseModel):
    metric: Literal["EPS","revenue","operating_margin","capex"]
    period_end: str
    basis: str = Field(min_length=1,max_length=100)
    unit: str = Field(min_length=1,max_length=100)
    low: str
    high: str
    evidence_id: str = Field(min_length=1,max_length=150)
    source_note: str = Field(min_length=1,max_length=2000)


@app.get("/api/v1/config")
def configuration():
    return {"deepseek_configured":bool(settings.deepseek_api_key.get_secret_value()),
        "deepseek_model":settings.deepseek_model,"sec_configured":bool(settings.sec_user_agent),
        "database":"SQLite本地开发" if settings.database.startswith("sqlite") else "PostgreSQL",
        "history_years":settings.history_years,"market_cache_hours":settings.market_cache_hours,
        "financial_report_count":settings.financial_report_count,"identity_lookup":False,
        "analysis_policy":"manual-input-no-llm-v1","research_policy":"manual-eps-pe-technical-v4",
        "pe_policy":"仅手工录入，不自动获取或回退旧自动PE",
        "automatic_financial_sources_enabled":False,"original_analysis_enabled":False,"financial_calendar_connected":False,
        "fmp_configured":bool(settings.fmp_api_key.get_secret_value()),"yahoo_cooldown_seconds":settings.yahoo_cooldown_seconds,
        "display":"technical_results_only","display_timezone":settings.research_display_timezone,
        "workspace_market":"US","execution_mode":"研究，不提供账户交易许可"}


@app.post("/api/v1/research")
def start_research(p: Query):
    item=security_for(p.symbol,create=True)
    return {"symbol":item.symbol,"id":item.id}


@app.get("/api/v1/research")
def list_research():
    with Session() as session:
        return [{"symbol":s.symbol,"name":s.profile.get("name",s.symbol)} for s in session.scalars(select(Security).order_by(Security.symbol))]


@app.get("/api/v1/research/{symbol}")
def research(symbol: str):
    return build_report(security_for(symbol))


@app.post("/api/v1/research/{symbol}/refresh")
def refresh(symbol: str,p: Refresh):
    security=security_for(symbol)
    return {"job_id":queue(security.id,"refresh",p.model_dump())}


@app.post("/api/v1/research/{symbol}/analyze")
def analyze(symbol: str,p: AnalyzeRequest):
    raise HTTPException(410,"旧模型分析已停用，当前估值由每周任务更新")


@app.post("/api/v1/research/{symbol}/discover")
def discover(symbol: str):
    raise HTTPException(410,"自动搜索外部研究资料已停用，请手工录入")


@app.get("/api/v1/jobs/{job_id}")
def job_status(job_id: str):
    with Session() as session:
        job=session.get(Job,job_id)
        if not job:
            raise HTTPException(404,"任务不存在")
        return job_json(job)


@app.post("/api/v1/research/{symbol}/documents")
def add_document(symbol: str,p: Material):
    security=security_for(symbol)
    if p.url and not p.text:
        return {"job_id":queue(security.id,"document",p.model_dump())}
    if not p.text.strip():
        raise ValueError("请填写公开资料链接或原文内容")
    return {"document":archive(security.id,p.kind,p.title,p.text.encode(),"import.txt",
        source_url=p.url,published_at=p.published_at,period_end=p.period_end,metadata={"origin":"用户文本导入"})}


@app.post("/api/v1/research/{symbol}/upload")
async def upload(symbol: str,file: UploadFile=File(...),kind: str=Form(...),title: str=Form(...),period_end: str=Form("")):
    security=security_for(symbol)
    if kind not in ("call","filing","release","presentation"):
        raise ValueError("请选择有效资料类型")
    if not title.strip():
        raise ValueError("请填写资料标题")
    raw=await file.read(12_000_001)
    if len(raw)>12_000_000:
        raise ValueError("资料文件不能超过12MB")
    return {"document":archive(security.id,kind,title,raw,file.filename or "import.txt",
        period_end=period_end,content_type=file.content_type or "text/plain",metadata={"origin":"用户文件导入"})}


@app.get("/api/v1/documents/{doc_id}")
def document(doc_id: str):
    with Session() as session:
        doc=session.get(Document,doc_id)
        if not doc:
            raise HTTPException(404,"资料不存在")
        return {**document_summary(doc),"segments":doc.segments}


@app.get("/api/v1/documents/{doc_id}/original")
def original(doc_id: str):
    with Session() as session:
        doc=session.get(Document,doc_id)
        if not doc:
            raise HTTPException(404,"资料不存在")
        path=settings.storage/"documents"/doc.file_name
        if not path.is_file():
            raise HTTPException(404,"原始文件未找到，请从备份恢复")
        return FileResponse(path,filename=doc.file_name,media_type="application/octet-stream")


@app.post("/api/v1/research/{symbol}/earnings")
def event(symbol: str,p: Event):
    raise HTTPException(410,"旧财报事件流程已停用，估值期间可在个股研究页更正")


@app.post("/api/v1/research/{symbol}/preview/freeze")
def freeze(symbol: str):
    raise HTTPException(410,"旧预期冻结流程已停用，不读取旧财报或预期补值")


@app.post("/api/v1/research/{symbol}/guidance")
def guidance(symbol: str,p: Guidance):
    raise HTTPException(410,"旧原文指引确认流程已停用")


@app.post("/api/v1/research/{symbol}/valuations")
@app.post("/api/v1/research/{symbol}/manual-valuations/preview")
@app.post("/api/v1/research/{symbol}/manual-valuations")
def retired_valuation(symbol: str):
    raise HTTPException(410,"旧手工估值入口已停用，请在个股研究页点击更正估值")


class ValuationCorrection(BaseModel):
    model_config = {"extra":"forbid"}
    expected_id: str | None = Field(min_length=32,max_length=32)
    changes: dict[str, StrictStr | None] = Field(max_length=7)


@app.post("/api/v1/research/{symbol}/valuation-correction")
def correct_valuation(symbol: str, p: ValuationCorrection):
    try:
        return weekly_valuation.correct(security_for(symbol),p.changes,p.expected_id)
    except weekly_valuation.Conflict as exc:
        raise HTTPException(409,str(exc)) from exc


@app.post("/api/v1/research/{symbol}/reports")
def save_report(symbol: str):
    security=security_for(symbol)
    content=research(symbol)
    content.pop("jobs",None)
    with Session() as session:
        item=save_snapshot(session,security.id,"report",{"research":content,
            "message":"这是保存时的研究快照；AI结论和估值假设仍保持草稿/情景标签"})
        session.commit()
        return {"id":item.id}


@app.get("/api/v1/reports/{report_id}")
def saved_report(report_id: str):
    with Session() as session:
        item=session.get(Snapshot,report_id)
        if not item or item.kind!="report":
            raise HTTPException(404,"研究快照不存在")
        return serialize_snapshot(item)


static=ROOT/"gateway"/"static"
if static.is_dir():
    app.mount("/",StaticFiles(directory=static,html=True),name="研究界面")

"""系统内盘后两阶段计划任务：北京时间更新日线，再用完整缓存扫描买点并通知。"""
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import market, watchlist
from .config import settings
from .cli_helpers import storage_ready
from .providers import failure_detail
from .service import security_for
from .store import Job, Session, WatchlistEntry, digest, latest, stamp

logger = logging.getLogger(__name__)
KINDS = {"heat":"scheduled_futu_heat","market":"scheduled_market","signals":"scheduled_signals"}


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    version: str
    enabled: bool
    timezone: str
    start_date: str
    market_time: str
    signal_time: str
    catch_up_until: str
    poll_seconds: int = Field(ge=5,le=300)
    volume_top50_enabled: bool = False
    futu_heat_enabled: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.version!="post-close-schedule-v1":
            raise ValueError("盘后计划任务配置版本无效")
        ZoneInfo(self.timezone)
        if date.fromisoformat(self.start_date).isoformat()!=self.start_date:
            raise ValueError("计划开始日期需要YYYY-MM-DD格式")
        values = (self.market_time,self.signal_time,self.catch_up_until)
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v) for v in values):
            raise ValueError("计划时间需要HH:MM格式")
        if not self.market_time < self.signal_time < self.catch_up_until:
            raise ValueError("需要满足行情时间＜策略时间＜当日补跑截止时间")
        if self.futu_heat_enabled and self.volume_top50_enabled:
            raise ValueError("富途热榜与旧 Yahoo 成交量榜不能同时扩充关注池")
        return self


def policy():
    try:
        return Policy.model_validate(json.loads(Path(settings.post_close_schedule_file).expanduser().read_text()))
    except (ValueError,OSError,KeyError) as exc:
        raise ValueError("盘后任务配置无效，请核对POST_CLOSE_SCHEDULE_FILE和时间/时区") from exc


def _local(cfg):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(cfg.timezone))


def _target(cfg):
    now = _local(cfg)
    if not cfg.enabled:
        return now,None,"计划任务已停用"
    if now.date().isoformat()<cfg.start_date:
        return now,None,"尚未到计划开始日期"
    if now.strftime("%H:%M")<cfg.market_time:
        return now,None,"等待行情更新时点"
    if now.strftime("%H:%M")>=cfg.catch_up_until:
        return now,None,"已过当日补跑截止时间，不追发历史买点"
    cal, target = market.calendar_context()
    # 按实际收盘落在北京时间哪一天判断，不能按中国周一至周五硬切。
    closed = cal.session_close(target).tz_convert(cfg.timezone)
    if closed.date()!=now.date():
        return now,None,"没有在本地今日收盘的新美股交易日，跳过休市日"
    return now,target,"当前盘后处理窗口"


def _job_id(phase, target):
    return digest({"schedule":"post-close-v1","phase":phase,"session":target})[:32]


def _supported(symbol):
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}",symbol)) and not symbol.endswith((".SS",".SZ",".BJ"))


def _heat_day(cfg):
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if (cfg.enabled and cfg.futu_heat_enabled and day >= cfg.start_date
            and cfg.market_time <= now.strftime("%H:%M") < cfg.catch_up_until):
        return day
    return None


def _enqueue_heat(cfg, day):
    jid = _job_id("heat",day)
    with Session() as session:
        if session.get(Job,jid):
            return None
    anchor = security_for("AAPL",create=True)
    with Session() as session:
        session.add(Job(id=jid,security_id=anchor.id,kind=KINDS["heat"],active_key="post-close:heat",
            payload={"phase":"heat","local_date":day,"target_session":day,"policy":cfg.model_dump()},
            message=f"富途美股热榜排队中 · {day}"))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
    logger.info("已创建富途美股热榜任务 · %s（北京时间）", day)
    return jid


def enqueue_due():
    """只入队，不执行网络/模型/投递；同交易日同阶段确定ID永久防重复入队。"""
    cfg = policy()
    heat_day = _heat_day(cfg)
    heat_id = _enqueue_heat(cfg,heat_day) if heat_day else None
    now,target,message = _target(cfg)
    result = {"message":message,"local_date":now.date().isoformat(),"target_session":target,
              "queued":[heat_id] if heat_id else []}
    if target is None:
        if heat_id:
            result["message"] = "已入队每日富途热榜；没有新美股收盘，跳过行情及策略任务"
        return result
    if cfg.futu_heat_enabled:
        from . import futu_heat
        with Session() as session:
            dependency = session.get(Job,_job_id("heat",heat_day)) if heat_day else None
            if heat_day and (not dependency or dependency.status not in ("completed","failed")):
                return {**result,"message":"等待富途热榜同步结束，再冻结关注名单"}
        if not futu_heat.ready():
            return {**result,"message":"首次富途热榜尚未成功，保留旧池并暂停自动扫描"}
    members = watchlist.entries()
    if not members and not cfg.volume_top50_enabled:
        return {**result,"message":"关注列表为空，本次无股票可处理"}
    for phase in ("market","signals"):
        due = cfg.market_time if phase=="market" else cfg.signal_time
        if now.strftime("%H:%M")<due:
            continue
        jid = _job_id(phase,target)
        with Session() as session:
            if session.get(Job,jid):
                continue
            dependency = session.get(Job,_job_id("market",target)) if phase=="signals" else None
            if phase=="signals" and (dependency is None or dependency.status not in ("completed","failed")):
                result["message"] = "等待同交易日行情更新阶段结束，再计算买点"
                continue
            symbols = (list(dependency.result.get("symbols", dependency.payload["symbols"]))
                       if dependency else [m["symbol"] for m in members])
            if phase=="signals" and not symbols:
                continue
            payload = {"phase":phase,"target_session":target,"local_date":now.date().isoformat(),
                       "policy":cfg.model_dump(),"symbols":symbols,
                       "market_job_id":dependency.id if dependency else None}
        # 先结束读取事务再创建证券/任务，避免SQLite嵌套读写连接争锁。
        # 空池也可以由榜单扩充；锚点只用于Job外键及供应商冷却记录，不加入关注池。
        anchor = security_for(symbols[0] if symbols else "AAPL",create=True)
        with Session() as session:
            job = Job(id=jid,security_id=anchor.id,kind=KINDS[phase],active_key="post-close:"+phase,
                      payload=payload,message=f"盘后{'行情更新' if phase=='market' else '买点扫描与推送'}排队中 · {target}")
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                # 同日重复tick或前一交易日任务尚占用，不制造另一份排队任务。
                session.rollback()
                continue
        result["queued"].append(jid)
        logger.info("已创建盘后%s任务；%s %s，交易日%s，股票%s只",phase,cfg.timezone,due,target,len(symbols))
    return result


def _allowed(payload):
    cfg = policy()
    frozen = Policy.model_validate(payload["policy"])
    now = _local(frozen)
    due = frozen.market_time if payload["phase"]=="market" else frozen.signal_time
    _,target,_ = _target(cfg)
    return (target==payload["target_session"] and now.date().isoformat()==payload["local_date"]
            and due<=now.strftime("%H:%M")<frozen.catch_up_until)


def _checkpoint(job_id, result, percent, message):
    with Session() as session:
        row = session.get(Job,job_id)
        row.result = dict(result); row.progress=percent; row.message=message; row.updated_at=stamp()
        session.commit()


def _cached(symbol, target):
    security = security_for(symbol)
    with Session() as session:
        member = session.get(WatchlistEntry,symbol)
        source = latest(session,security.id,"market")
        state = market.cache_state(source)
        valid = (member is not None and member.active and source is not None and state["state"]=="fresh"
                 and state.get("expected_session")==target and source.payload.get("last_complete_session")==target
                 and any(b.get("complete") is True and b.get("date")==target for b in source.payload.get("bars",[])))
        return {"symbol":symbol,"ready":bool(valid),"market_id":source.id if source else None,
                "data_as_of":source.payload.get("last_complete_session") if source else None,"cache_state":state["state"]}


def _market(job, result):
    symbols = result.get("symbols", job.payload["symbols"])
    if (job.payload["policy"].get("volume_top50_enabled") and "volume_top50" not in result
            and not policy().futu_heat_enabled):
        if not _allowed(job.payload):
            return {**result,"status":"expired","message":"盘后窗口已结束或任务停用，未获取成交量榜"}
        from . import volume_leaders
        target = job.payload["target_session"]
        try:
            # 事务已提交而任务尚未落进度时，重启后复用同日榜单，不重新请求或累计。
            report = volume_leaders.latest_report()
            if not report or report["session"] != target:
                report = volume_leaders.apply(volume_leaders.fetch(job.security_id,target),
                                              anchor_id=job.security_id,
                                              allowed=lambda: _allowed(job.payload))
            symbols = list(dict.fromkeys([*symbols, *report["added"], *report["retained"]]))
            result["volume_top50"] = report
            message = f"成交量榜50只，新增或恢复{len(report['added'])}只"
            logger.info("%s · %s", target, message)
        except Exception as exc:
            detail = failure_detail(exc, "美股成交量前50")
            result["volume_top50"] = {**detail,"status":"failed","session":target}
            message = "成交量榜未更新：" + detail["message"]
            result["warnings"].append({"message":message})
            logger.warning("%s · %s；继续原关注池日线更新", target, message)
        result["symbols"] = symbols
        _checkpoint(job.id,result,0,message)
    completed = {r["symbol"] for r in result["items"]}
    for index,symbol in enumerate(symbols,1):
        if symbol in completed:
            continue
        if not _allowed(job.payload):
            result.update(status="expired",message="行情更新窗口已结束或任务停用，保留已完成缓存")
            return result
        logger.info("盘后行情[%s/%s] %s；目标交易日%s",index,len(symbols),symbol,job.payload["target_session"])
        try:
            with Session() as session:
                member = session.get(WatchlistEntry,symbol)
                active = member is not None and member.active
            if not active:
                item = {"symbol":symbol,"status":"SKIPPED_REMOVED","message":"已移出关注列表"}
            elif not _supported(symbol):
                item = {"symbol":symbol,"status":"FAILED","message":"计划任务仅处理美股代码"}
            else:
                market.refresh_market(security_for(symbol,create=True).id)
                item = _cached(symbol,job.payload["target_session"])
                item.update(status="READY" if item["ready"] else "FAILED",message="完整日线已缓存" if item["ready"] else "未取得目标日完整有效K线")
        except Exception as exc:
            item = {"symbol":symbol,"status":"FAILED",**failure_detail(exc)}
        result["items"].append(item)
        if item["status"]=="FAILED":
            result["warnings"].append({"symbol":symbol,"message":item["message"]})
            logger.warning("%s盘后日线待处理：%s",symbol,item["message"])
        _checkpoint(job.id,result,int(95*index/max(len(symbols),1)),f"行情更新 {index}/{len(symbols)} · {symbol}")
    result.update(status="partial" if result["warnings"] else "complete",message="关注列表日线更新阶段已结束")
    return result


def _signals(job, result):
    from . import notifications
    with Session() as session:
        dependency = session.get(Job,job.payload["market_job_id"])
        if (dependency is None or dependency.status not in ("completed","failed")
                or dependency.kind!=KINDS["market"] or dependency.payload.get("target_session")!=job.payload["target_session"]):
            raise ValueError("行情更新阶段尚未结束，不能提前扫描买点")
    if not _allowed(job.payload):
        return {**result,"status":"expired","message":"已过当前处理时段或任务已停用，不发送旧信号"}
    if not result.get("scan_id"):
        eligible = []
        for symbol in job.payload["symbols"]:
            try:
                if not _supported(symbol):
                    raise ValueError("仅支持美股")
                item = _cached(symbol,job.payload["target_session"])
            except (ValueError,LookupError):
                item = {"symbol":symbol,"ready":False}
            if item["ready"]:
                eligible.append(symbol)
            else:
                result["warnings"].append({"symbol":symbol,"message":"缺少目标日完整有效缓存或已移出关注列表，跳过买点推送"})
        if not eligible:
            return {**result,"status":"partial","message":"没有可用的当日完整K线，本次未扫描或发送"}
        logger.info("盘后计算买点：交易日%s，可用股票%s只；只读缓存，不判断账户金额",job.payload["target_session"],len(eligible))
        scan = watchlist.scan(symbols=eligible,offline=True)
        if scan.get("expected_session")!=job.payload["target_session"]:
            return {**result,"status":"expired","message":"目标交易日发生变化，本次不发送"}
        result["scan_id"] = scan["scan_id"]
        result["eligible_symbols"] = eligible
        # 必须先保存扫描ID，再进入可能产生外部消息的步骤。中断后按同扫描恢复去重。
        _checkpoint(job.id,result,65,"买点计算完成，正在准备研究及飞书消息")
    scan = watchlist.scan_record(result["scan_id"])
    if not _allowed(job.payload):
        return {**result,"status":"expired","message":"策略计算后处理窗口结束或计划已停用，不发送"}
    for row in scan["results"]:
        if row["status"]!="OK":
            result["warnings"].append({"symbol":row["symbol"],"message":(row.get("error") or {}).get("message") or "扫描资料未达到当前有效要求，不推送"})
    for symbol in scan.get("excluded_symbols") or []:
        result["warnings"].append({"symbol":symbol,"message":"扫描前已移出关注列表，跳过"})
    has_buy = any(row["status"]=="OK" and any(s.get("event_kind")=="BUY" for s in row.get("current_signals",[])) for row in scan["results"])
    if not has_buy:
        return {**result,"status":"partial" if result["warnings"] else "complete",
                "message":"当日没有当前有效买点，未发送飞书消息"}
    outcome = notifications.run(scan_id=result["scan_id"],send=True,cached_research=True,buy_only=True,
                                delivery_guard=lambda: _allowed(job.payload))
    result["notifications"] = outcome
    result["warnings"].extend({"message":w} for w in outcome["warnings"])
    for item in outcome["items"]:
        if item["status"] not in ("SENT","SKIPPED_SENT"):
            result["warnings"].append({"symbol":item["symbol"],"message":item.get("error") or item["status"]})
    sent = sum(i["status"]=="SENT" for i in outcome["items"])
    result.update(status="partial" if result["warnings"] else "complete",
                  message=f"盘后买点处理结束，新推送{sent}条；无买点不发研究快照")
    return result


def _heat(job, result):
    from . import futu_heat
    day = job.payload["local_date"]
    allowed = lambda: _heat_day(policy()) == day
    if not allowed():
        return {**result,"status":"expired","message":"富途热榜更新窗口已结束或已停用"}
    report = futu_heat.sync(day,send=True,allowed=allowed)
    notice = report["delivery"]
    if notice["status"] not in ("SENT","NO_ADDITIONS"):
        result["warnings"].append({"message":notice.get("message") or f"飞书状态 {notice['status']}，未确认送达"})
    result.update(status="partial" if result["warnings"] else "complete",
                  added=[row["symbol"] for row in report["added"]], active_count=report["active_count"],
                  delivery=notice,message=f"富途热榜完成：新增{len(report['added'])}只，当前{report['active_count']}只")
    return result


def run(job):
    if job.kind not in KINDS.values() or KINDS.get(job.payload.get("phase"))!=job.kind:
        raise ValueError("盘后任务阶段无效")
    result = {"phase":job.payload["phase"],"target_session":job.payload["target_session"],
              "items":[],"warnings":[],"status":"running",**(job.result or {})}
    if job.kind == KINDS["heat"]:
        return _heat(job,result)
    return _market(job,result) if job.kind==KINDS["market"] else _signals(job,result)


def status():
    cfg = policy(); now,target,message = _target(cfg)
    jobs = []
    ready = storage_ready("research_jobs")
    if ready:
        with Session() as session:
            jobs = [{"id":j.id,"kind":j.kind,"status":j.status,"progress":j.progress,"message":j.message,
                     "target_session":j.payload.get("target_session"),"local_date":j.payload.get("local_date"),
                     "result_status":j.result.get("status"),"warnings":j.result.get("warnings",[]),
                     "updated_at":j.updated_at} for j in session.scalars(select(Job).where(Job.kind.in_(tuple(KINDS.values())))
                        .order_by(Job.created_at.desc()).limit(20))]
    return {"policy":cfg.model_dump(),"local_time":now.isoformat(),"target_session":target,"message":message,"jobs":jobs,
            "storage_initialized":ready,
            "execution":"后台Worker存活时轮询到点入队；未在运行的服务不会被此接口唤醒"}

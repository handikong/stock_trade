"""系统内盘后两阶段计划任务：北京时间更新日线，再用完整缓存扫描买点并通知。"""
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import account_equity, cn_post_close, market, watchlist
from .config import settings
from .cli_helpers import storage_ready
from .providers import failure_detail
from .service import security_for
from .store import Job, Session, WatchlistEntry, digest, latest, stamp

logger = logging.getLogger(__name__)
KINDS = {"heat":"scheduled_futu_heat","market":"scheduled_market","signals":"scheduled_signals",
         "valuation":"scheduled_valuation","market_brief":"scheduled_market_brief","holdings_brief":"scheduled_holdings_brief",
         "cn_heat":"scheduled_cn_heat"}


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
    # 兼容旧配置和任务快照。旧 A 股人气榜入口已经停用。
    cn_heat_enabled: bool = False
    cn_heat_time: str = "16:30"
    cn_heat_until: str = "19:00"
    cn_enabled: bool = True
    cn_market_time: str = "15:35"
    cn_market_until: str = "21:00"
    cn_scan_time: str = "08:00"
    cn_push_time: str = "09:00"
    cn_push_until: str = "09:30"
    weekly_valuation_enabled: bool = False
    weekly_valuation_time: str = "08:00"
    market_brief_enabled: bool = False
    market_brief_time: str = "07:00"
    market_brief_until: str = "12:00"
    market_brief_feishu_enabled: bool = False
    # 自定义旧配置缺此字段时，A 股沿用旧的共用开关。
    cn_market_brief_feishu_enabled: bool | None = None
    holdings_brief_enabled: bool = False
    holdings_brief_time: str = "08:00"
    holdings_brief_until: str = "12:00"
    holdings_brief_feishu_enabled: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.version!="post-close-schedule-v1":
            raise ValueError("盘后计划任务配置版本无效")
        ZoneInfo(self.timezone)
        if date.fromisoformat(self.start_date).isoformat()!=self.start_date:
            raise ValueError("计划开始日期需要YYYY-MM-DD格式")
        values = (self.market_time,self.signal_time,self.catch_up_until,self.weekly_valuation_time,
                  self.market_brief_time,self.market_brief_until,self.holdings_brief_time,self.holdings_brief_until,
                  self.cn_market_time,self.cn_market_until,self.cn_scan_time,self.cn_push_time,self.cn_push_until)
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v) for v in values):
            raise ValueError("计划时间需要HH:MM格式")
        if not self.market_time < self.signal_time < self.catch_up_until:
            raise ValueError("需要满足行情时间＜策略时间＜当日补跑截止时间")
        if not self.market_brief_time<self.market_brief_until:
            raise ValueError("大盘报告生成时间需早于补跑截止")
        if not self.holdings_brief_time<self.holdings_brief_until:
            raise ValueError("持仓复盘生成时间需早于补跑截止")
        if not self.cn_scan_time<=self.cn_push_time<self.cn_push_until<="09:30":
            raise ValueError("A 股需要满足扫描时间≤推送时间＜补跑截止≤09:30")
        if not "15:00"<=self.cn_market_time<self.cn_market_until:
            raise ValueError("A 股收盘行情任务需在15:00后开始且早于截止时间")
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
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}",symbol)) and not symbol.endswith((".SH",".SS",".SZ",".BJ"))


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
    logger.info("已创建富途美股热榜任务 · %s（北京时间）",day)
    return jid


def _enqueue_daily(cfg):
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


def _valuation_day(cfg):
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if (cfg.enabled and cfg.weekly_valuation_enabled and now.weekday()==5
            and day>=cfg.start_date and now.strftime("%H:%M")>=cfg.weekly_valuation_time):
        return day
    return None


def _enqueue_valuation(cfg, day, target):
    jid = _job_id("valuation",day)
    dependencies = [_job_id("heat",day)]
    if target:
        dependencies.extend(_job_id(phase,target) for phase in ("market","signals"))
    with Session() as session:
        if session.get(Job,jid) or session.scalar(select(Job.id).where(
                Job.id.in_(dependencies), Job.status.in_(("queued","running"))).limit(1)):
            return None
    symbols = [row["symbol"] for row in watchlist.entries() if _supported(row["symbol"])]
    if not symbols:
        return None
    anchor = security_for(symbols[0],create=True)
    with Session() as session:
        session.add(Job(id=jid,security_id=anchor.id,kind=KINDS["valuation"],active_key="post-close:valuation",
            payload={"phase":"valuation","local_date":day,"target_session":day,"symbols":symbols},
            message=f"每周估值排队中 · {len(symbols)}只"))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
    logger.info("已创建每周估值任务 · %s（北京时间），关注股%s只",day,len(symbols))
    return jid


def _brief_day(cfg):
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if (cfg.enabled and cfg.market_brief_enabled and day>=cfg.start_date and now.weekday()<6
            and cfg.market_brief_time<=now.strftime("%H:%M")<cfg.market_brief_until):
        return day, {0:"monday",5:"weekly"}.get(now.weekday(),"daily")
    return None


def _holdings_brief_day(cfg):
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if (cfg.enabled and cfg.holdings_brief_enabled and day>=cfg.start_date and now.weekday()<5
            and cfg.holdings_brief_time<=now.strftime("%H:%M")<cfg.holdings_brief_until):
        return day
    return None


def _enqueue_report(phase, day, *, report_type=None, resume_delivery=False):
    jid = _job_id(phase,day)
    with Session() as session:
        existing = session.get(Job,jid)
        if existing:
            saved = (existing.result or {}).get("report")
            if resume_delivery and existing.status=="completed" and saved and not saved.get("delivery"):
                # 同日恢复之前只归档的报告。保留研判与模型结果，已经尝试投递的报告不重发。
                try:
                    changed = session.execute(update(Job).where(Job.id==jid,Job.status=="completed",
                        Job.updated_at==existing.updated_at).values(status="queued",active_key="post-close:market-brief",
                        progress=85,message="已归档美股大盘简报等待飞书",updated_at=stamp())).rowcount
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    return None
                return jid if changed else None
            return None
    _, target = market.calendar_context()
    anchor = security_for("SPY",create=True)
    label = "大盘报告" if phase=="market_brief" else "持仓复盘"
    payload = {"phase":phase,"local_date":day,"target_session":target}
    if report_type is not None:
        payload["report_type"] = report_type
    with Session() as session:
        session.add(Job(id=jid,security_id=anchor.id,kind=KINDS[phase],active_key="post-close:"+phase.replace("_","-"),
                        payload=payload,message=f"{label}排队中 · {day}（北京时间）"))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
    logger.info("已创建%s任务 · %s（北京时间），美股日线%s",label,day,target)
    return jid


def _enqueue_us(cfg):
    brief = _brief_day(cfg)
    brief_id = (_enqueue_report("market_brief",brief[0],report_type=brief[1],
                                resume_delivery=cfg.market_brief_feishu_enabled) if brief else None)
    result = _enqueue_daily(cfg)
    if brief_id:
        result["queued"].insert(0,brief_id)
        result["message"] = "已入队大盘报告"
    holdings_day = _holdings_brief_day(cfg)
    if holdings_day and (jid:=_enqueue_report("holdings_brief",holdings_day)):
        result["queued"].append(jid)
        result["message"] = "已入队持仓复盘"
    day = _valuation_day(cfg)
    if day and not result["queued"]:
        jid = _enqueue_valuation(cfg,day,result["target_session"])
        if jid:
            result["queued"].append(jid)
            result["message"] = "已入队每周关注股估值更新"
    return result


def _equity_window(cfg,region):
    if region=="US":
        local,target,_ = _target(cfg)
        return local.date().isoformat(),target
    day,target,phases = cn_post_close.current_window(cfg)
    return day,target if "market" in phases else None


def enqueue_due():
    """只入队；各市场按自己的交易日、窗口及已存任务独立判断。"""
    cfg = policy()
    try:
        result = _enqueue_us(cfg)
    except (ValueError,OSError) as exc:
        result = {"queued":[],"local_date":_local(cfg).date().isoformat(),"target_session":None,
                  "message":f"美股任务暂不可用 · {exc}"}
    try:
        cn = cn_post_close.enqueue_due(cfg)
    except (ValueError,OSError) as exc:
        cn = {"queued":[],"message":str(exc)}
    result["cn"] = cn
    result["queued"].extend(cn["queued"])
    if cn["queued"]:
        result["message"] += " · 已入队 A 股任务"
    for region in ("US","CN"):
        try:
            day,target = _equity_window(cfg,region)
            job_id = account_equity.enqueue(region,target,day) if target else None
            if job_id:
                result["queued"].append(job_id)
                result["message"] += f" · 已入队{account_equity.MARKET_LABELS[region]}净值"
        except (ValueError,OSError) as exc:
            result.setdefault("equity_warnings",{})[region] = str(exc)
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
    delivery = {"SENT":"飞书已接收","NO_ADDITIONS":"无新增，未发飞书"}.get(notice["status"],f"飞书 {notice['status']}")
    result.update(status="partial" if result["warnings"] else "complete",
                  added=[row["symbol"] for row in report["added"]], active_count=report["active_count"],
                  delivery=notice,message=f"富途热榜完成：新增{len(report['added'])}只，当前{report['active_count']}只 · {delivery}")
    return result


def run(job):
    if job.kind==account_equity.SCHEDULED_KIND:
        region = job.payload["market"]
        expected = (job.payload["local_date"],job.payload["target_session"])
        return account_equity.capture(job,lambda percent,message:_checkpoint(job.id,{},percent,message),
                                      allowed=lambda: _equity_window(policy(),region)==expected)
    if job.kind in cn_post_close.KINDS.values():
        return cn_post_close.run(job,policy=policy,checkpoint=_checkpoint)
    if job.kind not in KINDS.values() or KINDS.get(job.payload.get("phase"))!=job.kind:
        raise ValueError("盘后任务阶段无效")
    result = {"phase":job.payload["phase"],"target_session":job.payload["target_session"],
              "items":[],"warnings":[],"status":"running",**(job.result or {})}
    if job.kind == KINDS["heat"]:
        return _heat(job,result)
    if job.kind == KINDS["cn_heat"]:
        return {**result,"status":"disabled","message":"A 股人气榜已停用，请使用 A 股收息关注池"}
    if job.kind == KINDS["valuation"]:
        from . import weekly_valuation
        return weekly_valuation.run(job,result,allowed=lambda: _valuation_day(policy()) == job.payload["local_date"])
    if job.kind == KINDS["market_brief"]:
        from . import market_brief
        expected = (job.payload["local_date"],job.payload["report_type"])
        return market_brief.run(job,result,allowed=lambda: _brief_day(policy())==expected,
            delivery_enabled=lambda: policy().market_brief_feishu_enabled,checkpoint=_checkpoint)
    if job.kind == KINDS["holdings_brief"]:
        from . import holdings_brief
        day,target = (job.payload[key] for key in ("local_date","target_session"))
        return holdings_brief.run(job,result,allowed=lambda: _holdings_brief_day(policy())==day and market.calendar_context()[1]==target,
            delivery_enabled=lambda: policy().holdings_brief_feishu_enabled,checkpoint=_checkpoint)
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
                     "delivery_status":j.result.get("delivery_status") or (j.result.get("delivery") or {}).get("status"),
                     "notification_status":(j.result.get("notifications") or {}).get("status"),
                     "updated_at":j.updated_at} for j in session.scalars(select(Job).where(
                         Job.kind.in_((*KINDS.values(),*cn_post_close.KINDS.values(),account_equity.SCHEDULED_KIND)))
                        .order_by(Job.created_at.desc()).limit(20))]
    try:
        cn_day,cn_target,cn_phases = cn_post_close.current_window(cfg)
        cn = {"local_date":cn_day,"target_session":cn_target,"phases":cn_phases}
    except ValueError as exc:
        cn = {"message":str(exc),"phases":[]}
    return {"policy":cfg.model_dump(),"local_time":now.isoformat(),"target_session":target,"message":message,"jobs":jobs,"cn":cn,
            "storage_initialized":ready,
            "execution":"后台Worker存活时轮询到点入队；未在运行的服务不会被此接口唤醒"}

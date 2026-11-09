"""沪深收盘行情、次日盘前扫描和飞书任务。"""
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy.exc import IntegrityError

from . import cn_holdings, cn_income, cn_sources, market
from .providers import failure_detail
from .service import security_for
from .store import Job, Session, digest, latest, stamp

logger = logging.getLogger(__name__)
KINDS = {phase:f"scheduled_cn_{phase}" for phase in ("market","scan","morning","weekly","valuation")}
LABELS = {"market":"行情更新","scan":"盘前扫描","morning":"盘前飞书与复盘","weekly":"大盘周报","valuation":"每周估值"}


def current_window(cfg):
    local = datetime.now(timezone.utc).astimezone(cn_sources.BEIJING)
    day, clock = local.date().isoformat(), local.strftime("%H:%M")
    if not cfg.enabled or not cfg.cn_enabled or day<cfg.start_date:
        return day,None,()
    calendar,target = market.calendar_context("CN")
    trading = local.weekday()<5 and local.date()<=calendar.last_session.date() and calendar.is_session(day)
    phases = []
    if trading:
        if target==day and cfg.cn_market_time<=clock<cfg.cn_market_until:
            phases.append("market")
        if target<day and cfg.cn_scan_time<=clock<cfg.cn_push_until:
            phases.append("scan")
            if clock>=cfg.cn_push_time:
                phases.append("morning")
    elif local.weekday()==5:
        monday = (local.date()-timedelta(days=5)).isoformat()
        if target>=monday and cfg.market_brief_enabled and cfg.market_brief_time<=clock<cfg.market_brief_until:
            phases.append("weekly")
        if cfg.weekly_valuation_enabled and clock>=cfg.weekly_valuation_time:
            phases.append("valuation")
    return day,target,phases


def _id(phase, day, target):
    return digest(["cn-post-close-v1",phase,day if phase in ("weekly","valuation") else target])[:32]


def enqueue_due(cfg):
    try:
        day,target,phases = current_window(cfg)
    except ValueError as exc:
        return {"queued":[],"message":str(exc)}
    if not phases:
        return {"queued":[],"target_session":target,"message":"未到 A 股任务时段或休市"}
    from .cn_briefs import BENCHMARKS
    queued = []
    for phase in phases:
        job_id = _id(phase,day,target)
        with Session() as session:
            if session.get(Job,job_id):
                continue
            scan_id = _id("scan",day,target) if phase=="morning" else None
            if scan_id:
                scan = session.get(Job,scan_id)
                if scan is None or scan.status not in ("completed","failed"):
                    continue
            members = [row["symbol"] for row in cn_income.read(session)["members"]]
            held = [row.symbol for row in cn_holdings.positions(session)]
        if phase=="valuation" and not members:
            continue
        symbols = list(dict.fromkeys([*held,*BENCHMARKS,*members]))
        anchor = security_for(symbols[0],create=True)
        with Session() as session:
            session.add(Job(id=job_id,security_id=anchor.id,kind=KINDS[phase],active_key=f"cn-post-close:{phase}",
                message=f"A 股{LABELS[phase]}排队中 · {target}",payload={"phase":phase,"local_date":day,
                    "target_session":target,"members":members,"symbols":symbols,"scan_job_id":scan_id}))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
        queued.append(job_id)
        logger.info("已创建 A 股%s任务 · 北京时间 %s · 收盘交易日 %s",LABELS[phase],day,target)
    return {"queued":queued,"target_session":target,"message":"A 股计划任务已入队" if queued else "A 股当前时段任务已安排或等待依赖"}


def _refresh(job, result, allowed, checkpoint):
    symbols = job.payload["symbols"]
    done = {row["symbol"] for row in result["items"]}
    target = job.payload["target_session"]
    for code in symbols:
        if not allowed():
            break
        if code in done:
            continue
        try:
            security = security_for(code,create=True)
            market.refresh_market(security.id,region="CN")
            with Session() as session:
                cache = market.cache_state(latest(session,security.id,market.CN_SNAPSHOT_KIND),"CN")
            if cache["state"]!="fresh" or cache.get("last_complete_session")!=target:
                raise ValueError("目标收盘行情尚未齐全")
            item = {"symbol":code,"status":"ready"}
        except Exception as exc:
            message = failure_detail(exc)["message"]
            item = {"symbol":code,"status":"unavailable","message":message}
            result["warnings"].append({"symbol":code,"message":message})
        result["items"].append(item)
        count = len(result["items"])
        checkpoint(job.id,result,int(75*count/len(symbols)),f"A 股日线 {count}/{len(symbols)} · {code}")


def _scan(job, result, allowed, checkpoint):
    from . import watchlist
    if result.get("scan_id"):
        return
    _refresh(job,result,allowed,checkpoint)
    eligible = {row["symbol"] for row in result["items"] if row["status"]=="ready"}
    with Session() as session:
        active = {row["symbol"] for row in cn_income.read(session)["members"]}
    symbols = [code for code in job.payload["members"] if code in eligible and code in active]
    if not symbols or not allowed():
        result.update(scan_count=0,valid_count=0,buy_count=0)
        return
    scan = watchlist.scan(symbols=symbols,offline=True,region="CN")
    result["scan_id"] = scan["scan_id"]
    result["scan_count"] = len(scan["results"])
    result["valid_count"] = sum(row["status"]=="OK" for row in scan["results"])
    result["buy_count"] = sum(event.get("event_kind","BUY")=="BUY"
        for row in scan["results"] for event in row["current_signals"])
    for row in scan["results"]:
        if row["status"]!="OK":
            result["warnings"].append({"symbol":row["symbol"],"message":
                (row.get("error") or {}).get("message","策略数据待补，未生成可推送买点")})
    checkpoint(job.id,result,95,"A 股扫描与有效买点已保存")


def _send_report(job, result, card, category, part, guard, checkpoint):
    from . import feishu
    key = f"{category}:{part}"
    deliveries = result.setdefault("deliveries",{})
    existing = deliveries.get(key)
    if existing:
        if existing["status"]=="SENDING":
            existing.update(status="UNKNOWN",message="上次投递中断，请核对飞书群")
            checkpoint(job.id,result,95,"A 股报告投递状态已恢复")
        return
    if not guard():
        return
    delivery = {"category":category,"card":card,"status":"SENDING","started_at":stamp()}
    deliveries[key] = delivery
    checkpoint(job.id,result,95,"A 股报告等待飞书确认")
    try:
        accepted = feishu.send(card,allowed=guard)
        delivery["status"] = "SENT" if accepted else "SKIPPED"
    except (ValueError,feishu.DeliveryFailure) as exc:
        delivery.update(status=getattr(exc,"status","FAILED"),message=str(exc))
        result["warnings"].append({"message":str(exc)})
    delivery["finished_at"] = stamp()
    checkpoint(job.id,result,95,"A 股报告投递结果已保存")


def _market_report(job, result, allowed, checkpoint, report_type):
    from . import cn_briefs, market_brief
    if "market_report" not in result and allowed():
        if report_type=="weekly":
            for code in cn_briefs.BENCHMARKS:
                if not allowed():
                    return None
                try:
                    market.refresh_market(security_for(code,create=True).id,region="CN")
                except Exception as exc:
                    result["warnings"].append({"symbol":code,**failure_detail(exc)})
        result["market_report"] = cn_briefs.market_report(job.payload["local_date"],
                                                        job.payload["target_session"],report_type)
        checkpoint(job.id,result,80,"A 股大盘证据已保存")
    report = result.get("market_report")
    if not report:
        return None
    if (report.get("model") or {}).get("status")=="requesting":
        report["model"] = {"status":"unknown","message":"上次 DeepSeek 请求中断，保留程序研判"}
    elif "model" not in report and allowed():
        report["model"] = {"status":"requesting"}
        checkpoint(job.id,result,85,"已登记 A 股大盘解读请求")
        report["model"] = market_brief._interpret(report)
    checkpoint(job.id,result,90,"A 股大盘研判已保存")
    return report


def _morning(job, result, allowed, checkpoint):
    from . import cn_briefs, market_brief, notifications, watchlist
    from .post_close import policy
    day,target = (job.payload[key] for key in ("local_date","target_session"))
    with Session() as session:
        prep = session.get(Job,job.payload["scan_job_id"])
        scan_id = (prep.result or {}).get("scan_id") if prep else None
    scan = watchlist.scan_record(scan_id,region="CN") if scan_id else {}
    valid = [row for row in scan.get("results",[]) if row["status"]=="OK"]
    if result.get("notifications") is None and allowed():
        if not valid:
            message = "没有有效扫描结果，未发买点飞书"
            result["notifications"] = {"status":"NO_VALID_SCAN","items":[],"message":message}
            result["warnings"].append({"message":message})
        elif not any(signal.get("event_kind","BUY")=="BUY" for row in valid for signal in row["current_signals"]):
            result["notifications"] = {"status":"NO_BUYS","items":[],"message":"扫描有效，未触发买点"}
        else:
            try:
                outcome = notifications.run(scan_id=scan_id,send=True,buy_only=True,delivery_guard=allowed,region="CN")
                result["warnings"].extend({"message":warning} for warning in outcome["warnings"])
                for item in outcome["items"]:
                    if item["status"] not in ("SENT","SKIPPED_SENT"):
                        result["warnings"].append({"symbol":item["symbol"],"message":item.get("error") or item["status"]})
                sent = sum(item["status"]=="SENT" for item in outcome["items"])
                already = sum(item["status"]=="SKIPPED_SENT" for item in outcome["items"])
                result["notifications"] = {**outcome,"status":"PROCESSED",
                                           "message":f"买点飞书新发 {sent} 条，已送达去重 {already} 条"}
            except ValueError as exc:
                result["warnings"].append({"message":str(exc)})
                result["notifications"] = {"status":"FAILED","items":[],"message":str(exc)}
        checkpoint(job.id,result,55,"A 股买点处理已保存")
    if policy().holdings_brief_enabled and allowed():
        if "holdings_report" not in result:
            result["holdings_report"] = cn_briefs.holdings_report(day,target,scan)
            checkpoint(job.id,result,65,"A 股持仓复盘已保存")
        report = result["holdings_report"]
        def holdings_guard():
            if not allowed() or not policy().holdings_brief_enabled or not policy().holdings_brief_feishu_enabled:
                return False
            with Session() as session:
                return [(row.symbol,row.version) for row in cn_holdings.positions(session)]==[
                    (row["symbol"],row["version"]) for row in report["items"]]
        for part,card in enumerate(cn_briefs.holdings_cards(report),1):
            _send_report(job,result,card,"holdings",part,holdings_guard,checkpoint)
    if policy().market_brief_enabled and allowed():
        report_type = "monday" if datetime.fromisoformat(day).weekday()==0 else "daily"
        report = _market_report(job,result,allowed,checkpoint,report_type)
        if report and report["benchmarks"]:
            def market_guard():
                cfg = policy()
                push = cfg.cn_market_brief_feishu_enabled
                return allowed() and cfg.market_brief_enabled and (cfg.market_brief_feishu_enabled if push is None else push)
            _send_report(job,result,market_brief._card(report),"market",1,
                         market_guard,checkpoint)


def run(job, *, policy, checkpoint):
    from . import cn_research
    phase = job.payload["phase"]
    day,target = (job.payload[key] for key in ("local_date","target_session"))
    if KINDS.get(phase)!=job.kind:
        raise ValueError("A 股计划任务阶段无效")
    result = {"phase":phase,"target_session":target,"items":[],"warnings":[],**(job.result or {})}
    def allowed():
        try:
            actual_day,actual_target,phases = current_window(policy())
        except ValueError:
            return False
        return actual_day==day and actual_target==target and phase in phases
    if not allowed():
        for row in result.get("deliveries",{}).values():
            if row["status"]=="SENDING":
                row.update(status="UNKNOWN",message="上次投递中断，请核对飞书群")
        return {**result,"status":"expired","message":"A 股任务已过窗口或已停用"}
    if phase=="market":
        _refresh(job,result,allowed,checkpoint)
    elif phase=="scan":
        _scan(job,result,allowed,checkpoint)
    elif phase=="morning":
        _morning(job,result,allowed,checkpoint)
    elif phase=="weekly":
        _market_report(job,result,allowed,checkpoint,"weekly")
    else:
        return cn_research.weekly(job,result,allowed=allowed,checkpoint=checkpoint)
    message = f"A 股{LABELS[phase]}已处理 · 日线 {target}"
    if phase=="scan":
        message += f" · 有效 {result.get('valid_count',0)}/{result.get('scan_count',0)} 只 · 买点 {result.get('buy_count',0)} 条"
    if phase=="morning" and (outcome:=result.get("notifications")) and outcome.get("message"):
        message += f" · {outcome['message']}"
    return {**result,"status":"partial" if result["warnings"] or not allowed() else "complete","message":message}

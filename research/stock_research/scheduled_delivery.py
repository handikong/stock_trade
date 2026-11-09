"""定时飞书的计划与结果，只读现有任务和投递记录。"""
from datetime import datetime, time, timedelta
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import cn_post_close, post_close
from .cli_helpers import storage_ready
from .store import Job, Session, now

BEIJING = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)
CHANNELS = {
    "scheduled_futu_heat": {"heat":"富途热榜新增"},
    "scheduled_signals": {"signals":"美股买点"},
    "scheduled_market_brief": {"market":"美股大盘简报"},
    "scheduled_holdings_brief": {"holdings":"美股持仓复盘"},
    "scheduled_cn_morning": {"signals":"A 股买点", "holdings":"A 股持仓复盘", "market":"A 股大盘简报"},
}
REASONS = {
    "SENT":"飞书接口确认接收", "SKIPPED_SENT":"该消息已发送，本次去重",
    "NO_BUYS":"没有有效买点", "NO_ADDITIONS":"热榜没有新增股票", "NO_HOLDINGS":"没有登记持仓",
    "DISABLED":"配置已关闭，未发飞书", "SKIPPED_WINDOW":"已过处理窗口或任务已停用",
    "SKIPPED_SCHEDULE_WINDOW":"已过处理窗口或任务已停用", "NO_VALID_SCAN":"没有有效扫描结果",
    "SKIPPED_FAILED":"此前发送失败，自动任务不重发", "SKIPPED_UNKNOWN":"此前结果未知，自动任务不重发",
    "DEFERRED_LIMIT":"达到单轮消息上限", "SENDING":"已开始投递，尚无接收确认",
}
STATUS_ALIASES = {"已发送":"SENT", "发送中":"SENDING", "结果未知":"UNKNOWN", "失败":"FAILED", "跳过":"SKIPPED"}
WARNING = {"FAILED", "UNKNOWN", "NOT_CONFIGURED", "NO_VALID_SCAN", "NO_DATA", "NOT_ATTEMPTED",
           "SKIPPED_FAILED", "SKIPPED_UNKNOWN", "SKIPPED_WINDOW", "SKIPPED_SCHEDULE_WINDOW", "SKIPPED_CHANGED",
           "SKIPPED_STALE", "SKIPPED_STALE_AFTER_RESEARCH", "DEFERRED_LIMIT", "MISSED", "WINDOW_ELAPSED"}


def _safe(value):
    return " ".join(re.sub(r"https?://[^\s，；]+", "[地址已隐藏]", str(value or "")).split())[:400]


def plan(day, cfg):
    """按北京时间日期预览实际调度窗口。可选投递关闭时仍说明原因。"""
    day_key = day.isoformat()
    rows = []
    if not cfg.enabled:
        return rows

    def add(kind, channel, job_id, start, end, condition, *, enabled=True, dependency=None, title=None):
        rows.append({"kind":kind,"channel":channel,"name":title or CHANNELS[kind][channel],"job_id":job_id,
                     "start":start.astimezone(BEIJING),"end":end.astimezone(BEIJING),"enabled":enabled,
                     "condition":condition,"dependency":dependency})

    def at(clock, value=day, zone=BEIJING):
        return datetime.combine(value,time.fromisoformat(clock),zone)

    kinds = post_close.KINDS
    if day_key >= cfg.start_date and cfg.futu_heat_enabled:
        add(kinds["heat"],"heat",post_close._job_id("heat",day_key),at(cfg.market_time),at(cfg.catch_up_until),"热榜有新增股票")

    zone = ZoneInfo(cfg.timezone)
    for offset in (-1, 0, 1):
        local_day = day + timedelta(days=offset)
        start = at(cfg.signal_time,local_day,zone)
        if start.astimezone(BEIJING).date()!=day:
            continue
        end = at(cfg.catch_up_until,local_day,zone)
        _, target, _ = post_close._target(cfg,at=end-timedelta(seconds=1))
        if target:
            add(kinds["signals"],"signals",post_close._job_id("signals",target),start,end,
                f"美股 {target} 完整日线就绪且有有效买点",dependency=post_close._job_id("market",target))

    if day_key >= cfg.start_date and day.weekday()<6 and cfg.market_brief_enabled:
        name = {0:"开盘计划",5:"周报"}.get(day.weekday(),"日报")
        add(kinds["market_brief"],"market",post_close._job_id("market_brief",day_key),at(cfg.market_brief_time),
            at(cfg.market_brief_until),"有有效大盘数据",enabled=cfg.market_brief_feishu_enabled,title="美股大盘"+name)
    if day_key >= cfg.start_date and day.weekday()<5 and cfg.holdings_brief_enabled:
        add(kinds["holdings_brief"],"holdings",post_close._job_id("holdings_brief",day_key),at(cfg.holdings_brief_time),
            at(cfg.holdings_brief_until),"有登记持仓且资料与持仓版本仍有效",enabled=cfg.holdings_brief_feishu_enabled)

    start, end = at(cfg.cn_push_time), at(cfg.cn_push_until)
    _, target, phases = cn_post_close.current_window(cfg,at=end-timedelta(seconds=1))
    if "morning" in phases:
        kind = cn_post_close.KINDS["morning"]
        job_id = cn_post_close._id("morning",day_key,target)
        dependency = cn_post_close._id("scan",day_key,target)
        cn_market = cfg.cn_market_brief_feishu_enabled
        add(kind,"signals",job_id,start,end,f"A 股 {target} 盘前扫描结束且有有效买点",dependency=dependency)
        add(kind,"holdings",job_id,start,end,"盘前扫描结束且有登记持仓",
            enabled=cfg.holdings_brief_enabled and cfg.holdings_brief_feishu_enabled,dependency=dependency)
        add(kind,"market",job_id,start,end,"盘前扫描结束且有有效大盘数据",
            enabled=cfg.market_brief_enabled and (cfg.market_brief_feishu_enabled if cn_market is None else cn_market),
            dependency=dependency)
    return sorted(rows,key=lambda row:row["start"])


def _outcome(row, label=""):
    raw = row.get("status") or "NOT_ATTEMPTED"
    status = STATUS_ALIASES.get(raw,raw.upper())
    reason = row.get("error") or row.get("message") or REASONS.get(status) or status
    return {"status":status,"reason":_safe(reason),"item":label}


def _deliveries(kind, result):
    report = result.get("report") or {}
    notes = result.get("delivery_notes") or {}
    notifications = result.get("notifications") or {}
    rows = {"signals":[_outcome(row,row.get("symbol") or "") for row in notifications.get("items",[])]}
    if notifications.get("status") and not rows["signals"]:
        rows["signals"].append(_outcome(notifications))
    if kind=="scheduled_futu_heat" and result.get("delivery"):
        rows["heat"] = [_outcome(result["delivery"])]
    if kind=="scheduled_market_brief":
        if delivery:=report.get("delivery") or ({"status":result["delivery_status"]} if result.get("delivery_status") else None):
            rows["market"] = [_outcome(delivery)]
    if kind=="scheduled_holdings_brief":
        rows["holdings"] = [_outcome(row,f"卡片{row['part']}") for row in report.get("delivery",[])]
        if not rows["holdings"] and result.get("delivery_status"):
            rows["holdings"].append(_outcome({"status":result["delivery_status"]}))
        if result.get("remaining_delivery"):
            rows["holdings"].append(_outcome(result["remaining_delivery"],"余下卡片"))
    if kind=="scheduled_cn_morning":
        for key, delivery in (notes | (result.get("deliveries") or {})).items():
            channel, _, part = key.partition(":")
            rows.setdefault(channel,[]).append(_outcome(delivery,f"卡片{part}" if part else ""))
    return rows


def outcomes(kind, result, job_status, message):
    recorded = _deliveries(kind,result)
    fallback = ("SKIPPED_WINDOW" if result.get("status")=="expired" else
                "FAILED" if job_status=="failed" else "NOT_ATTEMPTED")
    rows = {channel:recorded.get(channel) or [_outcome({"status":fallback,"message":message})]
            for channel in CHANNELS.get(kind,{})}
    for items in rows.values():
        for item in items:
            if job_status in ("completed","failed") and item["status"]=="SENDING":
                item.update(status="UNKNOWN",reason="投递未取得最终确认，请核对飞书群")
    return rows


def _missing(dependency, required):
    if not dependency:
        return ("前置行情或扫描任务没有入队记录；推送未入队" if required else
                "没有入队记录，请核对后台运行和任务窗口")
    return f"前置任务 {dependency.status} · {_safe(dependency.message)}；推送未入队"


def inspect_plan(rows, *, current=None):
    current = current or now()
    keys = {key for row in rows for key in (row["job_id"],row["dependency"]) if key}
    jobs = {}
    if keys and storage_ready("research_jobs"):
        with Session() as session:
            jobs = {job.id:job for job in session.scalars(select(Job).where(Job.id.in_(keys)))}
    views = []
    for row in rows:
        job = jobs.get(row["job_id"])
        if job and job.status in ("completed","failed"):
            detail = outcomes(row["kind"],job.result or {},job.status,job.message)[row["channel"]]
            if not row["enabled"] and detail[0]["status"]=="NOT_ATTEMPTED":
                detail = [_outcome({"status":"DISABLED"})]
        elif not row["enabled"]:
            detail = [_outcome({"status":"DISABLED"})]
        elif job:
            status = "WINDOW_ELAPSED" if current>=row["end"] else job.status.upper()
            prefix = "截止时尚未结束 · " if status=="WINDOW_ELAPSED" else ""
            detail = [_outcome({"status":status,"message":prefix+_safe(job.message)})]
        elif current>=row["end"]:
            dependency = row["dependency"]
            detail = [_outcome({"status":"MISSED","message":_missing(jobs.get(dependency),dependency is not None)})]
        else:
            detail = [_outcome({"status":"PLANNED","message":row["condition"]})]
        views.append({**row,"details":detail})
    return views


def log_outcomes(job, result, status, message):
    day = job.payload.get("local_date","—")
    stage = result.get("status") or status
    warnings = result.get("warnings") or []
    log = logger.warning if status=="failed" or stage=="partial" else logger.info
    log("[计划任务] 日期=%s 任务=%s 编号=%s 状态=%s 告警=%s 原因=%s",
        day,job.kind,job.id,stage.upper(),len(warnings),_safe(message))
    for warning in warnings:
        label, reason = ((warning.get("symbol","—"),warning.get("message")) if isinstance(warning,dict) else ("—",warning))
        logger.warning("[计划任务] 日期=%s 任务=%s 编号=%s 子项=%s 状态=WARNING 原因=%s",
                       day,job.kind,job.id,label,_safe(reason))
    for channel, items in outcomes(job.kind,result,status,message).items():
        for item in items:
            code = item["status"]
            log = logger.warning if code in WARNING else logger.info
            log("[计划推送] 日期=%s 任务=%s 编号=%s 子项=%s 状态=%s 原因=%s",
                day,CHANNELS[job.kind][channel],job.id,item["item"] or "—",code,item["reason"])

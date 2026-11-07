"""每周自动估值参考，独立于手工确认估值及买卖策略。"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
from zoneinfo import ZoneInfo

from .store import Job, Session, WatchlistEntry, latest, save_snapshot, stamp

logger = logging.getLogger(__name__)
KIND = "weekly_valuation_reference"
BEIJING = ZoneInfo("Asia/Shanghai")


def _scenarios(eps, pe, day):
    if not eps or not pe or eps["fetched_at"] != pe["fetched_at"]:
        return [], "缺少同批完整数据，无法计算估值区间"
    if eps["period_end"] < day:
        return [], "EPS 所属财年已结束，等待更新"
    if eps["currency_per_share"] != "USD":
        return [], "财务 EPS 币种与美元报价不同，待核对币种及每股单位"
    inputs = [(eps[e], pe[p]) for e,p in (("low","p25"),("mean","p50"),("high","p75"))]
    if any(e is None or Decimal(e)<=0 for e,_ in inputs):
        return [], "三档 EPS 缺失或含非正值，无法使用 PE 倍数估值"
    return [
        {"name":name,"eps":e,"pe":p,
         "price":str((Decimal(e)*Decimal(p)).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))}
        for name,(e,p) in zip(("保守","基准","乐观"),inputs)
    ], "财年 EPS × 历史 Forward PE。PE 分母口径待核对，区间仅作试算。"


def read(session, security_id):
    snapshot = latest(session,security_id,KIND)
    if snapshot is None:
        return None
    value = snapshot.payload
    now = datetime.now(timezone.utc).astimezone(BEIJING).date()
    eps, pe = value.get("eps"), value.get("forward_pe")
    scenarios, message = _scenarios(eps,pe,now.isoformat())
    return {**value,"scenarios":scenarios,"message":message,"currency":"USD" if scenarios else None,
            "stale":any((now-datetime.fromisoformat(v["fetched_at"]).astimezone(BEIJING).date()).days>=7
                        for v in (eps,pe) if v)}


def _save(session, security_id, row, fetched_at, day):
    previous = latest(session,security_id,KIND)
    value = dict(previous.payload) if previous else {}
    issues = row["errors"] + row["quality_flags"]
    updated = []
    eps, pe = row.get("eps"), row.get("forward_pe")
    if (eps and not row["quality_flags"] and (eps.get("period_end") or "")>=day
            and all(eps.get(key) is not None for key in ("low","mean","high"))):
        value["eps"] = {**eps,"fetched_at":fetched_at,"url":row["forecast_url"]}
        updated.append("eps")
    else:
        issues.append("本次没有可采用的财年 EPS")
    if pe and all(pe.get(key) is not None and Decimal(pe[key])>0 for key in ("p25","p50","p75")):
        value["forward_pe"] = {**pe,"fetched_at":fetched_at,"url":row["ratios_url"]}
        updated.append("forward_pe")
    else:
        issues.append("本次没有足够的有效历史 Forward PE 样本")
    scenarios, message = _scenarios(value.get("eps"),value.get("forward_pe"),day)
    value.update(source="Stock Analysis",attempt={"date":day,"fetched_at":fetched_at,"updated":updated,"issues":issues})
    save_snapshot(session,security_id,KIND,value)
    return {"symbol":row["symbol"],"status":"READY" if len(updated)==2 and scenarios else
            "PARTIAL" if updated else "MISSING","issues":issues or ([] if scenarios else [message])}


def run(job, result, *, allowed):
    from .service import security_for
    from .stockanalysis_mg7 import collect

    symbols = job.payload["symbols"]
    day = job.payload["local_date"]
    if not allowed():
        return {**result,"status":"expired","message":"已过本周六估值窗口或任务已停用"}
    completed = {item["symbol"] for item in result["items"]}

    def record(row, fetched_at):
        if not allowed():
            return
        symbol = row["symbol"]
        security = security_for(symbol,create=True)
        # 同一事务存估值和完成进度，重启可从尚未写入的股票继续。
        with Session() as session:
            member = session.get(WatchlistEntry,symbol)
            if not member or not member.active:
                item = {"symbol":symbol,"status":"SKIPPED_REMOVED"}
            elif security.profile.get("type") in ("ETF","MUTUALFUND"):
                item = {"symbol":symbol,"status":"SKIPPED_FUND"}
            else:
                item = _save(session,security.id,row,fetched_at,day)
            result["items"].append(item)
            stored = session.get(Job,job.id)
            done = len(result["items"])
            stored.result = dict(result)
            stored.progress = int(95*done/len(symbols))
            stored.message = f"每周估值 {done}/{len(symbols)} · {symbol}"
            stored.updated_at = stamp()
            session.commit()
            logger.info("每周估值 %s/%s · %s",done,len(symbols),symbol)

    remaining = [symbol for symbol in symbols if symbol not in completed]
    if remaining:
        fetched = collect(remaining,scope="weekly_watchlist",on_row=record,allowed=allowed)
        if fetched["halted"]:
            result["warnings"].append({"message":fetched["halted"]})
    counts = Counter(item["status"] for item in result["items"])
    ready = counts["READY"]
    processed = len(result["items"])
    result.update(status="complete" if ready==len(symbols) else "partial",counts=dict(counts),
                  message=f"每周估值结束：试算{ready}只，已处理{processed}/{len(symbols)}只")
    return result

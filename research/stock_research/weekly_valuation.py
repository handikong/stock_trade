"""当前估值以周六采集为底稿，网页更正与采集共用一条版本链。"""
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import fcntl
import logging
import re
from zoneinfo import ZoneInfo

from .config import settings
from .store import Job, Session, WatchlistEntry, latest, save_snapshot, stamp

logger = logging.getLogger(__name__)
KIND = "weekly_valuation_reference"
BEIJING = ZoneInfo("Asia/Shanghai")
EPS = ("low","mean","high")
PE = ("p25","p50","p75")
EPS_FIELDS = ("period_end",*(f"eps_{key}" for key in EPS))
PE_FIELDS = tuple(f"pe_{key}" for key in PE)
FIELDS = (*EPS_FIELDS,*PE_FIELDS)


class Conflict(ValueError):
    pass


@contextmanager
def _write_lock():
    with (settings.storage / "weekly-valuation.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        yield


def _source_inputs(value):
    eps, pe = value.get("eps") or {}, value.get("forward_pe") or {}
    return {"period_end":eps.get("period_end"),
            **{f"eps_{key}":eps.get(key) if eps.get("currency_per_share")=="USD" else None for key in EPS},
            **{f"pe_{key}":pe.get(key) for key in PE}}


def _decimal_key(value):
    return Decimal(value) if value is not None else None


def _input_key(key, value):
    return value if key=="period_end" else _decimal_key(value)


def _scenarios(eps, pe, day):
    if not eps or not pe:
        return [], "EPS 或 PE 资料未齐，暂无法计算估值区间"
    if eps.get("fetched_at") != pe.get("fetched_at") and not (eps.get("corrected") or pe.get("corrected")):
        return [], "缺少同批完整数据，无法计算估值区间"
    if eps["period_end"] < day:
        return [], "EPS 所属财年已结束，等待更新"
    if eps["currency_per_share"] != "USD":
        return [], "财务 EPS 币种与美元报价不同，待核对币种及每股单位"
    inputs = [(eps[e], pe[p]) for e,p in zip(EPS,PE)]
    if any(e is None or Decimal(e)<=0 for e,_ in inputs):
        return [], "三档 EPS 缺失或含非正值，无法使用 PE 倍数估值"
    with localcontext() as ctx:
        ctx.prec = 48
        scenarios = [{"name":name,"eps":e,"pe":p,
                      "price":str((Decimal(e)*Decimal(p)).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))}
                     for name,(e,p) in zip(("保守","基准","乐观"),inputs)]
    return scenarios, "财年 EPS × Forward PE，分母口径待核对，区间仅作试算。"


def _view(snapshot):
    value = snapshot.payload if snapshot else {}
    corrections = value.get("corrections",{})
    inputs = {**_source_inputs(value),**corrections}
    eps, pe = value.get("eps"), value.get("forward_pe")
    if any(key in corrections for key in EPS_FIELDS) and all(inputs[key] is not None for key in EPS_FIELDS):
        eps = {**(eps or {}),**{key:inputs[f"eps_{key}"] for key in EPS},"period_end":inputs["period_end"],
               "currency_per_share":"USD","currency_source":"web_usd_per_share","corrected":True}
    if any(key in corrections for key in PE_FIELDS) and all(inputs[key] is not None for key in PE_FIELDS):
        pe = {**(pe or {}),**{key:inputs[f"pe_{key}"] for key in PE},"corrected":True}
    now = datetime.now(timezone.utc).astimezone(BEIJING).date()
    scenarios, message = _scenarios(eps,pe,now.isoformat())
    return {"id":snapshot.id if snapshot else None,"inputs":inputs,"corrected_fields":list(corrections),
            "corrected_at":value.get("corrected_at") if corrections else None,
            "source":value.get("source"),"attempt":value.get("attempt"),"eps":eps,"forward_pe":pe,
            "scenarios":scenarios,"message":message,"currency":"USD" if scenarios else None,
            "stale":any((now-datetime.fromisoformat(v["fetched_at"]).astimezone(BEIJING).date()).days>=7
                        for v in (eps,pe) if v and v.get("fetched_at"))}


def read(session, security_id):
    return _view(latest(session,security_id,KIND))


def correct(security, changes, expected_id):
    if security.profile.get("type") in ("ETF","MUTUALFUND"):
        raise ValueError("基金不适用公司EPS估值")
    with _write_lock(), Session() as session:
        snapshot = latest(session,security.id,KIND)
        if expected_id != (snapshot.id if snapshot else None):
            raise Conflict("估值已有更新，请关闭编辑后刷新研究页，再核对并保存。")
        value = dict(snapshot.payload) if snapshot else {}
        source = _source_inputs(value)
        before = value.get("corrections",{})
        corrections = dict(before)
        for key, raw in changes.items():
            if key not in FIELDS:
                raise ValueError("包含不支持的估值字段")
            if raw is not None:
                raw = raw.strip()
                if key=="period_end":
                    try:
                        end = date.fromisoformat(raw)
                    except ValueError:
                        raise ValueError("财年截止日须为有效的 YYYY-MM-DD") from None
                    if end.isoformat()!=raw or end<datetime.now(BEIJING).date():
                        raise ValueError("请选择尚未结束的财年截止日")
                elif not re.fullmatch(r"-?[0-9]{1,12}(?:\.[0-9]{1,8})?",raw):
                    raise ValueError("EPS 和 PE 须为十进制数，整数最多12位，小数最多8位")
                elif key in PE_FIELDS and Decimal(raw)<=0:
                    raise ValueError("PE 必须大于 0")
            if raw is None or _input_key(key,raw)==_input_key(key,source[key]):
                corrections.pop(key,None)
            else:
                corrections[key] = raw
        effective = {**source,**corrections}
        for label, fields in (("EPS",EPS_FIELDS[1:]),("PE",PE_FIELDS)):
            numbers = [Decimal(effective[key]) for key in fields if effective[key] is not None]
            if numbers != sorted(numbers):
                raise ValueError(f"{label} 须按低、基准、高从小到大排列")
        if corrections != before:
            value.update(corrections=corrections,corrected_at=stamp())
            snapshot = save_snapshot(session,security.id,KIND,value)
            result = _view(snapshot)
            session.commit()
            return result
        return _view(snapshot)


def _save(session, security_id, row, fetched_at, day):
    previous = latest(session,security_id,KIND)
    value = dict(previous.payload) if previous else {}
    old_inputs = _source_inputs(value)
    old_eps = value.get("eps") or {}
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
    new_inputs = _source_inputs(value)
    corrections = dict(value.get("corrections",{}))
    for group, fields in (("eps",EPS_FIELDS),("forward_pe",PE_FIELDS)):
        if group in updated and (any(_input_key(key,old_inputs[key])!=_input_key(key,new_inputs[key]) for key in fields)
                                 or (group=="eps" and (
                                     any(old_eps.get(key)!=eps.get(key) for key in ("currency_per_share","earnings_basis"))
                                     or any(_decimal_key(old_eps.get(key))!=_decimal_key(eps[key]) for key in EPS)))):
            for key in fields:
                corrections.pop(key,None)
    value.update(source="Stock Analysis",attempt={"date":day,"fetched_at":fetched_at,"updated":updated,"issues":issues})
    value["corrections"] = corrections
    snapshot = save_snapshot(session,security_id,KIND,value)
    current = _view(snapshot)
    scenarios, message = current["scenarios"], current["message"]
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
        with _write_lock(), Session() as session:
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

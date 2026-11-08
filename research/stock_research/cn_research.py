"""人民币估值缓存和 A 股买点研究，使用已有 EPS/PE 公共入口。"""
from datetime import date, datetime

import httpx

from . import cn_income, cn_sources, market, technical, watchlist
from .notification_cards import summarize
from .service import security_for
from .stockanalysis_mg7 import Reader, StopFetch, USER_AGENT
from .store import Session, latest, save_snapshot

VALUATION_KIND = "cn_weekly_valuation"


def _is_stock(code):
    try:
        cn_sources.symbol(code)
    except ValueError:
        return False
    return True


def valuation(session, security):
    if not _is_stock(security.symbol):
        return None
    row = latest(session,security.id,VALUATION_KIND)
    if row is None:
        return {"scenarios":[],"message":"尚无有效的人民币 EPS 和季度 Forward PE 三档"}
    day = datetime.now(cn_sources.BEIJING).date()
    value = row.payload
    expired = value["eps"]["period_end"] < day.isoformat()
    stale = (day-date.fromisoformat(value["as_of"])).days > 7
    return {**value,"id":row.id,"stale":stale or expired,
            "scenarios":[] if expired else value["scenarios"],
            "message":"预测财年已经结束，请等待新估值" if expired else "人民币财年 EPS × 历史 Forward PE，仅作估值参考"}


def prepare(code):
    security = security_for(code)
    with Session() as session:
        source = latest(session,security.id,market.CN_SNAPSHOT_KIND)
        cached = valuation(session,security)
        payload = source.payload if source else {}
        view = {**payload.get("technical",{})}
        view["stale"] = not technical.is_current(view)
        report = {"symbol":code,"market":{**{k:payload.get(k) for k in ("last_price","last_complete_session")},
                                          "technical":view},
                  "cache":market.cache_state(source,"CN"),"profile":payload.get("profile") or {},
                  "automatic_valuation_reference":cached}
        return {"brief":summarize(report),"versions":{"market":source.id if source else None,
                "cn_weekly_valuation":(cached or {}).get("id")}}


def weekly(job, result, *, allowed, checkpoint):
    symbols = [code for code in job.payload["members"] if _is_stock(code)]
    completed = {item["symbol"] for item in result["items"]}
    remaining = [code for code in symbols if code not in completed]
    if remaining and allowed():
        with httpx.Client(timeout=18,follow_redirects=False,
                          headers={"User-Agent":USER_AGENT,"Accept":"text/html, text/plain"}) as client:
            try:
                reader = Reader(client)
                for code in remaining:
                    if not allowed():
                        break
                    value = cn_sources.research(code,reader=reader)
                    if value.get("halted"):
                        result["warnings"].append({"symbol":code,"message":value["halted"]})
                        break
                    if not allowed():
                        break
                    status = "INCOMPLETE"
                    if value["scenarios"]:
                        security = security_for(code,create=True)
                        with watchlist.write_lock(), Session() as session:
                            active = {row["symbol"] for row in cn_income.read(session)["members"]}
                            status = "READY" if code in active else "SKIPPED_REMOVED"
                            if status=="READY":
                                previous = latest(session,security.id,VALUATION_KIND)
                                if previous is None or previous.payload!=value:
                                    save_snapshot(session,security.id,VALUATION_KIND,value)
                                    session.commit()
                    else:
                        result["warnings"].append({"symbol":code,"message":"人民币 EPS 或季度 Forward PE 样本不足，保留已有估值"})
                    result["items"].append({"symbol":code,"status":status})
                    checkpoint(job.id,result,int(95*len(result["items"])/len(symbols)),
                               f"A 股每周估值 {len(result['items'])}/{len(symbols)} · {code}")
            except StopFetch as exc:
                result["warnings"].append({"message":str(exc)})
    count = sum(item["status"]=="READY" for item in result["items"])
    return {**result,"status":"partial" if count!=len(symbols) else "complete",
            "message":f"A 股每周估值结束 · {count}/{len(symbols)} 只取得完整数据"}

import json
import math
import logging
from datetime import date, datetime, timedelta, timezone
import exchange_calendars as xcals
import pandas as pd
import yfinance as yf
from .config import settings
from .store import Session, Security, latest, save_snapshot, stamp
from .technical import structures, is_current as technical_is_current
from .providers import yahoo_call

logger=logging.getLogger(__name__)


def clean(value):
    """供应商浮点先转十进制文本；NaN不许静默变成零。"""
    if value is None:
        return None
    if isinstance(value,(datetime,date,pd.Timestamp)):
        return value.isoformat()
    if isinstance(value,dict):
        return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [clean(v) for v in value]
    if isinstance(value,str) or isinstance(value,bool):
        return value
    try:
        if not math.isfinite(float(value)):
            return None
        return str(value)
    except (ValueError,TypeError):
        return str(value)


def calendar_context():
    current=pd.Timestamp.now(tz="UTC")
    cal=xcals.get_calendar("XNYS",start=str(current.date()-timedelta(days=settings.history_years*366+30)),
        end=str(current.date()+timedelta(days=14)))
    session=cal.date_to_session(str(current.date()),direction="previous")
    if cal.session_close(session)+pd.Timedelta(minutes=settings.post_close_delay_minutes)>current:
        session=cal.previous_session(session)
    return cal,session.date().isoformat()


def cache_state(item):
    if not item:
        return {"state":"missing","message":"尚无行情缓存"}
    payload=item.payload
    try:
        _,expected=calendar_context()
    except Exception:
        return {"state":"unknown","message":"交易日历暂不可用，不能确认缓存新鲜度"}
    acquired=datetime.fromisoformat(payload["fetched_at"])
    ttl_ok=(datetime.now(timezone.utc)-acquired).total_seconds()<settings.market_cache_hours*3600
    ready=payload.get("last_complete_session","")>=expected and payload.get("coverage_ok",False) and ttl_ok
    return {"state":"fresh" if ready else "stale","expected_session":expected,
        "last_complete_session":payload.get("last_complete_session"),"fetched_at":payload["fetched_at"],
        "message":"命中完整有效缓存" if ready else "缓存需补齐或复核；旧资料仅供研究参考"}


def refresh_market(security_id: str, force=False):
    with Session() as session:
        old=latest(session,security_id,"market")
        security=session.get(Security,security_id)
        symbol=security.symbol
        old_payload=old.payload if old else None
        if not force and cache_state(old)["state"]=="fresh":
            if not technical_is_current(old_payload.get("technical",{})):
                updated={**old_payload,"technical":technical_view(old_payload["bars"]),"technical_calculated_at":stamp()}
                save_snapshot(session,security_id,"market",updated)
                session.commit()
                return {"cached":True,"technical_recomputed":True,"sessions":len(old_payload["bars"])}
            return {"cached":True}
    ticker=yf.Ticker(symbol)
    # 本模块已限定美股；给SDK提供交易时区，避免其时区探测失败后偷偷回退到info查询。
    # 这不是证券身份判断，后面仍校验日线响应自带的实际时区。
    if not hasattr(ticker,"_tz"):
        raise ValueError("当前yfinance版本不兼容无身份查询适配器，请使用项目约束的0.2.x版本")
    ticker._tz="America/New_York"
    cal,last_complete=calendar_context()
    target_start=date.today()-timedelta(days=settings.history_years*366)
    # 正常补齐近期缺口；遇企业行动则重新取完整窗口，防止新旧调整口径拼接。
    start=target_start
    if old_payload and old_payload.get("bars") and not force:
        start=max(target_start,date.fromisoformat(old_payload["bars"][-1]["date"])-timedelta(days=14))
        if old_payload.get("missing_sessions"):
            start=min(start,date.fromisoformat(old_payload["missing_sessions"][0])-timedelta(days=7))
        if old_payload.get("requested_start",str(target_start))>str(target_start):
            start=target_start
    def history(begin):
        return yahoo_call(security_id,"history(日线)",lambda:ticker.history(start=str(begin),interval="1d",
            auto_adjust=False,back_adjust=False,actions=True,repair=False,prepost=False,raise_errors=True))
    frame=history(start)
    if frame.empty:
        raise ValueError("yf没有返回有效行情，已保留原缓存")
    if start>target_start and ((frame.get("Stock Splits",pd.Series(dtype=float))!=0).any()
            or (frame.get("Dividends",pd.Series(dtype=float))!=0).any()):
        frame=history(target_start)
        start=target_start
    # 用户取消独立身份查询。只读取本次日线响应已包含的元数据，不调用get_info。
    # 0.2.x公开get_history_metadata会追加分时请求，故在适配层读取已载入对象，缺失时明确报错。
    info=dict(getattr(getattr(ticker,"_price_history",None),"_history_metadata",None) or {})
    if info.get("exchangeTimezoneName")!="America/New_York":
        raise ValueError("日线响应缺少有效美股时区，已保留原缓存；没有追加身份查询")
    profile={"name":info.get("longName") or info.get("shortName") or security.profile.get("sec_name") or symbol,
        "type":info.get("instrumentType") or "UNKNOWN","currency":info.get("currency"),
        "exchange":info.get("fullExchangeName") or info.get("exchangeName"),
        "market_metadata_source":"日线响应附带字段","market_metadata_at":stamp()}
    if not profile["currency"]:
        raise ValueError("日线响应缺少币种，不能假设为USD；已保留原缓存，没有追加身份查询")
    # 增量缺失不能删除旧窗口中的有效记录；完整重取时则不混入旧调整价。
    merged={b["date"]:b for b in (old_payload or {}).get("bars",[])} if start>target_start else {}
    for idx,row in frame.iterrows():
        close=clean(row.get("Close")); adjusted=clean(row.get("Adj Close"))
        if close is None or adjusted is None or float(close)<=0 or float(adjusted)<=0:
            raise ValueError("供应商价格存在无效数值，未覆盖原缓存")
        factor=float(adjusted)/float(close)
        if any(clean(row.get(k)) is None for k in ("Open","High","Low")):
            raise ValueError("供应商OHLC字段不完整，未覆盖原缓存")
        if not (0<float(row["Low"])<=min(float(row["Open"]),float(close))
                <=max(float(row["Open"]),float(close))<=float(row["High"])):
            raise ValueError("供应商OHLC关系不一致，未覆盖原缓存")
        volume=clean(row.get("Volume"))
        if volume is None or float(volume)<0:
            raise ValueError("供应商成交量无效，未覆盖原缓存")
        session_date=idx.date().isoformat()
        merged[session_date]={"date":session_date,"open":clean(row["Open"]),"high":clean(row["High"]),
            "low":clean(row["Low"]),"close":close,"adjusted_close":adjusted,
            "adjusted_open":str(float(row["Open"])*factor),"adjusted_high":str(float(row["High"])*factor),
            "adjusted_low":str(float(row["Low"])*factor),"volume":int(float(volume)),
            "dividend":clean(row.get("Dividends",0)),"split":clean(row.get("Stock Splits",0)),
            "complete":session_date<=last_complete}
    bars=[merged[k] for k in sorted(merged) if k>=str(target_start)]
    complete=[b for b in bars if b["complete"]]
    if not complete:
        raise ValueError("没有可确认的完整交易日行情，已保留原缓存")
    expected={s.date().isoformat() for s in cal.sessions_in_range(complete[0]["date"],last_complete)}
    available={b["date"] for b in complete}
    missing=sorted(expected-available)
    technical=technical_view(bars)
    payload={"bars":bars,"fetched_at":stamp(),"source":"yfinance","interval":"1d",
        "adjustment":"OHLC为供应商auto_adjust=False字段；技术使用Adj Close等比调整，不宣称逐笔原始成交价",
        "last_complete_session":complete[-1]["date"],"coverage_ok":not missing,"missing_sessions":missing,
        "requested_start":str(target_start),"actual_start":complete[0]["date"],"calendar":"XNYS",
        "technical":technical,"profile":profile,"last_price":complete[-1]["close"]}
    # 原始响应作为独立文件保存，派生区间留在版本快照，不暴露全部日K到前端。
    raw=frame.to_json(orient="table",date_format="iso").encode()
    import hashlib
    path=settings.storage/"documents"/(hashlib.sha256(raw).hexdigest()+".json")
    path.write_bytes(raw)
    payload["raw_file"]=path.name
    with Session() as session:
        security=session.get(Security,security_id)
        security.profile={**security.profile,**profile}
        save_snapshot(session,security_id,"market",payload)
        session.commit()
    return {"cached":False,"sessions":len(bars),"missing_sessions":missing}


def technical_view(bars):
    result=structures(bars)
    complete=[b for b in bars if b.get("complete")]
    if not complete:
        return result
    factor=float(complete[-1]["adjusted_close"])/float(complete[-1]["close"])
    result["quote_conversion_factor"]=factor
    for zone in result.get("zones",[]):
        for key in ("low","high","center"):
            zone[f"quote_{key}"]=zone[key]/factor
    return result


def frame_records(frame):
    if frame is None or frame.empty:
        return []
    return [{"period":str(index),**{str(k):clean(v) for k,v in row.items()}} for index,row in frame.iterrows()]

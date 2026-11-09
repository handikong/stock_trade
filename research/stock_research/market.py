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
CN_SNAPSHOT_KIND = "cn_market"


def snapshot_kind(region):
    return CN_SNAPSHOT_KIND if region == "CN" else "market"


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


def calendar_context(region="US", *, at=None):
    current=pd.Timestamp.now(tz="UTC") if at is None else pd.Timestamp(at).tz_convert("UTC")
    if region == "CN":
        today = current.tz_convert("Asia/Shanghai").date()
        # 沪深股票常规收盘及休市使用上交所日历。超出已公布年份时不按工作日猜测。
        cal = xcals.get_calendar("XSHG")
        if today > cal.bound_max().date():
            raise ValueError("沪深交易日历未覆盖今日，请更新 exchange-calendars")
    else:
        today = current.date()
        cal=xcals.get_calendar("XNYS",start=str(today-timedelta(days=settings.history_years*366+30)),
            end=str(today+timedelta(days=14)))
    session=(cal.last_session if region == "CN" and today >= cal.last_session.date()
             else cal.date_to_session(str(today),direction="previous"))
    if cal.session_close(session)+pd.Timedelta(minutes=settings.post_close_delay_minutes)>current:
        session=cal.previous_session(session)
    return cal,session.date().isoformat()


def cache_state(item, region="US"):
    if not item:
        return {"state":"missing","message":"尚无行情缓存"}
    payload=item.payload
    try:
        _,expected=calendar_context(region)
    except Exception:
        return {"state":"unknown","message":"交易日历暂不可用，不能确认缓存新鲜度"}
    last=payload.get("last_complete_session","")
    if region == "CN":
        # 当日收盘行情要跨夜用于下个交易日 09:00，休市期间也不重复拉相同的日线。
        ready=last==expected and payload.get("coverage_ok",False) and payload.get("calendar")=="XSHG"
    else:
        acquired=datetime.fromisoformat(payload["fetched_at"])
        ttl_ok=(datetime.now(timezone.utc)-acquired).total_seconds()<settings.market_cache_hours*3600
        ready=last>=expected and payload.get("coverage_ok",False) and ttl_ok
    return {"state":"fresh" if ready else "stale","expected_session":expected,
        "last_complete_session":payload.get("last_complete_session"),"fetched_at":payload["fetched_at"],
        "message":"命中完整有效缓存" if ready else "缓存需补齐或复核；旧资料仅供研究参考"}


def _ohlc(row, close, session_date, *, accept_float_noise=False):
    opened, high, low = (clean(row.get(field)) for field in ("Open","High","Low"))
    if any(value is None for value in (opened,high,low)):
        raise ValueError(f"{session_date} 供应商OHLC字段不完整，未覆盖原缓存")
    bottom, top = sorted((opened,close),key=float)
    raw_high, raw_low = high, low
    if accept_float_noise:
        # 只消化单精度报价的浮点尾差，明显越界的日线仍拒绝。
        if 0<float(low)-float(bottom)<=min(float(bottom)*2e-7,1e-4):
            low = bottom
        if 0<float(top)-float(high)<=min(float(top)*2e-7,1e-4):
            high = top
    if not 0<float(low)<=float(bottom)<=float(top)<=float(high):
        raise ValueError(f"{session_date} 供应商OHLC关系不一致，未覆盖原缓存 · "
                         f"开 {opened} 高 {raw_high} 低 {raw_low} 收 {close}")
    return opened,high,low


def refresh_market(security_id: str, force=False, *, region="US"):
    kind = snapshot_kind(region)
    cn = region == "CN"
    timezone_name = "Asia/Shanghai" if cn else "America/New_York"
    with Session() as session:
        old=latest(session,security_id,kind)
        security=session.get(Security,security_id)
        symbol=security.symbol
        old_payload=old.payload if old else None
        if not force and cache_state(old,region)["state"]=="fresh":
            if not technical_is_current(old_payload.get("technical",{})):
                updated={**old_payload,"technical":technical_view(old_payload["bars"]),"technical_calculated_at":stamp()}
                save_snapshot(session,security_id,kind,updated)
                session.commit()
                return {"cached":True,"technical_recomputed":True,"sessions":len(old_payload["bars"])}
            return {"cached":True}
    if cn:
        from .cn_sources import symbol as cn_symbol
        symbol = cn_symbol(symbol,allow_etf=True)
        if symbol.endswith(".SH"):
            symbol = symbol[:-3]+".SS"
    ticker=yf.Ticker(symbol)
    # 提供交易时区，避免 SDK 探测失败后回退到 info 查询；稍后核对日线响应。
    if not hasattr(ticker,"_tz"):
        raise ValueError("当前yfinance版本不兼容无身份查询适配器，请使用项目约束的0.2.x版本")
    ticker._tz=timezone_name
    cal,last_complete=calendar_context(region)
    today = pd.Timestamp.now(tz=timezone_name).date() if cn else date.today()
    target_start=today-timedelta(days=settings.history_years*366)
    # 正常补齐近期缺口；遇企业行动则重新取完整窗口，防止新旧调整口径拼接。
    start=target_start
    if old_payload and old_payload.get("bars") and not force:
        start=max(target_start,date.fromisoformat(old_payload["bars"][-1]["date"])-timedelta(days=14))
        if old_payload.get("missing_sessions"):
            start=min(start,date.fromisoformat(old_payload["missing_sessions"][0])-timedelta(days=7))
        if old_payload.get("requested_start",str(target_start))>str(target_start):
            start=target_start
    # Yahoo 的 end 为开区间。沪深只取最近完整收盘之前的日线，盘中行无需写入缓存。
    history_end = {"end":str(date.fromisoformat(last_complete)+timedelta(days=1))} if cn else {}
    def history(begin):
        return yahoo_call(security_id,"history(日线)",lambda:ticker.history(start=str(begin),interval="1d",
            auto_adjust=False,back_adjust=False,actions=True,repair=False,prepost=False,raise_errors=True,**history_end))
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
    if info.get("exchangeTimezoneName")!=timezone_name:
        raise ValueError(f"日线响应缺少有效{'沪深' if cn else '美股'}时区，已保留原缓存；没有追加身份查询")
    profile={"name":info.get("longName") or info.get("shortName") or security.profile.get("sec_name") or symbol,
        "type":info.get("instrumentType") or "UNKNOWN","currency":info.get("currency"),
        "exchange":info.get("fullExchangeName") or info.get("exchangeName"),
        "market_metadata_source":"日线响应附带字段","market_metadata_at":stamp()}
    if not profile["currency"]:
        raise ValueError(f"日线响应缺少币种，不能假设为{'CNY' if cn else 'USD'}；已保留原缓存，没有追加身份查询")
    if cn and profile["currency"]!="CNY":
        raise ValueError("沪深日线币种与人民币不一致，已保留原缓存")
    # 增量缺失不能删除旧窗口中的有效记录；完整重取时则不混入旧调整价。
    merged={b["date"]:b for b in (old_payload or {}).get("bars",[])} if start>target_start else {}
    if cn:
        merged={day:bar for day,bar in merged.items() if bar.get("complete") is True and day<=last_complete}
    for idx,row in frame.iterrows():
        session_date=idx.date().isoformat()
        if cn and (session_date>last_complete or not cal.is_session(session_date)):
            continue
        close=clean(row.get("Close")); adjusted=clean(row.get("Adj Close"))
        if close is None or adjusted is None or float(close)<=0 or float(adjusted)<=0:
            raise ValueError(f"{session_date} 供应商价格存在无效数值，未覆盖原缓存")
        factor=float(adjusted)/float(close)
        opened,high,low = _ohlc(row,close,session_date,accept_float_noise=cn)
        volume=clean(row.get("Volume"))
        if volume is None or float(volume)<0:
            raise ValueError(f"{session_date} 供应商成交量无效，未覆盖原缓存")
        merged[session_date]={"date":session_date,"open":opened,"high":high,
            "low":low,"close":close,"adjusted_close":adjusted,
            "adjusted_open":str(float(opened)*factor),"adjusted_high":str(float(high)*factor),
            "adjusted_low":str(float(low)*factor),"volume":int(float(volume)),
            "dividend":clean(row.get("Dividends",0)),"split":clean(row.get("Stock Splits",0)),
            "complete":session_date<=last_complete}
    bars=[merged[k] for k in sorted(merged) if k>=str(target_start)]
    complete=[b for b in bars if b["complete"]]
    if not complete:
        raise ValueError("没有可确认的完整交易日行情，已保留原缓存")
    if cn and old_payload and complete[-1]["date"]<old_payload.get("last_complete_session",""):
        raise ValueError("本次沪深日线早于已缓存收盘，已保留原缓存")
    expected={s.date().isoformat() for s in cal.sessions_in_range(complete[0]["date"],last_complete)}
    available={b["date"] for b in complete}
    missing=sorted(expected-available)
    technical=technical_view(bars)
    payload={"bars":bars,"fetched_at":stamp(),"source":"yfinance","interval":"1d",
        "adjustment":"OHLC为供应商auto_adjust=False字段；技术使用Adj Close等比调整，不宣称逐笔原始成交价",
        "last_complete_session":complete[-1]["date"],"coverage_ok":not missing,"missing_sessions":missing,
        "requested_start":str(target_start),"actual_start":complete[0]["date"],"calendar":cal.name,
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
        save_snapshot(session,security_id,kind,payload)
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

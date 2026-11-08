"""缓存公开公司市值，供美股和 A 股扫描名单排序。"""
from datetime import datetime, timedelta
import re

import httpx

from .stockanalysis_mg7 import ORIGIN, USER_AGENT, Reader, _number, _tables
from .cn_sources import BOARDS
from .store import Session, WatchlistState, now

STATE_ID = "us-stock-market-caps"
SOURCE_URL = ORIGIN + "/stocks/"
JOB_KIND = "watchlist_market_caps"
CN_STATE_ID = "cn-stock-market-caps"
CN_JOB_KIND = "cn_watchlist_market_caps"
CN_LISTS = {
    "SH":("沪市","Shanghai Stock Exchange",ORIGIN+"/list/shanghai-stock-exchange/"),
    "SZ":("深市","Shenzhen Stock Exchange",ORIGIN+"/list/shenzhen-stock-exchange/"),
}
MULTIPLIERS = {"K":10**3, "M":10**6, "B":10**9, "T":10**12}


def cached(session, region="US"):
    row = session.get(WatchlistState,CN_STATE_ID if region=="CN" else STATE_ID)
    return row.seed if row else {}


def _page(reader, url, exchange=None):
    page = reader.page(url)
    if exchange is not None:
        heading = page.find("h1")
        if (heading is None or heading.get_text(" ",strip=True)!=f"{exchange} Stocks"
                or not any("Values are in Chinese yuan (CNY)" in p.get_text(" ",strip=True) for p in page.find_all("p"))):
            raise ValueError(f"{exchange} 市值列表的市场或人民币口径无法确认，旧市值保留")
    total = None
    for heading in page.find_all(("h1", "h2", "h3")):
        match = re.fullmatch(r"([\d,]+) Stocks", heading.get_text(" ", strip=True))
        if match:
            total = int(match[1].replace(",", ""))
            break
    header = ["No.","Symbol","Company Name","Market Cap"] if exchange is not None else ["Symbol","Company Name","Industry","Market Cap"]
    symbol_column = 1 if exchange is not None else 0
    for rows in _tables(page):
        if rows[0][:4] != header:
            continue
        records = {}
        for cells in rows[1:]:
            if len(cells) < 4 or not cells[symbol_column]:
                raise ValueError("市值列表的列结构无效，旧市值保留")
            code, text = cells[symbol_column].replace(".", "-"), cells[3]
            if code in records:
                raise ValueError("市值列表出现重复代码，旧市值保留")
            unit = text[-1:]
            multiple = MULTIPLIERS.get(unit, 1)
            value = _number(text[:-1] if unit in MULTIPLIERS else text)
            records[code] = format(value * multiple, "f") if value is not None and value > 0 else None
        if total and records:
            return total, records
        break
    raise ValueError("未能读取公开公司市值表，旧市值保留")


def _collect(reader, url, progress, *, label, begin, end, exchange=None):
    progress(begin,f"正在读取公开{label}市值列表")
    total, records = _page(reader,url,exchange)
    page_size = len(records)
    pages = (total + page_size - 1) // page_size
    if not 1 <= pages <= 24:
        raise ValueError("市值列表页数异常，旧市值保留")
    for index in range(2, pages + 1):
        progress(begin + (end-begin)*(index-1)//pages,f"正在读取{label}市值第 {index}/{pages} 页")
        count, batch = _page(reader,url+f"?page={index}",exchange)
        if count != total or len(batch) != min(page_size,total-len(records)) or records.keys() & batch.keys():
            raise ValueError("市值列表分页不完整或重复，旧市值保留")
        records.update(batch)
    if len(records)!=total:
        raise ValueError("市值列表数据不完整，旧市值保留")
    return records


def refresh(progress, *, region="US"):
    cn = region=="CN"
    state_id = CN_STATE_ID if cn else STATE_ID
    with Session() as session:
        prior = cached(session,region)
    if prior and now() - datetime.fromisoformat(prior["fetched_at"]) < timedelta(hours=24):
        return {"message":"已复用最近 24 小时内的公司市值缓存"}
    with httpx.Client(timeout=18, follow_redirects=False, headers={
            "User-Agent":USER_AGENT, "Accept":"text/html, text/plain"}) as client:
        reader = Reader(client)
        if cn:
            values = {}
            for venue,(label,exchange,url) in CN_LISTS.items():
                begin = 5 if venue=="SH" else 50
                records = _collect(reader,url,progress,label=label,begin=begin,end=begin+45,exchange=exchange)
                batch = {f"{code}.{venue}":value for code,value in records.items()
                         if value is not None and re.fullmatch(BOARDS[venue],code)}
                if not batch:
                    raise ValueError(f"{label}市值数据无有效沪深股票，旧市值保留")
                values.update(batch)
        else:
            records = _collect(reader,SOURCE_URL,progress,label="美股",begin=5,end=95)
            values = {code:value for code,value in records.items()
                      if value is not None and re.fullmatch(r"[A-Z][A-Z0-9\-]{0,14}",code)}
            if not values:
                raise ValueError("市值列表数据不完整，旧市值保留")
    with Session() as session:
        row = session.get(WatchlistState,state_id)
        payload = {"source":"Stock Analysis", "fetched_at":now().isoformat(), "values":values}
        if row is None:
            session.add(WatchlistState(id=state_id,seed=payload))
        else:
            row.seed = payload
        session.commit()
    return {"message":"公司市值缓存已更新，扫描名单可按市值排序"}

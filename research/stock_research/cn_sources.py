"""沪深 A 股人气榜及人民币估值资料，独立于美股来源。"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from json import JSONDecodeError
import logging
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from .stockanalysis_mg7 import ORIGIN, USER_AGENT, Reader, StopFetch, _forecast, _number, _ratios, _tables
from .store import Session, WatchlistState, stamp
from .watchlist import write_lock

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
RANK_PAGE = "https://guba.eastmoney.com/rank/"
RANK_API = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
QUOTE_API = "https://push2.eastmoney.com/api/qt/ulist.np/get"
DELAY_QUOTE_API = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
COUNT = 100
POOL_ID = "cn-eastmoney-hot-pool"
BOARDS = {"SH":r"(?:60[0135]|688)[0-9]{3}", "SZ":r"(?:00[0-3]|30[01])[0-9]{3}"}
EXCHANGES = {"SH":"sha", "SZ":"she"}
RISK_NAME = re.compile(r"^(?:S?\*?ST|退市)|退$", re.IGNORECASE)


def symbol(value):
    text = value.strip().upper()
    match = re.fullmatch(r"([0-9]{6})(?:\.(SH|SS|SZ))?", text)
    if match:
        code, suffix = match.groups()
        suffix = "SH" if suffix == "SS" else suffix
    else:
        match = re.fullmatch(r"(SH|SZ)([0-9]{6})", text)
        if not match:
            raise ValueError("请输入沪深 A 股代码，例如 600519.SH 或 300750.SZ")
        suffix, code = match.groups()
    venue = next((key for key, pattern in BOARDS.items() if re.fullmatch(pattern,code)), None)
    if venue is None or (suffix and suffix != venue):
        raise ValueError("仅支持沪深普通 A 股，代码和交易所需一致")
    return f"{code}.{venue}"


def _rank_rows(payload):
    items = payload.get("data") if isinstance(payload,dict) else None
    if not isinstance(items,list) or len(items) != COUNT:
        raise ValueError("东方财富未返回完整的人气榜前 100 条，A 股关注池保留")
    ranks = {}
    seen = set()
    for item in items:
        raw = item.get("sc") if isinstance(item,dict) else None
        rank = item.get("rk") if isinstance(item,dict) else None
        if (not isinstance(raw,str) or not re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}",raw)
                or type(rank) is not int or not 1 <= rank <= COUNT or rank in ranks or raw in seen):
            raise ValueError("东方财富榜单排名或股票代码异常，A 股关注池保留")
        ranks[rank] = raw
        seen.add(raw)
    return sorted(ranks.items())


def _delay_quote_redirect(response):
    request = response.next_request
    if request is None or request.method != "GET" or response.status_code not in (301,302,307,308):
        return False
    target = request.url
    expected = httpx.URL(DELAY_QUOTE_API)
    return (target.scheme == "https" and target.host == expected.host and target.path == expected.path
            and target.port in (None,443) and not target.userinfo)


def fetch_hot_list():
    headers = {"Accept":"application/json", "Referer":RANK_PAGE, "User-Agent":"StockTradeResearch/0.1"}
    operation = "人气榜"
    try:
        with httpx.Client(timeout=15,follow_redirects=False,headers=headers) as client:
            response = client.post(RANK_API,json={"appId":"appId01","globalId":str(uuid4()),
                                                   "marketType":"","pageNo":1,"pageSize":COUNT})
            response.raise_for_status()
            ranks = _rank_rows(response.json())
            candidates, excluded = [], []
            for rank, raw in ranks:
                try:
                    code = symbol(raw)
                except ValueError:
                    excluded.append({"rank":rank,"source_symbol":raw,"reason":"沪深普通 A 股之外"})
                    continue
                market = "1" if code.endswith(".SH") else "0"
                candidates.append((rank,raw,code,market))
            if not candidates:
                raise ValueError("人气榜没有可确认的沪深股票，A 股关注池保留")
            operation = "批量报价"
            params = {"secids":",".join(f"{venue}.{code[:6]}" for _,_,code,venue in candidates),
                      "fields":"f12,f13,f14,f20","fltt":2,"invt":2}
            response = client.get(QUOTE_API,params=params)
            if _delay_quote_redirect(response):
                logger.info("东方财富批量报价 HTTP %s，继续读取同一接口的延时行情",response.status_code)
                response = client.get(DELAY_QUOTE_API,params=params)
            response.raise_for_status()
            quote = response.json()
    except httpx.HTTPStatusError as exc:
        response = exc.response
        destination = response.next_request
        note = (f"，跳转目标 {destination.url.host} {destination.url.path[:100]!r}" if destination else
                "，未提供可用的跳转目标" if response.is_redirect else "")
        raise ValueError(f"东方财富{operation}返回 HTTP {response.status_code}{note}，A 股关注池保留") from None
    except httpx.HTTPError:
        raise ValueError(f"东方财富{operation}网络请求失败，A 股关注池保留") from None
    except JSONDecodeError:
        raise ValueError(f"东方财富{operation}未返回有效 JSON，A 股关注池保留") from None
    data = quote.get("data") if isinstance(quote,dict) else None
    quotes = data.get("diff") if isinstance(data,dict) else None
    if isinstance(quotes,dict):
        quotes = list(quotes.values())
    if not isinstance(quotes,list):
        raise ValueError("东方财富未返回对应股票简称，A 股关注池保留")
    lookup = {(str(row.get("f13")),row.get("f12")):row for row in quotes if isinstance(row,dict)
              and isinstance(row.get("f12"),str)}
    stocks = []
    for rank, raw, code, venue in candidates:
        row = lookup.get((venue,code[:6]),{})
        name = row.get("f14")
        if not isinstance(name,str) or name.strip() in ("","-"):
            raise ValueError(f"{code} 缺少股票简称，无法核对风险警示，A 股关注池保留")
        name = name.strip()
        if RISK_NAME.search(name.replace("＊","*").replace(" ","")):
            excluded.append({"rank":rank,"source_symbol":raw,"reason":"风险警示或退市整理"})
            continue
        cap = _number(str(row.get("f20")))
        stocks.append({"symbol":code,"source_symbol":raw,"rank":rank,"name":name[:100],
                       "market_cap_cny":format(cap,"f") if cap is not None and cap > 0 else None})
    if not stocks:
        raise ValueError("人气榜无合格沪深股票，A 股关注池保留")
    return {"source":"东方财富 A 股人气榜","url":RANK_PAGE,"fetched_at":stamp(),
            "quote_host":response.url.host,"candidate_count":COUNT,"stocks":stocks,
            "excluded":sorted(excluded,key=lambda row:row["rank"])}


def pool(session):
    state = session.get(WatchlistState,POOL_ID)
    return state.seed if state else {"market":"CN","currency":"CNY","members":{},"last_date":None}


def sync_hot_list(day=None, *, allowed=lambda: True):
    day = day or datetime.now(BEIJING).date().isoformat()
    key = "cn-hot:" + day
    with Session() as session:
        archived = session.get(WatchlistState,key)
        if archived:
            return {**archived.seed,"cached":True}
    if not allowed():
        raise ValueError("A 股人气榜任务已过窗口或停用")
    report = fetch_hot_list()
    if datetime.fromisoformat(report["fetched_at"]).astimezone(BEIJING).date().isoformat() != day:
        raise ValueError("抓取跨过北京时间日期，未修改 A 股关注池")
    with write_lock(), Session() as session:
        archived = session.get(WatchlistState,key)
        if archived:
            return {**archived.seed,"cached":True}
        if not allowed():
            raise ValueError("A 股人气榜任务已过窗口或停用，未修改关注池")
        current = pool(session)
        if current["last_date"] and current["last_date"] > day:
            raise ValueError("已有更新的 A 股名单，未回写旧榜单")
        members = dict(current["members"])
        added = []
        for row in report["stocks"]:
            code = row["symbol"]
            first = members.get(code,{}).get("first_seen",day)
            if code not in members:
                added.append(code)
            members[code] = {**row,"first_seen":first,"last_seen":day}
        payload = {**current,"members":members,"last_date":day}
        state = session.get(WatchlistState,POOL_ID)
        if state:
            state.seed = payload
        else:
            session.add(WatchlistState(id=POOL_ID,seed=payload))
        result = {**report,"date":day,"added":added,"active_count":len(members),"cached":False}
        session.add(WatchlistState(id=key,seed=result))
        session.commit()
    return result


def _annual_pe(page):
    for rows in _tables(page):
        header = rows[0]
        fields = {row[0]:row[1:] for row in rows[1:]}
        if header[0] != "Fiscal Year" or not ("PE Ratio" in fields or "Forward PE" in fields):
            continue
        labels = header[1:]
        result = {}
        for label, key in (("PE Ratio","trailing_pe"),("Forward PE","forward_pe")):
            numbers = fields.get(label,[])
            if len(numbers) != len(labels):
                continue
            result[key] = {period:str(value) for period,raw in zip(labels,numbers)
                           if (value := _number(raw)) is not None and value > 0}
        if result:
            return result
    raise ValueError("未找到公开年度 PE 表")


def research(value):
    code = symbol(value)
    ticker, venue = code.split(".")
    base = f"{ORIGIN}/quote/{EXCHANGES[venue]}/{ticker}"
    as_of = datetime.now(BEIJING).date()
    urls = {"eps":base+"/forecast/","annual_pe":base+"/financials/ratios/",
            "quarterly_forward_pe":base+"/financials/ratios/?p=quarterly"}
    result = {"symbol":code,"currency":"CNY","as_of":as_of.isoformat(),"source":"Stock Analysis",
              "urls":urls,"warnings":[],"scenarios":[]}
    with httpx.Client(timeout=18,follow_redirects=False,headers={
            "User-Agent":USER_AGENT,"Accept":"text/html, text/plain"}) as client:
        reader = Reader(client)
        for key, parse in (("eps",_forecast),("annual_pe",_annual_pe),
                           ("quarterly_forward_pe",lambda page:_ratios(page,as_of))):
            try:
                parsed = parse(reader.page(urls[key]))
                if key == "eps" and (parsed["currency_per_share"] != "CNY"
                                     or parsed["currency_source"] != "financial_currency_label"):
                    raise ValueError("预测 EPS 未明确标为人民币")
                result[key] = parsed
            except StopFetch as exc:
                result["warnings"].append(str(exc))
                break
            except ValueError as exc:
                result["warnings"].append(f"{key} · {exc}")
    eps, pe = result.get("eps") or {}, result.get("quarterly_forward_pe") or {}
    pairs = [(eps.get(e),pe.get(p)) for e,p in (("low","p25"),("mean","p50"),("high","p75"))]
    if (eps.get("period_end") or "") >= as_of.isoformat() and all(e and p and Decimal(e)>0 for e,p in pairs):
        result["scenarios"] = [{"name":name,"price":str((Decimal(e)*Decimal(p)).quantize(Decimal("0.01"),
                                 rounding=ROUND_HALF_UP))} for name,(e,p) in zip(("保守","基准","乐观"),pairs)]
        result["warnings"].append("财年 EPS × 历史 Forward PE 仅为情景参考，PE 分母口径待核对")
    else:
        result["warnings"].append("未取得完整且有效的人民币 EPS 和季度 Forward PE 三档，估值区间留空")
    return result

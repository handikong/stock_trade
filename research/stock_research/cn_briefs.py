"""A 股大盘与持仓简报，复用公共技术分析和飞书卡片。"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx
from sqlalchemy import select

from . import (band_strategies, cn_holdings, cn_sources, futu_macd, holdings_brief,
               market, market_brief, market_conditions, portfolio_risk)
from .notification_cards import _text
from .price_features import wilder_atr
from .providers import failure_detail
from .store import Security, Session, latest, stamp

BENCHMARKS = {"510300.SH":"沪深300 ETF","159915.SZ":"创业板 ETF"}
NEWS_URL = "https://www.csrc.gov.cn/searchList/a1a078ee0bc54721ab6b148884c784a8"


def _news(first_day, last_day):
    try:
        response = httpx.get(NEWS_URL,timeout=8,follow_redirects=False,headers={"User-Agent":"StockTradeResearchBot/0.1"},
                             params={"_isAgg":"true","_isJson":"true","_pageSize":"18",
                                     "_template":"index","_rangeTimeGte":"","_channelName":"","page":"1"})
        response.raise_for_status()
        source = response.json()["data"]["results"]
        if not isinstance(source,list):
            raise ValueError("官方新闻列表格式异常")
    except (httpx.HTTPError,ValueError,TypeError,KeyError):
        return [],["证监会公开要闻未取得"]
    items = {}
    now = datetime.now(cn_sources.BEIJING)
    for row in source:
        if not isinstance(row,dict):
            continue
        try:
            when = datetime.fromisoformat(row["publishedTimeStr"])
            when = (when.replace(tzinfo=cn_sources.BEIJING) if when.tzinfo is None else
                    when.astimezone(cn_sources.BEIJING))
            url = urljoin(NEWS_URL,row["url"])
            parsed = urlsplit(url)
        except (ValueError,TypeError,KeyError):
            continue
        if (parsed.scheme!="https" or parsed.netloc!="www.csrc.gov.cn"
                or not re.fullmatch(r"/csrc/c\d+/c\d+/content\.shtml",parsed.path)):
            continue
        if first_day <= when.date().isoformat() <= last_day and when<=now:
            title = BeautifulSoup(str(row.get("title") or ""),"html.parser").get_text(" ",strip=True)[:120]
            if title:
                items[url] = {"title":title,"url":url,"published_beijing":when.isoformat()}
    rows = sorted(items.values(),key=lambda item:item["published_beijing"],reverse=True)[:5]
    return [{"id":f"CN{i}",**row} for i,row in enumerate(rows,1)], ([] if rows else ["区间内未取得可核对日期的证监会要闻"])


def market_report(day, target, report_type):
    calendar,_ = market.calendar_context("CN")
    try:
        next_session = calendar.next_session(target).date().isoformat()
    except ValueError:
        next_session = None
    weekly = report_type=="weekly"
    target_day = date.fromisoformat(target)
    since = (target_day-timedelta(days=target_day.weekday())).isoformat() if weekly else target
    news,warnings = _news(since,day)
    benchmarks, market_data = {},{}
    with Session() as session:
        securities = {row.symbol:row.id for row in session.scalars(
            select(Security).where(Security.symbol.in_(BENCHMARKS)))}
    for code, name in BENCHMARKS.items():
        try:
            cached = market_brief._read_bars(securities[code],target,"CN") if code in securities else None
            if cached is None:
                raise ValueError("缺少目标交易日的完整行情")
            bars, provenance = cached
            benchmarks[name] = market_conditions.benchmark(bars,weekly=weekly,shortened=set(),calendar=calendar)
            market_data[name] = provenance
        except (ValueError,KeyError) as exc:
            warnings.append(f"{name} · {failure_detail(exc)['message']}")
    return {"schema":"cn-market-brief-v1","market":"CN","report_type":report_type,"local_date":day,
            "as_of_session":target,"next_session":next_session,"reused_session":True,"prepared_at":stamp(),
            "rules":market_conditions.parameters(),"benchmarks":benchmarks,"market_data":market_data,
            "participation":None,"vix":None,"news":news,"events":[],"warnings":warnings,
            "assessment":market_conditions.assessment(benchmarks,None,None,[],tuple(BENCHMARKS.values()))}


def holdings_report(day, target, scan):
    parameters, strategies = futu_macd.policy(), band_strategies.policy()
    context = {"target":target,"cfg":portfolio_risk.policy(),"shortened":set()}
    buys = {row["symbol"] for row in scan.get("results",[]) if any(
        signal.get("event_kind","BUY")=="BUY" for signal in row.get("current_signals",[]))}
    report = {"local_date":day,"as_of_session":target,"items":[]}
    with Session() as session:
        positions = cn_holdings.positions(session)
        symbols = [row.symbol for row in positions]
        securities = {row.symbol:row.id for row in session.scalars(select(Security).where(Security.symbol.in_(symbols)))}
        for position in positions:
            code = position.symbol
            source = latest(session,securities[code],market.CN_SNAPSHOT_KIND) if code in securities else None
            view, price = None,None
            if source is not None and market.cache_state(source,"CN")["state"]=="fresh" and source.payload["last_complete_session"]==target:
                payload = source.payload
                bars = [bar for bar in payload["bars"] if bar["complete"] and bar["date"]<=target]
                price = Decimal(payload["last_price"])
                factor = float(bars[-1]["adjusted_close"])/float(price)
                atr = wilder_atr(bars,14) if len(bars)>=60 else None
                if atr and atr>0:
                    try:
                        view = holdings_brief._technical({"bars":bars,"price":price,"atr":atr/factor,
                            "factor":factor,"technical":payload["technical"]},context,strategies,parameters)
                    except (ValueError,KeyError,ArithmeticError):
                        view = None
            action = holdings_brief.technical_action(price,position.average_cost,view)
            if action is None:
                action = (("add","收盘触发已有买点，先核对当前支撑、账户资金和组合风险。")
                          if code in buys and view["exit_level"] is not None and view["exit_level"]<2
                          else ("hold","继续观察关键区间；按收盘位置和保护条件复核。"))
            report["items"].append({"symbol":code,"version":position.version,"market_id":source.id if source else None,
                "price":str(price) if price is not None else None,"technical":view,
                "action":action[0],"reason":action[1],"protective_alerts":[],"add":None})
    return report


def holdings_cards(report):
    items = report["items"]
    for start in range(0,len(items),holdings_brief.BATCH_SIZE):
        group = items[start:start+holdings_brief.BATCH_SIZE]
        elements = [{"tag":"markdown","content":f"收盘 {report['as_of_session']} · 价格 CNY · 按日线复核"}]
        for row in group:
            label = holdings_brief.ACTIONS[row["action"]]
            lines = "\n".join(_text(line,240) for line in holdings_brief._detail(row))
            elements.extend([{"tag":"hr"},{"tag":"markdown","content":f"**{row['symbol']} · {label}**\n{lines}"}])
        yield {"schema":"2.0","header":{"template":"blue","title":{"tag":"plain_text","content":"A 股持仓复盘"}},
               "body":{"elements":elements}}

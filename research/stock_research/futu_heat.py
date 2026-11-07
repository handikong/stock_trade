"""富途公开美股热度榜：全量校验后首次换池，后续只追加股票并按日通知。"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from . import feishu, watchlist
from .store import Session, WatchlistEntry, WatchlistState, stamp

logger = logging.getLogger(__name__)
STATE_ID = "futu-heat-watchlist"
ORIGIN = "https://www.futunn.com"
PAGE = ORIGIN + "/quote/us"
ENDPOINT = ORIGIN + "/quote-api/quote-v2/get-heat-list"
COUNT = 100
SHANGHAI = ZoneInfo("Asia/Shanghai")
QUERY = {"sortId":17, "orderBy":1, "marketType":2, "offset":0, "count":COUNT}


def local_day():
    return datetime.now(timezone.utc).astimezone(SHANGHAI).date().isoformat()


def fetch():
    # 使用富途公开行情页提供给匿名访客的会话和查询签名；不读取登录凭据。
    raw = json.dumps({key:str(value) for key,value in QUERY.items()}, separators=(",", ":")).encode()
    prefix = hmac.new(b"quote_web", raw, hashlib.sha512).hexdigest()[:10]
    headers = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
               "Accept-Language":"zh-CN,zh;q=0.9"}
    try:
        with httpx.Client(timeout=15, headers=headers, follow_redirects=False) as client:
            page = client.get(PAGE)
            page.raise_for_status()
            csrf = client.cookies.get("csrfToken")
            if not csrf:
                raise ValueError("富途未提供公开行情会话，未更新股票池")
            response = client.get(ENDPOINT, params=QUERY, headers={
                "Accept":"application/json, text/plain, */*", "Referer":PAGE,
                "futu-x-csrf-token":unquote(csrf),
                "quote-token":hashlib.sha256(prefix.encode()).hexdigest()[:10],
            })
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                raise ValueError("富途未返回有效 JSON，未更新股票池") from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"富途返回 HTTP {exc.response.status_code}，未更新股票池") from None
    except httpx.HTTPError:
        raise ValueError("无法连接富途行情服务，未更新股票池") from None
    return parse_board(payload,fetched_at=stamp())


def parse_board(payload, *, fetched_at):
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise ValueError(f"富途未返回成功榜单（业务码 {code if type(code) is int else '未知'}），未更新股票池")
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != COUNT:
        raise ValueError("富途未返回完整100条，未更新股票池")

    stocks, excluded, seen = [], [], set()
    for rank,item in enumerate(items,1):
        if not isinstance(item,dict) or item.get("marketType") != 2 or item.get("marketLabel") != "US":
            raise ValueError("富途返回的市场或记录格式不符，未更新股票池")
        code = item.get("stockCode")
        kind = item.get("instrumentType")
        if not isinstance(code,str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}",code) or type(kind) is not int:
            raise ValueError("富途返回的证券代码或类型无效，未更新股票池")
        row = {"symbol":code.replace(".", "-"),"source_symbol":code,"rank":rank,
               "name":str(item.get("name") or code)[:100],"instrument_type":kind}
        if kind != 3:
            excluded.append({**row,"reason":"ETF或其他非股票"})
        elif row["symbol"] in seen:
            excluded.append({**row,"reason":"重复股票代码"})
        else:
            stocks.append(row)
            seen.add(row["symbol"])
    if not stocks:
        raise ValueError("富途榜单没有可确认的美股，未清空或修改股票池")
    return {"source":ENDPOINT,"query":QUERY,"fetched_at":fetched_at,"candidate_count":len(items),
            "stocks":stocks,"excluded":excluded}


def _day_id(day):
    return "futu-heat:" + day


def ready():
    with Session() as db:
        return db.get(WatchlistState,STATE_ID) is not None


def saved(day=None):
    with Session() as db:
        if day is None:
            state = db.get(WatchlistState,STATE_ID)
            if not state:
                return None
            day = state.seed["last_date"]
        row = db.get(WatchlistState,_day_id(day))
        return row.seed if row else None


def apply(board, day, *, allowed):
    with watchlist.write_lock(), Session() as db:
        previous = db.get(WatchlistState,_day_id(day))
        if previous:
            return previous.seed
        state = db.get(WatchlistState,STATE_ID)
        if not allowed() or (state and state.seed["last_date"] > day):
            raise ValueError("富途榜单已不在本次更新窗口，未修改股票池")
        index = db.get(WatchlistState,watchlist.UNIVERSE_STATE_ID)
        overrides = index.seed.get("manual_overrides",{}) if index else {}
        entries = {row.symbol:row for row in db.scalars(select(WatchlistEntry))}
        old_active = {code for code,row in entries.items() if row.active}
        candidates = [row for row in board["stocks"] if overrides.get(row["symbol"]) is not False]
        if not candidates:
            raise ValueError("富途榜单股票均已手动移出，未清空或修改股票池")
        selected = {row["symbol"] for row in candidates}
        initial = state is None
        removed = sorted(old_active - selected) if initial else []
        for code in removed:
            entries[code].active = False
            entries[code].updated_at = stamp()
        added = []
        for item in candidates:
            code = item["symbol"]
            member = entries.get(code)
            if member is None:
                db.add(WatchlistEntry(symbol=code,active=True))
            elif not member.active:
                member.active = True
                member.updated_at = stamp()
            if code not in old_active:
                added.append(item)
        if not db.get(WatchlistState,"default"):
            db.add(WatchlistState(id="default",seed={"version":"futu-heat-v1","symbols":[]}))
        report = {**board,"date":day,"initial":initial,"added":added,"removed":removed,
                  "blocked":[row for row in board["stocks"] if row["symbol"] not in selected],
                  "active_count":len((old_active - set(removed)) | selected),
                  "delivery":{"status":"PENDING" if added else "NO_ADDITIONS"}}
        db.add(WatchlistState(id=_day_id(day),seed=report))
        if state:
            state.seed = {"last_date":day}
        else:
            db.add(WatchlistState(id=STATE_ID,seed={"last_date":day}))
        db.commit()
        return report


def _card(report):
    codes = [row["symbol"] for row in report["added"]]
    chunks = ["、".join(codes[i:i+12]) for i in range(0,len(codes),12)]
    title = "富途热榜 · 股票池首次重建" if report["initial"] else "富途热榜 · 今日新增股票"
    extra = f"首次重建移出 {len(report['removed'])} 只旧股票。\n" if report["initial"] else ""
    text = (f"北京时间 {report['date']}｜热榜前{COUNT}条\n"
            f"当前关注 {report['active_count']} 只。{extra}\n"
            f"较更新前新增 {len(codes)} 只：\n" + ("\n".join(chunks) or "无"))
    return {"header":{"template":"blue","title":{"tag":"plain_text","content":title}},
            "elements":[{"tag":"div","text":{"tag":"plain_text","content":text}}]}


def send_notice(report, *, allowed):
    if report["delivery"]["status"] != "PENDING":
        return report
    try:
        feishu.destination()
    except ValueError as exc:
        return {**report,"delivery":{"status":"NOT_CONFIGURED","message":str(exc)}}
    day = report["date"]
    with watchlist.write_lock(), Session() as db:
        row = db.get(WatchlistState,_day_id(day))
        report = row.seed
        if report["delivery"]["status"] != "PENDING" or not allowed():
            return report
        report = {**report,"delivery":{"status":"SENDING","started_at":stamp()}}
        row.seed = report
        db.commit()
    card = None
    try:
        card = _card(report)
        feishu.send(card)
        delivery = {"status":"SENT","message":"飞书确认接收"}
    except feishu.DeliveryFailure as exc:
        delivery = {"status":exc.status,"message":str(exc)}
    except Exception:
        delivery = {"status":"UNKNOWN","message":"无法确认飞书送达，不自动重发"}
    with watchlist.write_lock(), Session() as db:
        row = db.get(WatchlistState,_day_id(day))
        report = {**row.seed,"delivery":{**row.seed["delivery"],**delivery,"card":card,"finished_at":stamp()}}
        row.seed = report
        db.commit()
    return report


def sync(day, *, send, allowed):
    report = saved(day)
    if report is None:
        if not allowed():
            raise ValueError("富途热榜更新窗口已结束")
        report = apply(fetch(),day,allowed=allowed)
    return send_notice(report,allowed=allowed) if send else report

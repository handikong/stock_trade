"""独立的人民币收息关注池，用户增删与宽基目录互相独立。"""
from datetime import date, datetime
from io import BytesIO
import json
import re

import httpx
import pandas as pd

from . import cn_sources
from .config import ROOT
from .store import Session, WatchlistState, stamp
from .watchlist import write_lock

STATE_ID = "cn-income-watchlist"
ETF_CATALOG = ROOT / "config" / "cn-income-etfs.json"
DIVIDEND_FILE = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000922cons.xls"
RISK_NAME = re.compile(r"^(?:S?\*?ST|退市)|退$", re.IGNORECASE)


def _catalog():
    try:
        entries = json.loads(ETF_CATALOG.read_text(encoding="utf-8"))
        return {code:{"symbol":code,"kind":"broad_etf","name":row["name"],
                      "benchmark":row["benchmark"],"source":row["source"]}
                for code,row in entries.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("A 股宽基 ETF 目录无法读取，请核对 cn-income-etfs.json") from None


def _etf_code(value, catalog):
    text = value.strip().upper().replace(".SS",".SH")
    if text in catalog:
        return text
    if re.fullmatch(r"[0-9]{6}",text):
        matches = [code for code in catalog if code.split(".")[0] == text]
        if len(matches) == 1:
            return matches[0]
    raise ValueError("该基金未列入已核对跟踪指数的 A 股宽基 ETF 目录")


def _payload(session):
    state = session.get(WatchlistState,STATE_ID)
    return state.seed if state else {}


def _members(payload, catalog):
    members = {**catalog, **payload.get("dividend_candidates",{})}
    for code, row in payload.get("overrides",{}).items():
        if row is None:
            members.pop(code,None)
        elif row["kind"] == "dividend":
            members[code] = row
        elif code in catalog:
            members[code] = catalog[code]
    return members


def _view(payload, catalog):
    return {"market":"CN","currency":"CNY",
            "members":sorted(_members(payload,catalog).values(),key=lambda row:(row["kind"],row["symbol"])),
            "etf_catalog":list(catalog.values()),"updated_at":payload.get("updated_at"),
            "dividend_source":payload.get("dividend_source")}


def read(session):
    return _view(_payload(session),_catalog())


def _save(session, payload):
    state = session.get(WatchlistState,STATE_ID)
    if state is None:
        session.add(WatchlistState(id=STATE_ID,seed=payload))
    else:
        state.seed = payload


def add(value, kind, name=""):
    catalog = _catalog()
    if kind == "broad_etf":
        code = _etf_code(value,catalog)
        row = catalog[code]
    elif kind == "dividend":
        code = cn_sources.symbol(value)
        name = name.strip()
        if RISK_NAME.search(name.replace("＊","*").replace(" ","")):
            raise ValueError("收息池不接受 ST 或退市整理股票")
        row = {"symbol":code,"name":name or code,"kind":"dividend","basis":"人工关注"}
    else:
        raise ValueError("收息关注池仅支持高息股和大盘宽基 ETF")
    with write_lock(), Session() as session:
        payload = _payload(session)
        existing = _members(payload,catalog).get(code)
        if kind == "dividend" and not name and existing:
            row["name"] = existing["name"]
        if existing == row:
            return _view(payload,catalog)
        overrides = {**payload.get("overrides",{}),code:row}
        changed = {**payload,"overrides":overrides,"updated_at":stamp()}
        _save(session,changed)
        session.commit()
        return _view(changed,catalog)


def remove(code):
    catalog = _catalog()
    with write_lock(), Session() as session:
        payload = _payload(session)
        if code not in _members(payload,catalog):
            raise LookupError("该证券不在 A 股收息关注池")
        overrides = {**payload.get("overrides",{}),code:None}
        changed = {**payload,"overrides":overrides,"updated_at":stamp()}
        _save(session,changed)
        session.commit()
        return _view(changed,catalog)


def _excel_digits(value, width):
    match = re.fullmatch(r"([0-9]{1," + str(width) + r"})(?:\.0+)?",str(value).strip())
    return match[1].zfill(width) if match else None


def _index_date(value):
    raw = str(value).strip()
    digits = _excel_digits(raw,8)
    if digits:
        raw = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    try:
        return date.fromisoformat(raw.split(" ",1)[0]).isoformat()
    except ValueError:
        raise ValueError("中证红利成分文件日期无法核对，原名单保留") from None


def _dividend_rows(content):
    if len(content) > 5_000_000 or not content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise ValueError("中证红利来源未返回有效 XLS，原名单保留")
    try:
        frame = pd.read_excel(BytesIO(content),engine="xlrd",dtype=str,keep_default_na=False)
    except ImportError:
        raise ValueError("缺少读取中证成分 XLS 的依赖，请在服务器重新执行 bash deploy.sh") from None
    except Exception:
        raise ValueError("中证红利成分文件无法读取，原名单保留") from None
    headers = [re.sub(r"\s+","",str(column)).replace("成份","成分") for column in frame.columns]
    expected = {0:"日期",1:"指数代码",4:"成分券代码",5:"成分券名称",7:"交易所"}
    if (len(frame) != 100 or len(headers) != 9
            or any(not headers[i].startswith(label) for i,label in expected.items())):
        raise ValueError("中证红利成分文件不是完整 100 条的预期格式，原名单保留")
    dates, seen, candidates = set(), set(), {}
    for row in frame.itertuples(index=False,name=None):
        day, index, _, _, number, name, _, venue, _ = row
        dates.add(_index_date(day))
        code = _excel_digits(number,6)
        name = name.strip()
        if _excel_digits(index,6) != "000922" or not code or code in seen or not name:
            raise ValueError("中证红利指数或成分代码校验失败，原名单保留")
        seen.add(code)
        exchange = "SH" if "上海" in venue else "SZ" if "深圳" in venue else None
        if exchange is None:
            raise ValueError("中证红利成分交易所无法核对，原名单保留")
        try:
            key = cn_sources.symbol(f"{code}.{exchange}")
        except ValueError:
            raise ValueError("中证红利成分代码与交易所不一致，原名单保留") from None
        if RISK_NAME.search(name.replace("＊","*").replace(" ","")):
            continue
        candidates[key] = {"symbol":key,"name":name[:100],"kind":"dividend","basis":"中证红利成分"}
    today = datetime.now(cn_sources.BEIJING).date().isoformat()
    if len(dates) != 1 or max(dates) > today or not candidates:
        raise ValueError("中证红利成分日期或沪深候选异常，原名单保留")
    return candidates,dates.pop()


def refresh_dividends():
    catalog = _catalog()
    try:
        response = httpx.get(DIVIDEND_FILE,timeout=25,follow_redirects=False)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"中证红利成分下载返回 HTTP {exc.response.status_code}，原名单保留") from None
    except httpx.HTTPError:
        raise ValueError("中证红利成分下载失败，原名单保留") from None
    candidates, as_of = _dividend_rows(response.content)
    with write_lock(), Session() as session:
        payload = _payload(session)
        previous = payload.get("dividend_source") or {}
        if previous.get("as_of","") > as_of:
            raise ValueError("下载的成分表早于本地记录，原名单保留")
        before = _members(payload,catalog)
        updated = stamp()
        changed = {**payload,"dividend_candidates":candidates,"updated_at":updated,
                   "dividend_source":{"name":"中证红利（000922）","as_of":as_of,"url":DIVIDEND_FILE}}
        after = _members(changed,catalog)
        _save(session,changed)
        session.commit()
    added = len(after.keys() - before.keys())
    return {**_view(changed,catalog),
            "message":f"中证红利候选已更新 · {len(candidates)} 只符合池范围 · 新增关注 {added} 只"}

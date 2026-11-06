"""读取美股盘后成交股数榜，将合格榜单的成员累积到活动关注池。"""
from datetime import datetime, timezone
import logging
import re
from zoneinfo import ZoneInfo

import yfinance as yf
from sqlalchemy import select

from . import watchlist
from .providers import yahoo_call
from .store import Session, WatchlistEntry, WatchlistState, save_snapshot, stamp

LIMIT = 50
CANDIDATE_LIMIT = 100
MARKET_ZONE = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def fetch(anchor_id, target):
    # Yahoo预设most_actives有限市值，使用自定义美国股票筛选，按当日成交股数排序。
    query = yf.EquityQuery("eq", ["region", "us"])
    data = yahoo_call(anchor_id, "美股成交量前50",
        lambda: yf.screen(query, offset=0, size=CANDIDATE_LIMIT, sortField="dayvolume", sortAsc=False))
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(quotes, list):
        raise ValueError("Yahoo 未返回成交量榜列表")
    if len(quotes) < LIMIT:
        raise ValueError(f"Yahoo 只返回 {len(quotes)} 条候选，不足 {LIMIT} 只")

    fetched = datetime.now(timezone.utc)
    rows = []
    seen = {}
    skipped = {"duplicates":[], "other_market":[], "other_type":[]}
    for quote in quotes:
        if not isinstance(quote, dict):
            raise ValueError("Yahoo 成交量榜记录格式无效")
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
            raise ValueError("成交量榜含无效证券代码，不更新关注列表")
        if quote.get("quoteType") != "EQUITY":
            skipped["other_type"].append(symbol)
            continue
        # region=us 已限定请求范围，市场字段缺失不等同明确标为其他市场。
        quote_market = quote.get("market")
        if quote_market not in (None, "", "us_market"):
            skipped["other_market"].append({"symbol":symbol, "market":ascii(quote_market)[:80]})
            continue
        volume = quote.get("regularMarketVolume")
        quote_time = quote.get("regularMarketTime")
        if type(volume) is not int or volume <= 0 or type(quote_time) is not int:
            raise ValueError(f"Yahoo 榜单中的 {symbol} 缺少有效成交股数或常规时段报价时间")
        if symbol in seen:
            if seen[symbol] != (volume, quote_time):
                raise ValueError(f"Yahoo 榜单中的 {symbol} 重复记录成交量或报价时间冲突")
            skipped["duplicates"].append(symbol)
            continue
        seen[symbol] = (volume, quote_time)
        try:
            instant = datetime.fromtimestamp(quote_time, timezone.utc)
        except (ValueError, OSError, OverflowError):
            raise ValueError("成交量榜报价时间无效") from None
        actual_session = instant.astimezone(MARKET_ZONE).date().isoformat()
        if instant > fetched:
            raise ValueError("成交量榜报价时间在未来")
        if actual_session != target:
            raise ValueError(f"Yahoo 返回 {symbol} 的 {actual_session} 美东报价，目标完整交易日为 {target}；未采用该榜")
        if rows and volume > rows[-1]["volume"]:
            raise ValueError("成交量榜的返回顺序与成交股数不一致，不更新关注列表")
        rows.append({"symbol":symbol, "volume":volume, "quote_at":instant.isoformat()})
        if len(rows) == LIMIT:
            break

    counts = (f"重复记录 {len(skipped['duplicates'])} 条，市场标识不匹配 "
              f"{len(skipped['other_market'])} 条，非股票 {len(skipped['other_type'])} 条")
    if len(rows) != LIMIT:
        raise ValueError(f"Yahoo 榜单筛选后只有 {len(rows)} 只有效股票（{counts}），不足 {LIMIT} 只")
    if any(skipped.values()):
        logger.info("Yahoo 成交量榜已补齐前%s只；跳过%s", LIMIT, counts)

    rows.sort(key=lambda row: (-row["volume"], row["symbol"]))
    return {"status":"complete", "session":target, "fetched_at":fetched.isoformat(),
            "source":"Yahoo Finance/yfinance EquityQuery(region=us, dayvolume DESC)",
            "skipped_candidates":skipped,
            "rows":[{**row, "rank":i} for i,row in enumerate(rows, 1)]}


def latest_report(target=None):
    with Session() as db:
        if target is not None:
            from .store import Snapshot
            saved = db.scalar(select(Snapshot.payload).where(
                Snapshot.kind == "volume_leaders",
                Snapshot.payload["session"].as_string() == target,
            ).order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))
            return saved
        state = db.get(WatchlistState, watchlist.VOLUME_LEADERS_STATE_ID)
        return state.seed.get("last_report") if state else None


def _membership_policy(db):
    state = db.get(WatchlistState, watchlist.UNIVERSE_STATE_ID)
    index = state.seed if state else {}
    return index.get("manual_overrides", {}), set(index.get("managed_symbols", []))


def _action(member, symbol, overrides, managed):
    if overrides.get(symbol) is False or (member is not None and not member.active and symbol not in managed):
        return "excluded"
    return "added" if member is None or not member.active else "retained"


def preview(expected_session=None):
    """立即请求最近完整交易日的榜单，不保存榜单或更新成员。"""
    from .cli_helpers import storage_ready
    from .market import calendar_context
    from .store import Security

    _, target = calendar_context()
    if expected_session and expected_session != target:
        raise ValueError(f"当前最近完整美股交易日是 {target}，Yahoo 实时榜不能指定其他日期；历史榜请用 leaders --date")
    if not storage_ready("securities", "research_snapshots", "watchlist_entries", "watchlist_state"):
        raise ValueError("请先使用已有研究数据库，未初始化时不执行手动取榜")
    with Session() as db:
        anchor_id = db.scalar(select(Security.id).order_by(Security.symbol).limit(1))
    if anchor_id is None:
        raise ValueError("数据库没有证券记录，无法沿用 Yahoo 限流状态")
    report = fetch(anchor_id, target)
    with Session() as db:
        overrides, managed = _membership_policy(db)
        rows = [{**row, "action":_action(db.get(WatchlistEntry,row["symbol"]),row["symbol"],overrides,managed)}
                for row in report["rows"]]
    return {**report, "preview":True, "rows":rows,
            "would_add":[row["symbol"] for row in rows if row["action"] == "added"]}


def apply(report, *, anchor_id, allowed):
    """榜单、上榜记录及关注池增量同事务提交，防止同日重启重复计数。"""
    with watchlist.write_lock(), Session() as db:
        state = db.get(WatchlistState, watchlist.VOLUME_LEADERS_STATE_ID)
        previous = state.seed if state else {}
        last = previous.get("last_report")
        if last and last["session"] == report["session"]:
            return last
        if last and last["session"] > report["session"]:
            raise ValueError("已保存更新交易日的成交量榜，不回写旧榜")
        if not allowed():
            raise ValueError("盘后窗口已结束或任务停用，未写入成交量榜")
        overrides, managed = _membership_policy(db)
        history = dict(previous.get("by_symbol", {}))
        added, retained, blocked, rows = [], [], [], []
        for row in report["rows"]:
            symbol = row["symbol"]
            member = db.get(WatchlistEntry, symbol)
            action = _action(member, symbol, overrides, managed)
            if action == "excluded":
                blocked.append(symbol)
            elif member is None:
                db.add(WatchlistEntry(symbol=symbol, active=True))
                added.append(symbol)
            elif not member.active:
                # 指数规则自动停用的股票可由榜单恢复；人工移除在上方优先拦截。
                member.active = True
                member.updated_at = stamp()
                added.append(symbol)
            else:
                retained.append(symbol)
            prior = history.get(symbol, {})
            heat = {"first_session":prior.get("first_session", report["session"]),
                    "last_session":report["session"], "last_rank":row["rank"],
                    "last_volume":row["volume"], "appearances":prior.get("appearances", 0) + 1}
            history[symbol] = heat
            rows.append({**row, **heat, "action":action})
        result = {**report, "rows":rows, "added":added, "retained":retained, "blocked":blocked}
        payload = {"last_report":result, "by_symbol":history}
        if state:
            state.seed = payload
        else:
            db.add(WatchlistState(id=watchlist.VOLUME_LEADERS_STATE_ID, seed=payload))
        save_snapshot(db,anchor_id,"volume_leaders",result)
        db.commit()
        return result

"""当前美国指数候选池与流动性导入；不调用财报、身份、模型、策略或推送。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import logging
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
import yfinance as yf

from . import market, watchlist
from .config import ROOT, settings
from .providers import failure_detail, yahoo_call
from .service import security_for
from .store import Session, Security, WatchlistEntry, WatchlistState, digest, latest, save_snapshot, stamp

logger = logging.getLogger(__name__)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str
    lookback_sessions: int = Field(ge=5, le=252)
    minimum_average_dollar_volume: str
    minimum_close: str
    maximum_members_age_days: int = Field(ge=1, le=90)
    request_interval_seconds: float = Field(ge=0.2, le=30)
    members_file: str
    adjustments_file: str = "config/us-index-adjustments.json"

    @model_validator(mode="after")
    def valid(self):
        if self.version != "us-index-liquidity-v1":
            raise ValueError("指数流动性配置版本无效")
        for value in (self.minimum_average_dollar_volume, self.minimum_close):
            number = Decimal(value)
            if not number.is_finite() or number <= 0:
                raise ValueError("价格和成交金额门槛必须为正数十进制字符串")
        return self


def policy(path=None):
    return Policy.model_validate(json.loads(Path(path or settings.us_universe_policy_file).read_text()))


def members(cfg):
    path = Path(cfg.members_file).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return validate_members(cfg, json.loads(path.read_text()))


def validate_members(cfg, data):
    if data.get("version") != "us-index-members-v1":
        raise ValueError("指数名单版本无效")
    sources = data.get("sources", [])
    if sorted(s.get("index", "") for s in sources) != ["NASDAQ100", "SP500"]:
        raise ValueError("需要且只能包含NASDAQ100与SP500两份来源")
    # 公开介绍页可能落后于正式调整公告；只对完全匹配的来源集合应用有据的更正。
    data = deepcopy(data)
    sources = data["sources"]
    correction_path = Path(cfg.adjustments_file).expanduser()
    if not correction_path.is_absolute():
        correction_path = ROOT / correction_path
    corrections = json.loads(correction_path.read_text())
    if corrections.get("version") != "index-members-adjustments-v1":
        raise ValueError("指数成分更正档案版本无效")
    today = datetime.now(timezone.utc).date()
    for source in sources:
        for change in corrections.get("changes", []):
            if source["index"] != change["index"] or digest(sorted(r["symbol"] for r in source["members"])) != change["base_symbols_digest"]:
                continue
            if date.fromisoformat(change["effective_date"]) > today:
                continue
            updated = {r["symbol"]: r for r in source["members"] if r["symbol"] not in change["remove"]}
            updated.update({r["symbol"]: r for r in change["add"]})
            source["members"] = [updated[s] for s in sorted(updated)]
            source.setdefault("applied_adjustments", []).append(change)
    result = {}
    for source in sources:
        acquired = datetime.fromisoformat(source["retrieved_at"])
        if acquired.tzinfo is None:
            raise ValueError("名单取得时间需要含时区")
        # 来源有明确日期时同时检查，不能用重新读取时间给旧名单续期。
        dates = [acquired.astimezone(timezone.utc).date()]
        if source.get("as_of"):
            dates.append(date.fromisoformat(source["as_of"]))
        if any(not 0 <= (today - d).days <= cfg.maximum_members_age_days for d in dates):
            raise ValueError("指数名单过旧或日期在未来，请更新名单及真实来源日期；原股票池保留")
        rows = source.get("members", [])
        lower, upper = (90, 120) if source["index"] == "NASDAQ100" else (450, 550)
        if not lower <= len(rows) <= upper or len({watchlist.normalize_symbol(r["symbol"]) for r in rows}) != len(rows):
            raise ValueError("指数名单数量异常或重复，拒绝以残缺来源覆盖股票池")
        for row in rows:
            symbol = watchlist.normalize_symbol(row["symbol"])
            if not symbol[0].isalpha() or symbol.endswith((".SS", ".SZ", ".BJ")):
                raise ValueError("指数名单只能包含美股证券代码")
            item = result.setdefault(symbol, {"symbol": symbol, "indexes": [], "name": row.get("name", "")})
            item["indexes"].append(source["index"])
    return data, [result[s] for s in sorted(result)]


@contextmanager
def sync_lock():
    settings.storage.joinpath("universe").mkdir(parents=True, exist_ok=True)
    with settings.storage.joinpath("universe", "sync.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("指数股票池正在同步，请等待结束") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def evaluate(bars, currency, cfg, target, sessions):
    """逐日未复权收盘×当日股数为成交金额近似，避免把股价高等同于冷门。"""
    if currency != "USD":
        raise ValueError("缺少日线自带的USD币种，不能猜测美元成交金额")
    by_date = {}
    for bar in bars:
        day = bar.get("date", "")
        if bar.get("complete") and day in sessions:
            if day in by_date:
                raise ValueError("流动性窗口有重复交易日")
            by_date[day] = bar
    if sorted(by_date) != sessions:
        raise ValueError(f"需要截至{target}连续{cfg.lookback_sessions}个完整交易日，实际{len(by_date)}个")
    turnovers = []
    for day in sessions:
        bar = by_date[day]
        try:
            close, volume = Decimal(str(bar["close"])), Decimal(str(bar["volume"]))
        except (InvalidOperation, KeyError, TypeError):
            raise ValueError("行情缺少有效收盘或成交量") from None
        if not close.is_finite() or not volume.is_finite() or close <= 0 or volume < 0 or volume != volume.to_integral_value():
            raise ValueError("收盘价或成交量无效")
        turnovers.append(close * volume)
    average = sum(turnovers) / Decimal(len(turnovers))
    close = Decimal(str(by_date[target]["close"]))
    reasons = []
    if average < Decimal(cfg.minimum_average_dollar_volume):
        reasons.append("日均成交金额不足")
    if close < Decimal(cfg.minimum_close):
        reasons.append("股价低于门槛")
    return {"status": "EXCLUDED" if reasons else "ELIGIBLE", "reasons": reasons,
            "average_dollar_volume": str(average), "close": str(close), "currency": "USD",
            "window_start": sessions[0], "window_end": target, "session_count": len(sessions)}


def _cached(symbol, cfg, target, sessions):
    with Session() as db:
        security = db.scalar(select(Security).where(Security.symbol == symbol))
        if not security:
            return None
        for kind in ("market", "universe_liquidity"):
            snap = latest(db, security.id, kind)
            if not snap:
                continue
            try:
                result = evaluate(snap.payload.get("bars", []),
                    snap.payload.get("profile", {}).get("currency"), cfg, target, sessions)
                return {**result, "data_source": kind, "source_id": snap.id, "source_version": snap.fingerprint}
            except ValueError:
                continue
    return None


def _acquire(symbol, cfg, target, sessions):
    security = security_for(symbol, create=True)
    ticker = yf.Ticker(symbol)
    if not hasattr(ticker, "_tz"):
        raise ValueError("yfinance版本不支持无身份查询的日线适配器")
    ticker._tz = "America/New_York"
    frame = yahoo_call(security.id, "指数流动性日线", lambda: ticker.history(
        start=sessions[0], end=(date.fromisoformat(target) + timedelta(days=1)).isoformat(),
        interval="1d", auto_adjust=False, back_adjust=False, actions=False,
        repair=False, prepost=False, raise_errors=True, timeout=15))
    if frame.empty:
        raise ValueError("未取得近期日线")
    metadata = dict(getattr(getattr(ticker, "_price_history", None), "_history_metadata", None) or {})
    if metadata.get("exchangeTimezoneName") != "America/New_York":
        raise ValueError("日线缺少有效美股时区；未追加证券身份查询")
    bars = []
    for idx, row in frame.iterrows():
        day = idx.date().isoformat()
        bars.append({"date": day, "close": market.clean(row.get("Close")),
                     "volume": market.clean(row.get("Volume")), "complete": day <= target})
    currency = metadata.get("currency")
    evaluated = evaluate(bars, currency, cfg, target, sessions)
    payload = {"bars": bars, "profile": {"currency": currency}, "fetched_at": stamp(),
               "source": "yfinance.history", "target_session": target,
               "note": "仅流动性日线缓存，不冒充完整研究/策略行情缓存"}
    with Session() as db:
        snap = save_snapshot(db, security.id, "universe_liquidity", payload)
        db.commit()
        return {**evaluated, "data_source": "yfinance.history", "source_id": snap.id, "source_version": snap.fingerprint}


def _apply(report):
    with watchlist.write_lock(), Session() as db:
        state = db.get(WatchlistState, watchlist.UNIVERSE_STATE_ID)
        control = dict(state.seed) if state else {}
        previous = set(control.get("managed_symbols", []))
        overrides = dict(control.get("manual_overrides", {}))
        leaders = db.get(WatchlistState, watchlist.VOLUME_LEADERS_STATE_ID)
        volume_members = set(leaders.seed.get("by_symbol", {})) if leaders else set()
        # 兼容旧版已经移除的记录，不让首次指数导入悄悄恢复用户排除的股票。
        existing = {r.symbol: r for r in db.scalars(select(WatchlistEntry))}
        for symbol, row in existing.items():
            if not row.active and symbol not in previous:
                overrides.setdefault(symbol, False)
        wanted = {r["symbol"] for r in report["results"] if r["status"] == "ELIGIBLE"}
        candidates = {r["symbol"] for r in report["results"]}
        unknown = {r["symbol"] for r in report["results"] if r["status"] == "UNKNOWN"}
        managed = previous | candidates
        changed = {"added": [], "removed": [], "manual_preserved": [], "unknown_preserved": []}
        for symbol in sorted(managed):
            row = existing.get(symbol)
            if symbol in overrides:
                changed["manual_preserved"].append(symbol)
                continue
            if row is not None and row.active and symbol in volume_members:
                # 已靠成交量榜保留的活动成员不随指数名单或流动性规则移出。
                continue
            # 网络失败不能等同低流动性，也不能批量撤销已有关注；新未知候选暂不加入。
            if symbol in unknown:
                if row is not None and row.active:
                    changed["unknown_preserved"].append(symbol)
                continue
            active = symbol in wanted
            if row is None and active:
                db.add(WatchlistEntry(symbol=symbol, active=True))
                changed["added"].append(symbol)
            elif row is not None and row.active != active:
                row.active = active
                row.updated_at = stamp()
                changed["added" if active else "removed"].append(symbol)
        control.update({"managed_symbols": sorted(managed), "manual_overrides": overrides,
                        "last_report": report["report_id"], "target_session": report["target_session"],
                        "members_digest": report["members_digest"], "policy": report["policy"]})
        if state:
            state.seed = control
        else:
            db.add(WatchlistState(id=watchlist.UNIVERSE_STATE_ID, seed=control))
        db.flush()
        changed["active_count"] = len(list(db.scalars(select(WatchlistEntry).where(WatchlistEntry.active.is_(True)))))
        db.commit()
        return changed


def sync(*, apply=False, offline=False, policy_file=None):
    cfg = policy(policy_file)
    source, candidates = members(cfg)
    cal, target = market.calendar_context()
    sessions = [d.date().isoformat() for d in cal.sessions_window(target, -cfg.lookback_sessions)]
    with sync_lock():
        report = {"report_id": "universe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
                  "created_at": stamp(), "target_session": target, "policy": cfg.model_dump(),
                  "members_digest": digest(source),
                  "sources": [{k: v for k, v in s.items() if k != "members"} for s in source["sources"]],
                  "candidate_count": len(candidates), "results": [], "applied": False}
        stopped = None
        for index, candidate in enumerate(candidates, 1):
            symbol = candidate["symbol"]
            result = _cached(symbol, cfg, target, sessions)
            if result is None:
                if offline or stopped:
                    result = {"status": "UNKNOWN", "reasons": [stopped or "缓存窗口不足，离线不联网"]}
                else:
                    logger.info("流动性[%s/%s] %s：补充近期日线", index, len(candidates), symbol)
                    try:
                        result = _acquire(symbol, cfg, target, sessions)
                    except Exception as exc:
                        failure = failure_detail(exc)
                        result = {"status": "UNKNOWN", "reasons": [failure["message"]], "error": failure}
                        if failure.get("category") == "rate_limited":
                            stopped = "Yahoo限流，本轮剩余股票仅检查缓存，不自动重试"
                    time.sleep(cfg.request_interval_seconds)
            report["results"].append({**candidate, **result})
            if index % 20 == 0 or index == len(candidates):
                logger.info("流动性进度%s/%s；通过%s、排除%s、缺数据%s", index, len(candidates),
                    *(sum(r["status"] == s for r in report["results"]) for s in ("ELIGIBLE", "EXCLUDED", "UNKNOWN")))
        report["counts"] = {s: sum(r["status"] == s for r in report["results"]) for s in ("ELIGIBLE", "EXCLUDED", "UNKNOWN")}
        path = settings.storage / "universe" / (report["report_id"] + ".json")
        report["report_file"] = str(path)
        # 名单提交前先落完整评估；即使提交后进程中断，也能按数据库last_report核对输入。
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        # 全部未取得有效数据时不因网络故障清空原池。
        if apply and not report["counts"]["ELIGIBLE"]:
            report["apply_error"] = "没有取得任何合格结果，保留原股票池；请补齐行情后重新同步"
        elif apply:
            report["changes"] = _apply(report)
            report["applied"] = True
        pending = path.with_suffix(".pending")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        pending.replace(path)
        return report

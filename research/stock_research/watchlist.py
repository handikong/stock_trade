"""单用户自选池、串行双策略扫描与可追溯信号历史；不处理成交或推送。"""
import fcntl
import json
import logging
import re
from contextlib import contextmanager
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from .config import ROOT, settings
from .store import (Session, WatchlistEntry, WatchlistScan, WatchlistSignal,
                    WatchlistSignalRevision, WatchlistState, Snapshot, digest, latest, stamp)

logger = logging.getLogger(__name__)
SEED_FILE = ROOT / "config" / "watchlist.json"
UNIVERSE_STATE_ID = "us-index-universe"
VOLUME_LEADERS_STATE_ID = "us-volume-leaders"


def normalize_symbol(value):
    symbol = value.strip().upper().removeprefix("$")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol):
        raise ValueError("请输入有效美股代码，例如AAPL、NVDA或BRK-B")
    return symbol


class SeedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str
    name: str
    symbols: list[str] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def symbols_valid(self):
        self.symbols = [normalize_symbol(s) for s in self.symbols]
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("初始股票池不能包含重复代码")
        return self


@contextmanager
def write_lock():
    """本地单用户写操作互斥，避免并发扫描重复记账；只读查询无需等待网络。"""
    settings.storage.mkdir(parents=True, exist_ok=True)
    with (settings.storage / "watchlist.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("股票池已有更新或扫描正在进行，请等待完成") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_seed():
    with Session() as session:
        if session.get(WatchlistState, "default"):
            return
    cfg = SeedPolicy.model_validate(json.loads(SEED_FILE.read_text(encoding="utf-8")))
    with write_lock(), Session() as session:
        if session.get(WatchlistState, "default"):
            return
        session.add(WatchlistState(id="default", seed=cfg.model_dump()))
        for symbol in cfg.symbols:
            if not session.get(WatchlistEntry, symbol):
                session.add(WatchlistEntry(symbol=symbol))
        session.commit()
    logger.info("首次初始化%s：%s", cfg.name, "、".join(cfg.symbols))


def entries(include_removed=False):
    with Session() as session:
        query = select(WatchlistEntry).order_by(WatchlistEntry.symbol)
        if not include_removed:
            query = query.where(WatchlistEntry.active.is_(True))
        return [{"symbol": r.symbol, "active": r.active, "created_at": r.created_at,
                 "updated_at": r.updated_at} for r in session.scalars(query)]


def change_symbols(symbols, active):
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    changed = []
    with write_lock(), Session() as session:
        # 明确的人工增删优先于以后指数同步；即使尚未首次导入也保存排除意图。
        state = session.get(WatchlistState, UNIVERSE_STATE_ID)
        control = dict(state.seed) if state else {"managed_symbols": [], "manual_overrides": {}}
        overrides = dict(control.get("manual_overrides", {}))
        for symbol in symbols:
            overrides[symbol] = active
            row = session.get(WatchlistEntry, symbol)
            if row is None and active:
                row = WatchlistEntry(symbol=symbol, active=True)
                session.add(row)
                outcome = "已添加"
            elif row is None:
                outcome = "不在股票池中"
            elif row.active == active:
                outcome = "已在股票池中" if active else "已移出"
            else:
                row.active = active
                row.updated_at = stamp()
                outcome = "已恢复关注" if active else "已移出，历史保留"
            changed.append({"symbol": symbol, "result": outcome})
        control["manual_overrides"] = overrides
        if state:
            state.seed = control
        else:
            session.add(WatchlistState(id=UNIVERSE_STATE_ID, seed=control))
        session.commit()
    return changed


def _persist_symbol(scan_id, summary, signals, provenance):
    """每只股票原子落库；事件按股票/策略/日期去重，参数或数据修订另存版本。"""
    with Session() as session:
        references = []; new_count = revised_count = 0
        for signal in signals:
            # 保留旧买点主键；退出必须关联原买点，避免同日多个跟踪项退出时合并。
            key = [summary["symbol"], signal["strategy_id"], signal["as_of"]]
            if signal.get("event_kind") == "EXIT":
                key.extend(["EXIT", signal["entry_signal_date"]])
            event_id = digest(key)
            payload = {"signal": signal, "provenance": provenance}
            revision_id = digest({"event_id": event_id, "payload": payload})
            event = session.get(WatchlistSignal, event_id)
            is_new = event is None
            if is_new:
                event = WatchlistSignal(id=event_id, symbol=summary["symbol"],
                    strategy_id=signal["strategy_id"], signal_date=signal["as_of"],
                    latest_revision_id=revision_id, revision_count=0)
                session.add(event)
                session.flush()
                new_count += 1
            revision = session.get(WatchlistSignalRevision, revision_id)
            if revision is None:
                session.add(WatchlistSignalRevision(id=revision_id, signal_id=event_id, payload=payload))
                event.revision_count += 1
                revised_count += int(not is_new)
            event.latest_revision_id = revision_id
            references.append({"event_id": event_id, "revision_id": revision_id,
                               "first_discovery": is_new})
        summary = {**summary, "first_discovered_count": new_count,
                   "new_revision_count": revised_count, "signal_records": references}
        run = session.get(WatchlistScan, scan_id)
        run.payload = {**run.payload, "results": [*run.payload["results"], summary]}
        session.commit()
    return summary


def _one_symbol(symbol, *, start, history_start, end, expected_session, historical,
                offline, refresh, policy_file, strategy_cfg, region="US"):
    from . import band_strategies, futu_macd, market
    from .providers import failure_detail
    from .service import security_for

    security = security_for(symbol, create=True)
    warnings = []
    with Session() as session:
        source = latest(session, security.id, market.snapshot_kind(region))
    # 普通扫描已有缓存就用；仅缺行情或明确--refresh时请求yf。
    if not offline and (source is None or refresh):
        try:
            market.refresh_market(security.id, force=refresh,region=region)
        except Exception as exc:
            warning = failure_detail(exc)
            warnings.append(warning)
            logger.warning("%s 行情获取失败：%s；尝试保留缓存", symbol, warning["message"])
    with Session() as session:
        source = latest(session, security.id, market.snapshot_kind(region))
        if source is None:
            raise ValueError("尚无行情缓存；离线模式请先获取日线" if offline else "未取得行情，后续股票继续扫描")
        eligible = [b for b in source.payload.get("bars", [])
                    if b.get("complete") is True and b["date"] <= end]
        if not eligible:
            raise ValueError("截止日期之前没有完整日线")
        last_bar = max(eligible,key=lambda b:b["date"])
        available_end = last_bar["date"]
        if region=="CN" and not last_bar.get("volume"):
            warnings.append({"message":"目标日无成交量，未确认可推送买点"})
        market_state = market.cache_state(source,region)
    display_start = start or available_end
    seed = history_start or f"{display_start[:4]}-01-01"
    if seed > display_start or display_start > end:
        raise ValueError("需要满足预热起点≤展示起点≤截止日期")
    report = futu_macd.evaluate_timeline(security.id, history_start=seed, start=display_start,
                                        end=end, policy_file=policy_file,region=region)
    warnings.extend(report.get("warnings", []))
    result = band_strategies.evaluate_report(report,strategy_cfg,region=region)
    # 指标按输入内容缓存：刷新取得相同日线时允许复用旧指标，但须逐项比对计算窗口。
    # 新鲜度来自本次取得的行情档案，策略证据仍保留其真实绑定的旧快照，不篡改来源。
    equivalent_source = report["source_market_id"] == source.id
    if not equivalent_source:
        from .trend import _bars
        with Session() as session:
            bound = session.get(Snapshot, report["source_market_id"])
            def window(item):
                return _bars({"bars": [b for b in item.payload.get("bars", [])
                    if b.get("complete") is True and seed <= b["date"] <= end]})
            equivalent_source = (bound is not None and window(bound) == window(source)
                and bound.payload.get("adjustment") == source.payload.get("adjustment"))
    if not equivalent_source:
        warnings.append({"message": "计算行情与新鲜度档案的窗口内容不一致，本次仅作历史参考，请重新扫描"})
    current = (not historical and result["as_of"] == expected_session
               and market_state["state"] == "fresh"
               and market_state.get("expected_session") == expected_session and not warnings)
    statuses = [{"strategy_id": s["strategy_id"], "name": s["name"], "status": s["status"],
                 "status_label": s["status_label"], "unmet": s["unmet"]}
                for s in result["latest"]["strategies"] if s["status"] != "DISABLED"]
    signals = sorted([{"symbol": symbol, **s} for s in [*result["signals"], *result["exit_signals"]]],
                     key=lambda s: (s["as_of"], s["event_kind"], s["strategy_id"], s.get("entry_signal_date", "")))
    recent_signals = [s for s in signals if s["as_of"] == result["as_of"]]
    summary = {"symbol": symbol, "status": "OK" if current else "HISTORICAL" if historical else "STALE",
        "data_as_of": result["as_of"], "expected_session": expected_session,
        "freshness_source_market_id": source.id, "calculation_source_market_id": result["source_market_id"],
        "source_window_equivalent": equivalent_source,
        "market_cache": market_state, "warnings": warnings, "strategies": statuses,
        "historical_only": not current, "signal_count": len(signals),
        "buy_signal_count": len(result["signals"]), "exit_signal_count": len(result["exit_signals"]),
        "exit_tracking": result["latest"]["exit_tracking"],
        "flows": result["flows"], "flow_format_version": result["flow_format_version"],
        "exit_alerts": result["exit_alerts"],
        "current_exit_alerts": result["latest"]["exit_alerts"] if current else [],
        "current_signals": recent_signals if current else [], "signals": signals}
    provenance = {"source_market_id": result["source_market_id"],
        "source_market_version": result["source_market_version"],
        "strategy_algorithm": result["algorithm"], "strategy_configuration": result["configuration"],
        "indicator_parameters": report["parameters"], "history_start": seed,
        "price_field": result["price_field"]}
    summary["evaluation"] = {**provenance, "source_trend_id": result["source_trend_id"],
        "input_version": result["input_version"], "configuration_version": result["configuration_version"]}
    return summary, signals, provenance


def scan(*, start=None, history_start=None, end=None, offline=False, refresh=False,
         policy_file=None, strategy_policy=None, symbol=None, symbols=None, region="US"):
    from . import band_strategies, futu_macd, market
    from .providers import failure_detail
    from .trend import _calendar

    # 显式指定仅扫描这一只，不添加/恢复自选池成员；仍复用同一计算和历史归档流程。
    normalize = normalize_symbol
    if region == "CN":
        from . import cn_sources
        def normalize(value):
            return cn_sources.symbol(value,allow_etf=True)
    selected_symbol = normalize(symbol) if symbol is not None else None
    if symbols is not None and selected_symbol is not None:
        raise ValueError("单股与股票池子集不能同时指定")
    selected_symbols = list(dict.fromkeys(normalize(s) for s in symbols)) if symbols is not None else None
    if offline and refresh:
        raise ValueError("离线模式不能同时刷新行情")
    for value in (start, history_start, end):
        if value:
            date.fromisoformat(value)
    if history_start and start and history_start > start:
        raise ValueError("预热起点不能晚于展示起点")
    policy_file = policy_file or futu_macd.POLICY_FILE
    futu_macd.policy(policy_file)
    strategy_cfg = band_strategies.policy(strategy_policy)
    cal, expected = market.calendar_context(region)
    historical = end is not None
    requested_end = end or expected
    if requested_end > expected or (start and start > requested_end):
        raise ValueError("展示日期不能晚于最近已收盘交易日，起点不能晚于终点")
    if region == "US":
        cal = _calendar(date.fromisoformat(requested_end), date.fromisoformat(requested_end))
    target = cal.date_to_session(requested_end, direction="previous").date().isoformat()
    if start and start > target:
        raise ValueError("展示范围没有已完成交易日")
    with write_lock():
        if region == "CN":
            from . import cn_income
            with Session() as session:
                active_symbols = [r["symbol"] for r in cn_income.read(session)["members"]]
        else:
            active_symbols = [r["symbol"] for r in entries()]
        excluded_symbols = [s for s in selected_symbols if s not in active_symbols] if selected_symbols is not None else []
        symbols = ([selected_symbol] if selected_symbol else [s for s in selected_symbols if s in active_symbols]
                   if selected_symbols is not None else active_symbols)
        if not symbols:
            raise ValueError("股票池为空，请先添加股票")
        payload = {"symbols": symbols, "scope": "SINGLE" if selected_symbol else "WATCHLIST",
            "requested_symbol": selected_symbol, "expected_session": expected, "target_session": target,
            "requested_symbols":selected_symbols,
            "excluded_symbols":excluded_symbols,
            "historical_mode": historical, "start": start, "history_start": history_start,
            "offline": offline, "refresh": refresh, "results": [], "market":region}
        with Session() as session:
            run = WatchlistScan(payload=payload)
            session.add(run); session.commit()
            scan_id = run.id
        final_status = "interrupted"
        try:
            for index, symbol in enumerate(symbols, 1):
                logger.info("[%s/%s] 扫描%s；两策略独立判断", index, len(symbols), symbol)
                try:
                    summary, signals, provenance = _one_symbol(symbol, start=start,
                        history_start=history_start, end=target, expected_session=expected,
                        historical=historical, offline=offline, refresh=refresh,
                        policy_file=policy_file, strategy_cfg=strategy_cfg,region=region)
                except Exception as exc:
                    detail = failure_detail(exc)
                    summary = {"symbol": symbol, "status": "ERROR", "error": detail,
                               "data_as_of": None, "signals": [], "current_signals": []}
                    signals = []; provenance = {}
                    logger.warning("%s 扫描未完成：%s", symbol, detail["message"])
                # 数据库持久化失败不可伪装成普通股票失败，向外报告并保留此前已提交结果。
                summary = _persist_symbol(scan_id, summary, signals, provenance)
                logger.info("%s 完成：%s，数据截止%s，首次发现%s条，新增修订%s条", symbol,
                    summary["status"], summary["data_as_of"] or "未知",
                    summary["first_discovered_count"], summary["new_revision_count"])
            final_status = "complete"
        finally:
            with Session() as session:
                run = session.get(WatchlistScan, scan_id)
                if final_status == "complete" and any(r["status"] in ("ERROR", "STALE")
                                                     or r.get("warnings") for r in run.payload["results"]):
                    final_status = "partial"
                run.status = final_status; run.finished_at = stamp()
                session.commit()
        return scan_record(scan_id,region=region)


def scan_market(region):
    return func.coalesce(WatchlistScan.payload["market"].as_string(),"US")==region


def scan_record(scan_id=None, *, region="US"):
    with Session() as session:
        row = session.get(WatchlistScan, scan_id) if scan_id else session.scalar(
            select(WatchlistScan).where(scan_market(region))
            .order_by(WatchlistScan.created_at.desc(), WatchlistScan.id.desc()).limit(1))
        if row is None or row.payload.get("market","US") != region:
            raise ValueError("尚无扫描记录或指定扫描不存在")
        return {"scan_id": row.id, "created_at": row.created_at, "finished_at": row.finished_at,
                "status": row.status, **row.payload}


def history(*, symbol=None, strategy_id=None, start=None, end=None, limit=100, event_kind=None):
    if limit < 1 or limit > 10000:
        raise ValueError("历史条数须在1～10000之间")
    if start and end and date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("历史查询起点不能晚于终点")
    if event_kind not in (None, "BUY", "EXIT"):
        raise ValueError("信号类型只能为BUY或EXIT")
    with Session() as session:
        query = select(WatchlistSignal).where(~WatchlistSignal.symbol.like("%.SH"),~WatchlistSignal.symbol.like("%.SZ"))
        if event_kind:
            query = query.join(WatchlistSignalRevision,
                WatchlistSignalRevision.id == WatchlistSignal.latest_revision_id).where(
                func.coalesce(WatchlistSignalRevision.payload["signal"]["event_kind"].as_string(), "BUY") == event_kind)
        if symbol:
            query = query.where(WatchlistSignal.symbol == normalize_symbol(symbol))
        if strategy_id:
            query = query.where(WatchlistSignal.strategy_id == strategy_id)
        if start:
            query = query.where(WatchlistSignal.signal_date >= date.fromisoformat(start).isoformat())
        if end:
            query = query.where(WatchlistSignal.signal_date <= date.fromisoformat(end).isoformat())
        events = session.scalars(query.order_by(WatchlistSignal.signal_date.desc(),
            WatchlistSignal.symbol, WatchlistSignal.strategy_id).limit(limit)).all()
        result = []
        for event in events:
            revision = session.get(WatchlistSignalRevision, event.latest_revision_id)
            result.append({"event_id": event.id, "symbol": event.symbol, "strategy_id": event.strategy_id,
                "signal_date": event.signal_date, "first_seen_at": event.first_seen_at,
                "revision_count": event.revision_count, "revision_id": revision.id, **revision.payload})
        return result


def revisions(event_id):
    with Session() as session:
        return [{"revision_id": r.id, "created_at": r.created_at, **r.payload} for r in session.scalars(
            select(WatchlistSignalRevision).where(WatchlistSignalRevision.signal_id == event_id)
            .order_by(WatchlistSignalRevision.created_at, WatchlistSignalRevision.id))]

"""当前信号→公共研究→群通知；历史回放禁止投递，本机文件锁和投递状态防重。"""
import fcntl
import logging
import time
from contextlib import contextmanager

from sqlalchemy import select

from . import band_strategies, feishu, futu_macd, market, signal_research, watchlist
from .cli import decimal_text
from .config import settings
from .notification_cards import FORMAT, buy_card
from .store import Session, Security, SignalDelivery, Snapshot, WatchlistEntry, digest, latest, stamp

logger = logging.getLogger(__name__)


@contextmanager
def notification_lock():
    settings.storage.mkdir(parents=True, exist_ok=True)
    with (settings.storage / "notifications.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("通知流程已在运行，请等待完成") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _events(scan, *, symbol=None, region="US"):
    _, expected = market.calendar_context(region)
    if scan.get("historical_mode") or scan.get("expected_session") != expected:
        return [], ["历史回放或非当前交易日的扫描只供研究，不生成群通知"]
    if scan.get("status") not in ("complete", "partial"):
        return [], ["扫描尚未完成或曾中断，请重新扫描后再处理通知"]
    result = []; rejected = []
    for row in scan["results"]:
        if symbol is not None and row["symbol"] != symbol:
            continue
        if row["status"] != "OK" or row["data_as_of"] != expected:
            rejected.append(f"{row['symbol']}：行情失败、旧行情或警告，本次不推送")
            continue
        base = {"symbol": row["symbol"], "as_of": expected,
                "market_id": row["freshness_source_market_id"],
                "history_start": row["evaluation"]["history_start"], "scan_id": scan["scan_id"],
                "evaluation": row["evaluation"], "execution_scope": "SINGLE" if symbol else "WATCHLIST"}
        for signal in row["current_signals"]:
            if signal["as_of"] == expected:
                result.append({**base, "kind": "BUY" if signal.get("event_kind", "BUY") == "BUY" else "EXIT_CONDITION",
                               "strategy_id": signal["strategy_id"], "signal": signal})
        for alert in row.get("current_exit_alerts", []):
            if alert["as_of"] == expected:
                result.append({**base, "kind": "ALERT", "strategy_id": alert["strategy_id"], "signal": alert})
    def priority(item):
        signal = item["signal"]
        return (0 if signal.get("reason") == "INITIAL_PROTECTION_BROKEN" else
                1 if item["kind"] == "EXIT_CONDITION" else 2 if item["kind"] == "ALERT" else 3,
                item["symbol"], item["strategy_id"], signal.get("entry_signal_date", ""))
    return sorted(result, key=priority), rejected


def _still_current(item, region="US"):
    """投递前重新校验交易日/行情及计算窗口；研究不能替换已触发信号的价格证据。"""
    _, expected = market.calendar_context(region)
    if item["as_of"] != expected:
        return False
    evaluation = item["evaluation"]
    # 读取旧scan-id时，不能沿用已经停用的策略/参数；通知只使用当前默认策略配置。
    if (evaluation["strategy_algorithm"] != band_strategies.ALGORITHM
            or evaluation["strategy_configuration"] != band_strategies.policy().model_dump()
            or evaluation["indicator_parameters"] != futu_macd.policy()):
        return False
    with Session() as session:
        # 单股是用户本次显式选择，无需先加入池；批量仍校验成员，旧扫描不能恢复移出的关注。
        if item.get("execution_scope", "WATCHLIST") != "SINGLE":
            if region == "CN":
                from . import cn_income
                if not any(row["symbol"] == item["symbol"] for row in cn_income.read(session)["members"]):
                    return False
            else:
                entry = session.get(WatchlistEntry, item["symbol"])
                if entry is None or not entry.active:
                    return False
        security = session.scalar(select(Security).where(Security.symbol == item["symbol"]))
        source = latest(session, security.id, market.snapshot_kind(region)) if security else None
        old = session.get(Snapshot, item["market_id"])
        trend = session.get(Snapshot, evaluation["source_trend_id"])
        if source is None or old is None or market.cache_state(source,region)["state"] != "fresh":
            return False
        if trend is None or trend.payload.get("algorithm") != futu_macd.ALGORITHM:
            return False
        if source.payload.get("last_complete_session") != expected:
            return False
        def window(snapshot):
            return [b for b in snapshot.payload.get("bars", []) if b.get("complete") is True
                    and item["history_start"] <= b["date"] <= expected]
        return window(source) == window(old) and source.payload.get("adjustment") == old.payload.get("adjustment")


def _key(item, destination_key):
    signal = item["signal"]
    return digest([destination_key, item["symbol"], item["kind"], item["strategy_id"], item["as_of"],
                   signal.get("entry_signal_date"), signal.get("to_level") if item["kind"] == "ALERT" else None])


def _card(item, research):
    if item["kind"]=="BUY":
        return buy_card(item["symbol"],research,item=item)
    signal = item["signal"]; kind = item["kind"]
    label = "达到退出条件，请手动决定" if kind == "EXIT_CONDITION" else signal["status_label"]
    color = "green" if kind == "ALERT" and signal.get("to_level") == 0 else "orange" if kind == "ALERT" else "red"
    text = [f"股票 {item['symbol']}｜策略 {item['strategy_id']}", f"信号交易日 {item['as_of']}；生成时间UTC {stamp()}"]
    configuration = item["evaluation"]["strategy_configuration"]
    price_basis = "技术调整价" if item["evaluation"].get("price_field") == "adjusted_close" else "原始收盘价"
    text.append("参考价格口径：" + price_basis + "，不是实际成交价；未提供真实账户仓位建议。")
    text.extend([f"对应历史买点 {signal.get('entry_signal_date', '未知')}；{signal['status_label']}",
                 "这是基于技术买点的跟踪，尚未核实你是否真实持仓；不会记为已卖出。"])
    state = signal.get("common_state") or (signal.get("evidence") or {}).get("common_state") or {}
    periods = (state.get("evidence") or {}).get("ema_periods") or [configuration["exits"]["fast_ema_period"], configuration["exits"]["slow_ema_period"]]
    def flag(value):
        return "是" if value is True else "否" if value is False else "未知"
    text.append(f"红带={flag(state.get('band_red'))}；EMA{'/'.join(str(p) for p in periods)}偏空={flag(state.get('ema_fast_below_slow'))}；MACD偏空={flag(state.get('macd_dif_below_dea'))}")
    if signal.get("reason") == "INITIAL_PROTECTION_BROKEN":
        evidence = signal["evidence"]
        text.append(f"保护线 {decimal_text(signal.get('initial_protection_price'))}；当日最低 {decimal_text(evidence.get('low'))}；日内破位由日线事后识别。")
    sections = ["\n".join(text)]
    sections.append("仅技术提示和研究草稿，实盘由你手动决定；未通过真实账户资金/持仓风控，不生成成交或收益。")
    # 使用plain_text，不让财报/AI文字中的链接、@提及或格式标记触发机器人行为。
    return {"config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": f"股票信号｜{item['symbol']}｜{label}"[:120]}},
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": section}} for section in sections]}


def _allowed(row, retry_failed, retry_unknown):
    return row is None or row.status == "READY" or (row.status == "FAILED" and retry_failed) or (row.status == "UNKNOWN" and retry_unknown)


def run(*, symbol=None, scan_id=None, refresh=False, send=False, cached_research=False, retry_failed=False, retry_unknown=False,
        buy_only=False, delivery_guard=None, region="US"):
    if type(buy_only) is not bool or (delivery_guard is not None and not callable(delivery_guard)):
        raise ValueError("买点筛选或计划任务投递检查参数无效")
    symbol = watchlist.normalize_symbol(symbol) if symbol is not None else None
    if scan_id and refresh:
        raise ValueError("读取扫描记录不能同时刷新；请去掉--scan-id创建新扫描")
    if (retry_failed or retry_unknown) and not send:
        raise ValueError("重发选项只用于--send模式")
    destination_key = feishu.destination(required=send)
    results = []; research_cache = {}
    with notification_lock():
        # 上次在HTTP调用期间中断，结果可能已到群；只转UNKNOWN，不能静默恢复成可重发。
        if send:
            with Session() as session:
                for row in session.scalars(select(SignalDelivery).where(SignalDelivery.status == "SENDING")):
                    row.status = "UNKNOWN"; row.updated_at = stamp()
                    row.last_error = "上次发送中断，是否送达未知，请核对群消息"
                    attempts = list(row.payload.get("attempt_history", []))
                    if attempts:
                        attempts[-1] = {**attempts[-1], "status": "UNKNOWN", "error": row.last_error}
                        row.payload = {**row.payload, "attempt_history": attempts}
                session.commit()
        scan = (watchlist.scan_record(scan_id,region=region) if scan_id
                else watchlist.scan(symbol=symbol,offline=not refresh,refresh=refresh,region=region))
        if symbol is None and scan.get("scope") == "SINGLE":
            symbol = watchlist.normalize_symbol(scan["requested_symbol"])
        if symbol is not None and not any(row["symbol"] == symbol for row in scan["results"]):
            raise ValueError(f"扫描记录中没有{symbol}，请去掉--scan-id为该股票重新扫描")
        events, warnings = _events(scan,symbol=symbol,region=region)
        if buy_only:
            events = [item for item in events if item["kind"]=="BUY"]
        handled = 0
        next_send_at = 0.0
        for item in events:
            if delivery_guard is not None and not delivery_guard():
                results.append({"symbol":item["symbol"],"kind":item["kind"],"status":"SKIPPED_SCHEDULE_WINDOW",
                                "error":"计划任务已停用或超过当前处理时段，本条未发送"})
                continue
            key = _key(item, destination_key or "PREVIEW")
            with Session() as session:
                previous = session.get(SignalDelivery, key) if destination_key else None
                if send and not _allowed(previous, retry_failed, retry_unknown):
                    results.append({"id": key, "symbol": item["symbol"], "kind": item["kind"],
                                    "status": "SKIPPED_" + previous.status})
                    continue
            if handled >= settings.feishu_max_messages_per_run:
                results.append({"symbol": item["symbol"], "kind": item["kind"], "status": "DEFERRED_LIMIT"})
                continue
            if not _still_current(item,region):
                results.append({"symbol": item["symbol"], "kind": item["kind"], "status": "SKIPPED_STALE",
                                "error": "行情、策略配置或股票池状态已变化，请重新扫描"})
                continue
            research = None
            if item["kind"] == "BUY":
                if item["symbol"] not in research_cache:
                    try:
                        if region == "CN":
                            from . import cn_research
                            research_cache[item["symbol"]] = cn_research.prepare(item["symbol"])
                        else:
                            research_cache[item["symbol"]] = signal_research.prepare(item["symbol"], cached_only=not send or cached_research)
                    except Exception:
                        logger.warning("%s研究未完成，保留技术信号并标注资料缺口", item["symbol"])
                        research_cache[item["symbol"]] = {"lines": ["本次研究未完成；资料待补齐，不得据此判为基本面通过。"], "versions": {}}
                research = research_cache[item["symbol"]]
            card = _card(item, research)
            handled += 1
            if not send:
                results.append({"id": key, "symbol": item["symbol"], "kind": item["kind"], "status": "PREVIEW", "card": card})
                continue
            # 本轮批量投递分散请求，低于单机器人5次/秒、100次/分钟；不假设独占外部机器人。
            delay = next_send_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if not _still_current(item,region):
                results.append({"symbol": item["symbol"], "kind": item["kind"], "status": "SKIPPED_STALE_AFTER_RESEARCH",
                                "error": "研究期间行情、策略配置或股票池状态已变化，取消本条发送"})
                continue
            if delivery_guard is not None and not delivery_guard():
                results.append({"symbol":item["symbol"],"kind":item["kind"],"status":"SKIPPED_SCHEDULE_WINDOW",
                                "error":"研究期间计划任务已停用或处理时段结束，本条未发送"})
                continue
            with Session() as session:
                row = session.get(SignalDelivery, key)
                if row is None:
                    row = SignalDelivery(id=key, destination_key=destination_key, symbol=item["symbol"],
                        kind=item["kind"], signal_date=item["as_of"], payload={})
                    session.add(row); session.flush()
                card_format = FORMAT if item["kind"]=="BUY" else "legacy-plain-text"
                attempt = {"started_at": stamp(), "status": "SENDING", "card": card,"card_format":card_format,
                           "research_versions": (research or {}).get("versions", {}), "research":research, "event": item}
                row.payload = {"event": item, "card": card, "research_versions": (research or {}).get("versions", {}),
                               "card_format":card_format,"attempt_history": [*row.payload.get("attempt_history", []), attempt]}
                row.status = "SENDING"; row.attempts += 1; row.updated_at = stamp(); row.last_error = ""
                session.commit()
            try:
                next_send_at = time.monotonic() + 0.65
                if region == "CN":
                    accepted = feishu.send(card,allowed=delivery_guard)
                    status, error = (("SENT","") if accepted else
                                     ("SKIPPED_SCHEDULE_WINDOW","投递前处理窗口已结束"))
                else:
                    feishu.send(card)
                    status, error = "SENT", ""
            except feishu.DeliveryFailure as exc:
                status = exc.status; error = str(exc)
            with Session() as session:
                row = session.get(SignalDelivery, key)
                row.status = status; row.last_error = error; row.updated_at = stamp()
                attempts = list(row.payload["attempt_history"])
                attempts[-1] = {**attempts[-1], "status": status, "error": error, "finished_at": row.updated_at}
                row.payload = {**row.payload, "attempt_history": attempts}
                session.commit()
            logger.info("%s %s 群通知：%s%s", item["symbol"], item["kind"], status, "；" + error if error else "")
            results.append({"id": key, "symbol": item["symbol"], "kind": item["kind"], "status": status, "error": error})
        return {"mode": "SEND" if send else "PREVIEW", "scan_id": scan["scan_id"],
                "scope": "SINGLE" if symbol else "WATCHLIST", "symbol": symbol,
                "warnings": warnings, "items": results, "message": "未发送外部消息" if not send else "SENT仅表示飞书接口确认接收，不代表已读或已成交"}


def history(limit=50):
    if not 1 <= limit <= 1000:
        raise ValueError("投递历史条数须在1～1000之间")
    with Session() as session:
        return [{"id": row.id, "symbol": row.symbol, "kind": row.kind, "signal_date": row.signal_date,
                 "status": row.status, "attempts": row.attempts, "updated_at": row.updated_at,
                 "error": row.last_error} for row in session.scalars(select(SignalDelivery)
                    .order_by(SignalDelivery.created_at.desc(), SignalDelivery.id.desc()).limit(limit))]

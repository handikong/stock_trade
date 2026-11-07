"""从各推送链路的投递档案汇总工作台记录。"""
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import futu_heat, holdings_brief, market_brief
from .config import settings
from .notification_cards import card_text
from .store import Job, Session, SignalDelivery, WatchlistState, now

Category = Literal["market", "stock", "watchlist", "holdings", "test"]
BEIJING = ZoneInfo("Asia/Shanghai")
CATEGORIES = {"market":"大盘推送", "stock":"个股推送", "watchlist":"关注推送",
              "holdings":"持仓复盘", "test":"连通性测试"}
STATUS_LABELS = {"SENT":"飞书已接收", "FAILED":"失败", "UNKNOWN":"结果未知", "SENDING":"发送中"}
HOLDINGS_STATUSES = {"已发送":"SENT", "失败":"FAILED", "结果未知":"UNKNOWN", "发送中":"SENDING"}
SIGNAL_LABELS = {"BUY":"策略买点", "ALERT":"退出预警", "EXIT":"退出条件", "EXIT_CONDITION":"退出条件"}


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        return instant.astimezone(timezone.utc) if instant.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _record(category, key, label, delivery, *, fallback=None, legacy_card=None):
    status = str(delivery.get("status", "")).upper()
    if status not in STATUS_LABELS:
        return None
    recorded = next((instant for field in ("started_at", "created_at", "finished_at")
                     if (instant := _instant(delivery.get(field))) is not None), None)
    when = recorded or _instant(fallback)
    if when is None:
        return None
    card = delivery.get("card")
    if not isinstance(card, dict):
        card = legacy_card
    card = card or {}
    title = ((card.get("header") or {}).get("title") or {}).get("content") or label
    body = "\n\n".join(card_text(card)).replace("**", "").strip()
    local = when.astimezone(BEIJING)
    message = delivery.get("error") or (delivery.get("message") if status != "SENT" else "")
    return {"id":key, "category":category, "category_label":CATEGORIES[category],
            "status":status, "status_label":STATUS_LABELS[status], "title":title,
            "date":local.date().isoformat(), "time":local.strftime("%H:%M:%S"),
            "at":when.isoformat(timespec="microseconds"), "estimated_time":recorded is None,
            "body":body[:8000] + ("\n…" if len(body) > 8000 else ""), "error":str(message or "")[:500]}


def _saved_card(delivery, build):
    if isinstance(delivery.get("card"), dict):
        return None
    try:
        return build()
    except (KeyError, TypeError, ValueError):
        # 旧版本报告可能缺少重建卡片所需字段，仍显示投递结果。
        return None


def _scheduled(job):
    report = (job.result or {}).get("report") or {}
    if job.kind == market_brief.KIND:
        delivery = report.get("delivery")
        if delivery:
            yield _record("market", f"job:{job.id}", market_brief.LABELS.get(report.get("report_type"), "大盘简报"),
                          delivery, fallback=job.updated_at, legacy_card=_saved_card(delivery, lambda: market_brief._card(report)))
        return
    for delivery in report.get("delivery", []):
        status = HOLDINGS_STATUSES.get(delivery.get("status"))
        if not status:
            continue
        part = delivery["part"]
        legacy = _saved_card(delivery, lambda: dict(holdings_brief._cards(report)).get(part))
        yield _record("holdings", f"job:{job.id}:{part}", "持仓复盘", {**delivery,"status":status},
                      fallback=job.updated_at, legacy_card=legacy)


def _database_records(lower, category):
    with Session() as session:
        if category in (None, "stock"):
            signals = select(SignalDelivery).where(SignalDelivery.updated_at >= lower, SignalDelivery.attempts > 0)
            for row in session.scalars(signals):
                payload = row.payload or {}
                label = f"{row.symbol} · {SIGNAL_LABELS.get(row.kind, '策略通知')}"
                attempts = payload.get("attempt_history") or [
                    {"status":row.status, "card":payload.get("card"), "error":row.last_error}]
                for index, attempt in enumerate(attempts, 1):
                    yield _record("stock", f"signal:{row.id}:{index}", label, attempt, fallback=row.updated_at)
        kinds = [kind for name, kind in (("market", market_brief.KIND), ("holdings", holdings_brief.KIND))
                 if category in (None, name)]
        if kinds:
            jobs = select(Job).where(Job.kind.in_(kinds), Job.updated_at >= lower)
            for job in session.scalars(jobs):
                yield from _scheduled(job)
        if category in (None, "watchlist"):
            for row in session.scalars(select(WatchlistState).where(WatchlistState.id.like("futu-heat:%"))):
                report = row.seed
                delivery = report.get("delivery") or {}
                if delivery.get("status") in STATUS_LABELS:
                    legacy = _saved_card(delivery, lambda: futu_heat._card(report))
                    yield _record("watchlist", row.id, "富途热榜 · 关注列表", delivery,
                                  fallback=row.created_at, legacy_card=legacy)


def _manual_records(category, folder, label):
    for path in (settings.storage / folder).glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem) or path.is_symlink():
            continue
        try:
            delivery = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(delivery, dict):
            yield _record(category, f"file:{category}:{path.stem}", label, delivery)


def listing(*, category: Category | None = None, day: date | None = None, limit=50, cursor=None):
    current = now()
    today = current.astimezone(BEIJING).date()
    if not 1 <= limit <= 100:
        raise ValueError("推送记录每页数量应为 1 至 100")
    if day and not date(1970, 1, 1) <= day <= today:
        raise ValueError("请选择今天或更早的有效日期")
    first = day or (today - timedelta(days=29))
    lower = datetime.combine(first, time.min, tzinfo=BEIJING).astimezone(timezone.utc).isoformat()
    end = (day or today).isoformat()
    first = first.isoformat()
    before = None
    if cursor is not None:
        at, separator, key = cursor.partition("|")
        instant = _instant(at)
        if not separator or instant is None or not key or len(cursor) > 180:
            raise ValueError("分页位置无效，请重新查询推送记录")
        before = instant.isoformat(timespec="microseconds"), key
    records = list(_database_records(lower, category)) if category != "test" else []
    if category in (None, "stock"):
        records.extend(_manual_records("stock", "research-notifications", "个股研究快照"))
    if category in (None, "test"):
        records.extend(_manual_records("test", "notification-probes", "飞书连通性测试"))
    selected = (row for row in records if row and first <= row["date"] <= end)

    def order(row):
        return row["at"], row["id"]

    rows = sorted((row for row in selected if before is None or order(row) < before), key=order, reverse=True)
    groups = {}
    stale = (current - timedelta(minutes=5)).isoformat(timespec="microseconds")
    for row in rows[:limit]:
        if row["status"] == "SENDING" and row["at"] < stale:
            row.update(status="UNKNOWN", status_label=STATUS_LABELS["UNKNOWN"],
                       error="发起投递超过 5 分钟，档案中仍无接收确认，请核对飞书群消息")
        groups.setdefault(row["date"], []).append(row)
    return {"groups":[{"date":group, "items":items} for group, items in groups.items()],
            "next_cursor":"|".join(order(rows[limit-1])) if len(rows) > limit else None}

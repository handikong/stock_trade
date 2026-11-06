import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now().isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def uid() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Security(Base):
    __tablename__ = "securities"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)


class Snapshot(Base):
    __tablename__ = "research_snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    payload: Mapped[dict] = mapped_column(JSON)


class Document(Base):
    __tablename__ = "source_documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[str] = mapped_column(String(60), default="")
    period_end: Mapped[str] = mapped_column(String(20), default="")
    acquired_at: Mapped[str] = mapped_column(String(60), default=stamp)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    file_name: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    segments: Mapped[list] = mapped_column(JSON)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Base):
    __tablename__ = "research_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    active_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="任务排队中")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    updated_at: Mapped[str] = mapped_column(String(60), default=stamp)


class SignalDelivery(Base):
    """群机器人投递档案；不保存Webhook或签名密钥，不代表成交。"""
    __tablename__ = "signal_deliveries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    destination_key: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    signal_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(24), default="READY")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    updated_at: Mapped[str] = mapped_column(String(60), default=stamp)
    payload: Mapped[dict] = mapped_column(JSON)
    last_error: Mapped[str] = mapped_column(Text, default="")


class WatchlistState(Base):
    __tablename__ = "watchlist_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seed: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    updated_at: Mapped[str] = mapped_column(String(60), default=stamp)


class ManualPosition(Base):
    """用户维护的当前持仓快照；不是策略模拟成交或券商成交账本。"""
    __tablename__ = "manual_positions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    quantity: Mapped[str] = mapped_column(String(60))
    average_cost: Mapped[str] = mapped_column(String(60))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    notes: Mapped[str] = mapped_column(String(1000), default="")
    version: Mapped[str] = mapped_column(String(32), default=uid)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    updated_at: Mapped[str] = mapped_column(String(60), default=stamp)


class WatchlistScan(Base):
    __tablename__ = "watchlist_scans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    finished_at: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    payload: Mapped[dict] = mapped_column(JSON)


class WatchlistSignal(Base):
    __tablename__ = "watchlist_signals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy_id: Mapped[str] = mapped_column(String(50), index=True)
    signal_date: Mapped[str] = mapped_column(String(10), index=True)
    first_seen_at: Mapped[str] = mapped_column(String(60), default=stamp)
    latest_revision_id: Mapped[str] = mapped_column(String(64))
    revision_count: Mapped[int] = mapped_column(Integer, default=1)


class WatchlistSignalRevision(Base):
    __tablename__ = "watchlist_signal_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("watchlist_signals.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(60), default=stamp)
    payload: Mapped[dict] = mapped_column(JSON)


engine = create_engine(settings.database, pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if settings.database.startswith("sqlite") else {})
Session = sessionmaker(engine, expire_on_commit=False)


def initialize():
    settings.storage.mkdir(parents=True, exist_ok=True)
    (settings.storage / "documents").mkdir(exist_ok=True)
    import yfinance as yf
    yf.set_tz_cache_location(str(settings.storage / "yf"))
    Base.metadata.create_all(engine)


def latest(session, security_id: str, kind: str):
    return session.scalar(select(Snapshot).where(Snapshot.security_id == security_id,
        Snapshot.kind == kind).order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))


def save_snapshot(session, security_id: str, kind: str, payload: dict):
    item = Snapshot(security_id=security_id, kind=kind, fingerprint=digest(payload), payload=payload)
    session.add(item)
    session.flush()
    return item


def serialize_snapshot(item):
    if item is None:
        return None
    created=item.created_at
    if created.tzinfo is None:
        created=created.replace(tzinfo=timezone.utc)
    return {"id":item.id,"version":item.fingerprint,"created_at":created.isoformat(),**item.payload}

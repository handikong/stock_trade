"""只读取公开RSS/Atom提供的财报简析节选，不抓文章全文、不调用大模型。"""
import fcntl
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .config import settings
from .documents import fetch_public
from .store import Session, Security, Snapshot, digest, latest, now, save_snapshot, stamp

logger = logging.getLogger(__name__)
ALGORITHM = "public-feed-earnings-excerpts-v1"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    url_template: str
    reference_url: str


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str
    enabled: bool
    cache_hours: int = Field(ge=1, le=168)
    retry_minutes: int = Field(ge=1, le=1440)
    lookback_days: int = Field(ge=1, le=365)
    max_items: int = Field(ge=1, le=20)
    summary_max_chars: int = Field(ge=100, le=3000)
    min_summary_chars: int = Field(ge=20, le=500)
    include_keywords: list[str] = Field(min_length=1)
    exclude_keywords: list[str]
    sources: list[Source] = Field(min_length=1, max_length=5)


def _safe_url(value):
    try:
        u = urlparse(value)
        return bool(u.scheme == "https" and u.hostname and not u.username and not u.password and u.port in (None, 443))
    except ValueError:
        return False


def policy():
    try:
        cfg = Policy.model_validate(json.loads(Path(settings.external_briefs_policy_file).read_text()))
        if len({s.id for s in cfg.sources}) != len(cfg.sources) or cfg.min_summary_chars > cfg.summary_max_chars:
            raise ValueError("来源ID重复或摘要长度配置无效")
        for source in cfg.sources:
            if not _safe_url(source.url_template.format(symbol="AAPL")) or not _safe_url(source.reference_url):
                raise ValueError("订阅源必须是公开HTTPS链接")
        return cfg
    except (OSError, ValueError, KeyError, IndexError, AttributeError, TypeError):
        raise ValueError("外部简析配置无效，请检查external-briefs.json中的公开RSS来源和参数") from None


def _date(value):
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    # 不把缺时区的发布时间猜成服务器时间，也不以抓取日代替发布日期。
    return d.astimezone(timezone.utc) if d.tzinfo is not None else None


def _children(node, name):
    return [c for c in node if c.tag.rsplit("}", 1)[-1].lower() == name]


def _value(node, names):
    for name in names:
        parts = _children(node, name)
        if parts:
            return "".join(parts[0].itertext()).strip()
    return ""


def _plain(value):
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"[\s\x00-\x1f]+", " ", soup.get_text(" ", strip=True)).strip()


def _mentions(value, keywords):
    return any(re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", value, re.I) for word in keywords)


def parse_feed(raw, source, feed_url, cfg, symbol):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("不接受包含DTD或实体声明的订阅源")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError("来源没有返回可解析的RSS/Atom，可能是登录、限流或验证页面") from None
    if root.tag.rsplit("}", 1)[-1].lower() not in ("rss", "feed", "rdf"):
        raise ValueError("来源不是RSS/Atom订阅内容")
    result = []; seen = set(); current = now()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = _plain(_value(node, ("title",)))
        summary = _plain(_value(node, ("description", "summary", "encoded", "content")))
        combined = title + " " + summary
        if not title or not _mentions(combined, cfg.include_keywords) or _mentions(combined, cfg.exclude_keywords):
            continue
        links = _children(node, "link")
        link = next((l.get("href") or (l.text or "").strip() for l in links if l.get("rel", "alternate") == "alternate"), "")
        if not _safe_url(link) or link in seen:
            continue
        published = _date(_value(node, ("pubdate", "published", "date", "updated")))
        if published and (published < current - timedelta(days=cfg.lookback_days) or published > current + timedelta(minutes=5)):
            continue
        seen.add(link)
        # RSS正文可能很长；只保留公开节选，绝不继续下载文章/付费正文或调用AI缩写。
        result.append({"symbol": symbol, "title": title[:300], "summary": summary[:cfg.summary_max_chars],
            "summary_truncated": len(summary) > cfg.summary_max_chars,
            "content_status": "public_excerpt" if len(summary) >= cfg.min_summary_chars else "headline_only",
            "publisher": _value(node, ("source",)) or source.name,
            "author": _value(node, ("creator", "author")), "source_id": source.id, "source_name": source.name,
            "source_url": link, "feed_url": feed_url, "published_at": published.isoformat() if published else None,
            "captured_at": stamp(), "feed_sha256": hashlib.sha256(raw).hexdigest(),
            "financial_period_match": "unconfirmed", "association": "股票订阅源关联，尚未核实文章主体与具体财报期",
            "nature": "external_commentary", "verified": False})
    return result


def view(payload):
    """只读视图校验时效，不请求来源、不改写保存时间。"""
    result = dict(payload or {})
    try:
        cfg = policy()
    except ValueError as exc:
        result.update(stale=True, warnings=[*(result.get("warnings") or []), {"message": str(exc)}])
        return result
    success = _date(result.get("last_success_at"))
    result["stale"] = (not cfg.enabled or result.get("algorithm") != ALGORITHM or result.get("policy") != cfg.model_dump()
                       or result.get("fetch_status") != "ok" or success is None or not timedelta(0) <= now() - success <= timedelta(hours=cfg.cache_hours))
    return result


def refresh(security_id, force=False):
    return {"status":"disabled","skipped":"外部简析自动获取已停用，请手工录入研究数据"}


def _refresh_legacy_entry(security_id, force=False):
    cfg = policy()
    if not cfg.enabled:
        return {"skipped": "外部简析已在配置中关闭"}
    directory = settings.storage / "brief-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (digest(security_id) + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该股票的外部简析正在更新，请稍后查询") from None
        try:
            return _refresh(security_id, force, cfg)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh(security_id, force, cfg):
    with Session() as session:
        security = session.get(Security, security_id); symbol = security.symbol
        if security.profile.get("type") == "ETF":
            return {"skipped": "ETF不自动套用公司财报简析订阅"}
        old = latest(session, security_id, "external_briefs")
        previous = dict(old.payload) if old else {}
        attempted = _date(previous.get("fetched_at"))
        if previous.get("algorithm") == ALGORITHM and previous.get("policy") == cfg.model_dump() and attempted:
            interval = cfg.cache_hours * 60 if previous.get("fetch_status") == "ok" else cfg.retry_minutes
            if not force and timedelta(0) <= now() - attempted < timedelta(minutes=interval):
                return {"cached": True, "warnings": previous.get("warnings", [])}
    items = []; warnings = []; sources = []; successes = 0
    for source in cfg.sources:
        url = source.url_template.format(symbol=quote(symbol, safe=""))
        with Session() as session:
            limits = session.scalars(select(Snapshot).where(Snapshot.kind == "brief_provider_limit").order_by(Snapshot.created_at.desc()).limit(100))
            limit = next((s for s in limits if s.payload.get("source_id") == source.id), None)
        until = _date((limit.payload if limit else {}).get("retry_at"))
        if until and until > now():
            message = f"{source.name}限流冷却中，本次不请求；force也不绕过冷却"
            warnings.append({"message": message}); sources.append({"source_id": source.id, "status": "cooldown", "retry_at": until.isoformat()})
            continue
        logger.info("[external_briefs] 正在读取%s公开RSS摘要，不抓文章全文", source.name)
        try:
            raw, _, final_url = fetch_public(url, limit=2_000_000)
            parsed = parse_feed(raw, source, final_url, cfg, symbol)
            items.extend(parsed); successes += 1
            sources.append({"source_id": source.id, "status": "ok", "matched": len(parsed)})
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            message = f"{source.name}公开简析未取得" + (f"（HTTP {status}）" if isinstance(status, int) else "，来源不可用或格式不符")
            warnings.append({"message": message}); sources.append({"source_id": source.id, "status": "failed", "http_status": status})
            if status == 429:
                with Session() as session:
                    save_snapshot(session, security_id, "brief_provider_limit", {"source_id": source.id, "retry_at": (now() + timedelta(minutes=cfg.retry_minutes)).isoformat()})
                    session.commit()
    items.sort(key=lambda item: (item["content_status"] == "public_excerpt", item.get("published_at") or ""), reverse=True)
    items = list({item["source_url"]: item for item in reversed(items)}.values())
    items.sort(key=lambda item: (item["content_status"] == "public_excerpt", item.get("published_at") or ""), reverse=True)
    if successes and not any(i["content_status"] == "public_excerpt" for i in items):
        warnings.append({"message": "公开源未提供符合筛选的实质简析；可能仅有标题、日期窗口外文章或没有相关条目"})
    captured = stamp()
    with Session() as session:
        for item in items:
            content_key = digest({k: v for k, v in item.items() if k not in ("captured_at", "feed_sha256")})
            saved = session.scalar(select(Snapshot).where(Snapshot.security_id == security_id, Snapshot.kind == "external_brief_article", Snapshot.fingerprint == content_key))
            if saved is None:
                saved = save_snapshot(session, security_id, "external_brief_article", dict(item))
                saved.fingerprint = content_key
            item["archive_id"] = saved.id
        payload = {"algorithm": ALGORITHM, "policy": cfg.model_dump(), "symbol": symbol, "fetched_at": captured,
            "fetch_status": "ok" if successes == len(cfg.sources) else "partial" if successes else "failed",
            "last_success_at": captured if successes else previous.get("last_success_at"),
            "items": items[:cfg.max_items] if successes else previous.get("items", []), "sources": sources, "warnings": warnings,
            "message": "公开网站/作者观点节选，未核实财报期；不替代SEC财务事实、公司指引或估值假设，不调用DeepSeek"}
        saved = save_snapshot(session, security_id, "external_briefs", payload)
        session.commit()
        return {"snapshot_id": saved.id, "items": len(payload["items"]), "warnings": warnings}

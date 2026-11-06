import hashlib
import io
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy import select
from .config import settings
from .store import Document, Session, uid


def check_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("资料地址需要不含用户名密码的公开HTTPS链接")
    if parsed.port not in (None, 443):
        raise ValueError("资料地址只允许标准HTTPS端口")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("不能抓取本机、内网或保留网络地址")


def fetch_public(url: str, limit: int = 12_000_000) -> tuple[bytes, str, str]:
    # 每次重定向都重新校验，禁止公开链接跳转到本地管理服务。
    with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            check_url(url)
            headers = {"User-Agent": settings.sec_user_agent or "StockResearch/0.1"}
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code in (301,302,303,307,308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("资料来源返回了无效跳转")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("资料超过大小限制，请改用较小的官方材料或文本导入")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", ""), url
    raise ValueError("资料跳转次数过多")


def extract_text(raw: bytes, content_type: str, filename: str) -> str:
    if raw.startswith(b"%PDF") or "pdf" in content_type or filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("不处理加密PDF，请使用有权获取的可读文本")
        text = "\n\n".join(f"[第{i+1}页]\n{p.extract_text() or ''}" for i,p in enumerate(reader.pages))
    else:
        text = raw.decode("utf-8-sig", errors="replace")
        if "html" in content_type or re.search(r"<(html|body|div|p)[\s>]", text, re.I):
            soup=BeautifulSoup(text,"html.parser")
            for tag in soup(["script","style","nav","footer","noscript"]):
                tag.decompose()
            text=soup.get_text("\n",strip=True)
    text=re.sub(r"[ \t]+", " ", text)
    text=re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 80:
        raise ValueError("未提取到足够可读正文；扫描PDF或音频需先转录，再导入文本")
    return text


def segment(text: str, doc_id: str, kind: str) -> tuple[list[dict], bool]:
    section="prepared" if kind=="call" else "document"
    found_qa=False
    speaker=""
    parts=[]
    for line in text.splitlines():
        line=line.strip()
        if not line:
            continue
        if kind=="call" and (re.match(r"^(questions?[\s-]*(and|&)[\s-]*answers?|q\s*&\s*a)(\s+session)?\s*[:：]?\s*$",line,re.I)
                or re.search(r"(we will now|we'll now|now begin).{0,35}(question.and.answer|q&a)",line,re.I)):
            section="qa"
            found_qa=True
        match=re.match(r"^([A-Z][A-Za-z .,'’\-]{2,65})\s*[:：]\s*(.*)",line)
        if match:
            speaker=match[1]
        # 长段拆分也保留同一来源与顺序，不悄悄截去后半部分问答。
        for offset in range(0,len(line),2500):
            parts.append({"id":f"{doc_id}:p{len(parts)+1}","section":section,
                "speaker":speaker,"speaker_status":"inferred" if speaker else "unknown",
                "text":line[offset:offset+2500]})
    return parts, found_qa


def saved_document(security_id, kind, source_url, period_end, accession=None):
    """原文按来源、财报期及申报版本复用；正文或原件缺失时允许补齐。"""
    with Session() as session:
        rows=session.scalars(select(Document).where(Document.security_id==security_id,
            Document.kind==kind,Document.source_url==source_url,Document.period_end==period_end)
            .order_by(Document.acquired_at.desc()))
        for item in rows:
            if accession and item.metadata_json.get("accession")!=accession:
                continue
            if item.text and item.segments and (settings.storage/"documents"/item.file_name).is_file():
                return document_summary(item)
    return None


def archive(security_id: str, kind: str, title: str, raw: bytes, filename: str,
            source_url="", published_at="", period_end="", content_type="text/plain", metadata=None) -> dict:
    text=extract_text(raw,content_type,filename)
    hashed=hashlib.sha256(raw).hexdigest()
    with Session() as session:
        existing=session.scalar(select(Document).where(Document.security_id==security_id,
            Document.kind==kind, Document.content_hash==hashed, Document.source_url==source_url))
        if existing:
            # 原件意外丢失时补回同一内容，已有哈希不能替代实际文件。
            original=settings.storage/"documents"/existing.file_name
            if not original.is_file():
                original.write_bytes(raw)
            if not existing.text or not existing.segments:
                parts,qa=segment(text,existing.id,kind)
                existing.text=text
                existing.segments=parts
                existing.metadata_json={**existing.metadata_json,"qa_detected":qa,
                    "section_status":"heuristic" if qa else "unpartitioned" if kind=="call" else "not_applicable"}
                session.commit()
            return document_summary(existing)
        did=uid()
        parts,qa=segment(text,did,kind)
        extension="pdf" if raw.startswith(b"%PDF") else "html" if "html" in content_type else "txt"
        saved=f"{hashed}.{extension}"
        path=settings.storage/"documents"/saved
        if not path.exists():
            path.write_bytes(raw)
        item=Document(id=did,security_id=security_id,kind=kind,title=title[:500],source_url=source_url,
            published_at=published_at,period_end=period_end,content_hash=hashed,file_name=saved,
            text=text,segments=parts,metadata_json={**(metadata or {}),"original_name":filename,
                "qa_detected":qa,"section_status":"heuristic" if qa else "unpartitioned" if kind=="call" else "not_applicable",
                "association":"来源与证券的关联需核对；不将用户指定关联冒充官方认证"})
        session.add(item)
        session.commit()
        return document_summary(item)


def document_summary(item: Document) -> dict:
    return {"id":item.id,"kind":item.kind,"title":item.title,"source_url":item.source_url,
        "published_at":item.published_at,"period_end":item.period_end,"acquired_at":item.acquired_at,
        "hash":item.content_hash,"characters":len(item.text),"metadata":item.metadata_json}

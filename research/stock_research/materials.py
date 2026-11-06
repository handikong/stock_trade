"""获取两期业绩附件，提取待核对指引片段；不自动获取电话会或调用模型。"""
import json
import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from sqlalchemy import select
from .config import settings
from .documents import archive, extract_text, fetch_public, saved_document
from .providers import failure_detail
from .report_scope import scope_config
from .store import Document, Security, Session, digest, latest, save_snapshot, stamp

logger=logging.getLogger(__name__)
MATERIALS_VERSION="release-guidance-no-calls-v1"
GUIDANCE_VERSION="keyword-guidance-permanent-sources-v1"


def download_material(security_id, url, kind, title, period_end, published_at="", metadata=None):
    existing=saved_document(security_id,kind,url,period_end,(metadata or {}).get("accession"))
    if existing:
        return existing,None,url
    raw,ctype,actual=fetch_public(url)
    if kind=="call":
        text=extract_text(raw,ctype,actual.rsplit("/",1)[-1])
        if len(text)<1000 or not re.search(r"question.{0,10}answer|\bQ\s*&\s*A\b|\bOperator\s*:",text,re.I):
            raise ValueError("该链接尚不能确认为含问答的电话会正文，保留为候选链接，不当作完整转录")
    result=archive(security_id,kind,title,raw,actual.rsplit("/",1)[-1],source_url=actual,
        period_end=period_end,published_at=published_at,content_type=ctype,metadata=metadata)
    return result,raw,actual


def sec_releases(security_id, filings):
    name=filings.get("submissions_raw_file","")
    if not re.fullmatch(r"[0-9a-f]{64}\.json",name):
        raise ValueError("尚无本轮SEC提交索引原始文件，无法可靠定位业绩附件；先刷新SEC或导入公司发布稿")
    recent=json.loads((settings.storage/"documents"/name).read_text())["filings"]["recent"]
    with Session() as session:
        cik=session.get(Security,security_id).profile.get("cik")
    if not cik:
        raise ValueError("SEC公司映射缺失，无法获取业绩附件")
    docs=[];warnings=[]
    # 每一期仅定位一份最接近定期报告公布时点的业绩8-K；不遍历下载任意8-K。
    for period in filings.get("report_periods",[]):
        mains=[r for r in filings.get("items",[]) if r["period_end"]==period]
        main=min(mains,key=lambda r:r["filed"])
        anchor=date.fromisoformat(main["filed"])
        matches=[]
        for i,form in enumerate(recent.get("form",[])):
            if form not in ("8-K","8-K/A"):
                continue
            filed=date.fromisoformat(recent["filingDate"][i])
            items=recent.get("items",[""]*len(recent["form"]))[i]
            if not date.fromisoformat(period)<=filed<=anchor+timedelta(days=7) or abs((filed-anchor).days)>14:
                continue
            if items and "2.02" not in items:
                continue
            matches.append((abs((filed-anchor).days),i))
        if not matches:
            warnings.append({"message":f"{period}附近未找到业绩8-K，尚未取得发布稿附件"});continue
        _,i=min(matches)
        accession=recent["accessionNumber"][i]
        base=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/"
        try:
            if not recent.get("items",[""]*len(recent["form"]))[i]:
                cover,ctype,cover_url=fetch_public(base+recent["primaryDocument"][i])
                text=extract_text(cover,ctype,cover_url.rsplit("/",1)[-1])
                if not re.search(r"Item\s*2\s*\.\s*02|Results of Operations and Financial Condition",text,re.I):
                    raise ValueError(f"{period}候选8-K无法确认为业绩披露，未把任意附件当作发布稿")
            logger.info("[materials] 正在获取%s业绩附件索引",period)
            raw,_,_=fetch_public(base+accession+"-index.html")
            soup=BeautifulSoup(raw,"html.parser")
            links=[]
            for row in soup.select("table.tableFile tr"):
                cells=row.find_all("td")
                if len(cells)<4 or not re.fullmatch(r"EX-99(?:\.\d+)?",cells[3].get_text(strip=True),re.I):
                    continue
                link=cells[2].find("a",href=True)
                if not link:
                    continue
                url=urljoin(base,link["href"])
                if not url.startswith(base) or url in links:
                    continue
                links.append(url)
            if not links:
                warnings.append({"message":f"{period}业绩8-K未找到可读取EX-99附件，不以封面冒充发布稿"})
            if len(links)>settings.material_link_limit:
                warnings.append({"message":f"{period}共有{len(links)}份EX-99附件，本次按MATERIAL_LINK_LIMIT仅取{settings.material_link_limit}份",
                    "remaining_urls":links[settings.material_link_limit:]})
            for j,url in enumerate(links[:settings.material_link_limit]):
                try:
                    doc,_,_=download_material(security_id,url,"release" if j==0 else "presentation",
                        f"业绩附件 · {period} · {j+1}",period,recent["filingDate"][i],
                        {"origin":"SEC业绩附件","accession":accession,"period_association":"按定期报告与业绩8-K公布窗口关联，待核对"})
                    docs.append(doc)
                except Exception as exc:
                    warnings.append(failure_detail(exc,"SEC业绩附件"))
        except Exception as exc:
            warnings.append(failure_detail(exc,"SEC业绩附件索引"))
    return docs,warnings


def guidance_draft(security_id, periods):
    with Session() as session:
        docs=list(session.scalars(select(Document).where(Document.security_id==security_id,
            Document.kind.in_(["call","release","presentation"])).order_by(Document.acquired_at.desc())))
        selected=[];seen=set()
        for doc in docs:
            if doc.period_end not in periods:
                continue
            key=(doc.kind,doc.source_url or doc.title,doc.period_end)
            if key in seen:
                continue
            seen.add(key)
            selected.append(doc)
        cache_key=digest({"version":GUIDANCE_VERSION,"scope":scope_config(),"periods":periods,
            "documents":[(d.id,d.content_hash) for d in selected]})
        previous=latest(session,security_id,"guidance_draft")
        if previous and previous.fingerprint==cache_key:
            return previous.id
        items=[]
        for doc in selected:
            for segment in doc.segments:
                if re.search(r"\bguidance\b|\boutlook\b|\bwe expect\b|\bwe anticipate\b|\bnext quarter\b|业绩指引|下季度",segment["text"],re.I):
                    items.append({"text":segment["text"],"evidence_id":segment["id"],"document_id":doc.id,
                        "report_period":doc.period_end,"published_at":doc.published_at,"review_status":"draft"})
        item=save_snapshot(session,security_id,"guidance_draft",{"fetched_at":stamp(),"scope":scope_config(),
            "algorithm":GUIDANCE_VERSION,"source_ids":[d.id for d in selected],
            "items":items,"message":"关键词定位的原文片段，可能只是当期回顾；不等于已确认指引，不推断目标期间或数值"})
        item.fingerprint=cache_key
        session.commit()
    return item.id


def refresh_materials(security_id,force=False):
    return {"status":"disabled","skipped":"财报/业绩附件/电话会议自动读取已停用，请手工录入研究数据"}


def _refresh_materials_legacy(security_id,force=False):
    with Session() as session:
        security=session.get(Security,security_id)
        if security.profile.get("type")=="ETF":
            return {"skipped":"ETF不获取公司业绩附件"}
        filings=latest(session,security_id,"filings")
        previous=latest(session,security_id,"materials")
        document_ids=(previous.payload.get("document_ids") or []) if previous else []
        archived=[session.get(Document,did) for did in document_ids]
        use_archive=(not force and previous and previous.payload.get("scope")==scope_config()
                and previous.payload.get("materials_version")==MATERIALS_VERSION
                and previous.payload.get("filings_version")== (filings.fingerprint if filings else None)
                and archived and all(d and d.text and d.segments and
                    (settings.storage/"documents"/d.file_name).is_file() for d in archived)
                and not previous.payload.get("warnings"))
        payload=filings.payload if filings else {}
        periods=payload.get("report_periods",[])
    if not periods:
        raise ValueError("尚无最近两期定期财报索引，无法定位对应业绩材料")
    if use_archive:
        # 新导入的官方材料也应触发本地指引提取；与原文下载是否复用分别判断。
        draft_id=guidance_draft(security_id,periods)
        return {"cached":True,"retention":"permanent","guidance_draft_id":draft_id}
    archived=[];warnings=[]
    try:
        releases,errors=sec_releases(security_id,payload)
        archived.extend(releases);warnings.extend(errors)
    except Exception as exc:
        warnings.append(failure_detail(exc,"SEC业绩发布稿"))
    # 用户取消自动电话会获取：不扫描转录链接、不调用FMP转录接口，不产生缺失警告。
    draft_id=guidance_draft(security_id,periods)
    with Session() as session:
        save_snapshot(session,security_id,"materials",{"fetched_at":stamp(),"scope":scope_config(),
            "retention":"permanent",
            "materials_version":MATERIALS_VERSION,"calls_policy":"disabled",
            "filings_version":filings.fingerprint,"report_periods":periods,"document_ids":[d["id"] for d in archived],
            "candidates":[],"missing_call_periods":[],"guidance_draft_id":draft_id,"warnings":warnings})
        session.commit()
    return {"archived":len(archived),"calls_policy":"disabled","warnings":warnings}

"""命令行资料与假设导入，和网页复用已有存档/确认/计算规则。"""
import json
import mimetypes
from datetime import date
from pathlib import Path
from .documents import archive, fetch_public
from .earnings import freeze_preview, record_event, record_guidance
from .store import Document, Security, Session, save_snapshot
from sqlalchemy import select
from .valuation import calculate


def load_json(path):
    path=Path(path).expanduser().resolve()
    if path.stat().st_size>12_000_000:
        raise ValueError("输入JSON文件不能超过12MB")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")),path.parent
    except (ValueError,UnicodeError) as exc:
        raise ValueError("输入文件不是有效UTF-8 JSON，请核对格式") from exc


def import_materials(security_id, path):
    data,folder=load_json(path)
    rows=data if isinstance(data,list) else [data]
    if not rows or len(rows)>100:
        raise ValueError("资料清单需要1至100条")
    imported=[]
    for row in rows:
        if not isinstance(row,dict) or row.get("kind") not in ("call","filing","release","presentation") or not row.get("title"):
            raise ValueError("每条资料需要kind(call/filing/release/presentation)与title")
        if row.get("period_end"):
            date.fromisoformat(row["period_end"])
        url=row.get("url","")
        if row.get("text"):
            raw=row["text"].encode();filename="import.txt";ctype="text/plain"
        elif row.get("file"):
            source=(folder/Path(row["file"]).expanduser()).resolve()
            if source.stat().st_size>12_000_000:
                raise ValueError("资料文件不能超过12MB")
            raw=source.read_bytes();filename=source.name;ctype=mimetypes.guess_type(filename)[0] or "text/plain"
        elif url:
            raw,ctype,url=fetch_public(url);filename=url.rsplit("/",1)[-1]
        else:
            raise ValueError("资料需要text、file或url之一")
        imported.append(archive(security_id,row["kind"],row["title"],raw,filename,source_url=url,
            period_end=row.get("period_end",""),published_at=row.get("published_at",""),content_type=ctype,
            metadata={"origin":"用户命令行导入"}))
    return {"document_ids":[d["id"] for d in imported]}


def import_event(security_id,path):
    data,_=load_json(path)
    if not isinstance(data,dict) or not all(data.get(k) for k in ("period_end","scheduled_at","source_url")):
        raise ValueError("财报事件需要period_end、含时区的scheduled_at、source_url")
    if not isinstance(data.get("confirmed",False),bool):
        raise ValueError("财报事件confirmed需要true或false")
    return {"snapshot_id":record_event(security_id,data)}


def import_valuations(security_id,path, *, confirm=False):
    data,_=load_json(path)
    from .eps_only import SCHEMA, is_simple
    from .manual_multiples import SCHEMA as PE_SCHEMA, is_simple as has_manual_pe
    if isinstance(data,dict) and (is_simple(data) or has_manual_pe(data) or data.get("schema_version") in ("manual-valuation-v1", "manual-valuation-v2", SCHEMA, PE_SCHEMA)):
        from .manual_valuation import save
        return save(security_id,data,confirm=confirm)
    if confirm:
        raise ValueError("确认估值支持手工EPS＋PE JSON及旧v1/v2/v3；旧计算器结果只保留研究档案")
    rows=data if isinstance(data,list) else [data]
    if not rows or len(rows)>20:
        raise ValueError("估值输入需要1至20个情景模型")
    with Session() as session:
        profile=session.get(Security,security_id).profile
        ids=[]
        for row in rows:
            if not isinstance(row,dict):
                raise ValueError("估值输入需要完整JSON对象")
            try:
                result=calculate(row,profile)
            except (KeyError,TypeError,AttributeError) as exc:
                raise ValueError("估值字段不完整，请提供完整EPS或DCF假设") from exc
            result.update({"origin":"manual_calculator","historical_only":True})
            ids.append(save_snapshot(session,security_id,"valuation",result).id)
        session.commit()
    return {"snapshot_ids":ids}


def import_guidance(security_id,path):
    data,_=load_json(path)
    required=("metric","period_end","basis","unit","low","high","evidence_id","source_note")
    if not isinstance(data,dict) or any(data.get(k) in (None,"") for k in required):
        raise ValueError("指引需要metric、period_end、basis、unit、low/high、evidence_id与source_note")
    if data["metric"] not in ("EPS","revenue","operating_margin","capex"):
        raise ValueError("指引metric只支持EPS、revenue、operating_margin、capex")
    with Session() as session:
        docs=session.scalars(select(Document).where(Document.security_id==security_id))
        allowed={s["id"] for d in docs for s in d.segments}
    if data["evidence_id"] not in allowed:
        raise ValueError("请引用本股票已保存原文中的真实段落编号")
    return {"snapshot_id":record_guidance(security_id,data)}

from datetime import date, datetime, timezone
from sqlalchemy import select
from .store import Session, Snapshot, latest, save_snapshot, stamp
from .valuation import number


def record_event(security_id: str, payload: dict):
    scheduled=datetime.fromisoformat(payload["scheduled_at"].replace("Z","+00:00"))
    if scheduled.tzinfo is None:
        raise ValueError("财报公布时间必须包含时区，例如2026-11-01T16:00:00-05:00")
    date.fromisoformat(payload["period_end"])
    with Session() as session:
        item=save_snapshot(session,security_id,"earnings",{**payload,"recorded_at":stamp(),
            "scheduled_at":scheduled.astimezone(timezone.utc).isoformat(),"source_type":"user_verified"})
        session.commit()
        return item.id


def freeze_preview(security_id: str):
    with Session() as session:
        event=latest(session,security_id,"earnings")
        estimates=latest(session,security_id,"estimates")
        if not event or not estimates:
            raise ValueError("需要先记录财报事件时间，并获取当前一致预期")
        if not event.payload.get("confirmed"):
            raise ValueError("请先核对公司确认的财报日期，估计日程不能用于正式冻结前瞻")
        scheduled=datetime.fromisoformat(event.payload["scheduled_at"])
        captured=datetime.fromisoformat(estimates.payload["captured_at"])
        current=datetime.now(timezone.utc)
        if current>=scheduled or captured>=scheduled:
            raise ValueError("财报已到公布时点，不能把现在的预期回填成财报前快照")
        guidance=latest(session,security_id,"guidance")
        old=latest(session,security_id,"preview")
        if old and old.payload.get("event_id")==event.id:
            return old.id
        item=save_snapshot(session,security_id,"preview",{"event_id":event.id,"event":event.payload,
            "estimates_id":estimates.id,"estimates":estimates.payload,
            "guidance":guidance.payload if guidance else None,"frozen_at":stamp(),
            "message":"这是公布前已取得的预期快照；供应商目标期间/口径未知时不计算历史超预期"})
        session.commit()
        return item.id


def record_guidance(security_id: str, p: dict):
    date.fromisoformat(p["period_end"])
    low,high=number(p.get("low"),"指引下限"),number(p.get("high"),"指引上限")
    if low>high:
        raise ValueError("指引下限不能高于上限")
    with Session() as session:
        history=session.scalars(select(Snapshot).where(Snapshot.security_id==security_id,Snapshot.kind=="guidance")
            .order_by(Snapshot.created_at.desc()))
        previous=next((row for row in history if all(row.payload.get(k)==p.get(k)
            for k in ("period_end","metric","basis","unit"))),None)
        change="没有可比的旧指引"
        if previous and all(previous.payload.get(k)==p.get(k) for k in ("period_end","metric","basis","unit")):
            old_low,old_high=number(previous.payload["low"],"旧下限"),number(previous.payload["high"],"旧上限")
            change="上调" if low>old_low and high>old_high else "下调" if low<old_low and high<old_high else "维持" if low==old_low and high==old_high else "区间变化，方向需复核"
        item=save_snapshot(session,security_id,"guidance",{**p,"low":str(low),"high":str(high),
            "change":change,"previous_id":previous.id if previous else None,"recorded_at":stamp(),
            "review_status":"user_verified","message":"人工核对记录，与AI提取草稿分开"})
        session.commit()
        return item.id

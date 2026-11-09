"""手工维护沪深股票与 ETF 的人民币持仓快照。"""
from sqlalchemy import select

from . import cn_sources
from .store import CNManualPosition, CNPositionChange, Security, Session, stamp, uid
from .valuation import number
from .workspace import Conflict, position_lock, quote, summarize_holdings


def _view(row):
    return {"id":row.id,"symbol":row.symbol,"name":row.name,"quantity":row.quantity,
            "average_cost":row.average_cost,"notes":row.notes,"version":row.version,
            "active":row.active,"updated_at":row.updated_at}


def positions(session):
    return list(session.scalars(select(CNManualPosition).where(CNManualPosition.active.is_(True))
                                .order_by(CNManualPosition.symbol)))


def holdings():
    with Session() as session:
        rows = positions(session)
        securities = {s.symbol:s for s in session.scalars(select(Security).where(
            Security.symbol.in_([row.symbol for row in rows])))} if rows else {}
        items = []
        for row in rows:
            security = securities.get(row.symbol)
            items.append({**_view(row),"name":row.name or (security.profile.get("name") if security else "") or row.symbol,
                          "quote":quote(session,security,"CN",daily_change=True)})
        return summarize_holdings(items,"CN","CNY")


def _active_position(session, position_id, version):
    row = session.get(CNManualPosition,position_id)
    if row is None or not row.active:
        raise LookupError("A 股持仓不存在或已移除")
    if version != row.version:
        raise Conflict("A 股持仓已变化，请重新读取后再操作")
    return row


def _record(session, row, action, previous):
    row.version = uid()
    row.updated_at = stamp()
    session.flush()
    result = _view(row)
    session.add(CNPositionChange(position_id=row.id,
                                payload={"action":action,"previous":previous,"position":result}))
    session.commit()
    return result


def save(data, position_id=None):
    code = cn_sources.symbol(data["symbol"],allow_etf=True)
    quantity = number(data["quantity"],"持有数量")
    average_cost = number(data["average_cost"],"人民币成本均价")
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError("A 股持有数量必须填写正整数，按实际持有的股数或 ETF 份数填写")
    if average_cost <= 0:
        raise ValueError("人民币成本均价必须大于 0")
    with position_lock(), Session() as session:
        if position_id:
            row = _active_position(session,position_id,data.get("expected_version"))
            if row.symbol != code:
                raise ValueError("编辑持仓时不能替换证券代码")
        else:
            row = session.scalar(select(CNManualPosition).where(CNManualPosition.symbol==code))
            if row and row.active:
                raise Conflict("该证券已在 A 股持仓中，请编辑已有记录")
        previous = _view(row) if row else None
        if row is None:
            row = CNManualPosition(symbol=code)
            session.add(row)
        row.name = data["name"].strip()
        row.quantity = format(quantity.to_integral_value(),"f")
        row.average_cost = format(average_cost,"f")
        row.notes = data["notes"].strip()
        row.active = True
        return _record(session,row,"EDIT" if position_id else "ADD",previous)


def remove(position_id, version):
    with position_lock(), Session() as session:
        row = _active_position(session,position_id,version)
        previous = _view(row)
        row.active = False
        _record(session,row,"REMOVE",previous)
    return {"message":"已移除 A 股持仓，历史记录保留"}

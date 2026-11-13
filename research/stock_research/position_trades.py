"""登记券商已成交的持仓变动，原子更新持仓与对应市场现金。"""
from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy import case, select

from . import account_equity, cn_holdings, cn_sources, market, workspace
from .store import (CNManualPosition, CNPositionChange, ManualPosition, PositionPlan,
                    PositionProtection, Security, Session, Snapshot, now, save_snapshot, stamp, uid)
from .valuation import number

TRADE_TYPES = {"买入": "BUY", "加仓": "ADD", "减仓": "REDUCE", "清仓": "CLOSE"}


def history(region, *, symbol=None, kind=None, start=None, end=None, page=1, limit=30):
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    cn = region == "CN"
    if symbol and symbol.strip():
        symbol = cn_sources.symbol(symbol, allow_etf=True) if cn else workspace.symbol(symbol)
    else:
        symbol = None
    if cn:
        archive = CNPositionChange
        code = CNManualPosition.symbol
        action = archive.payload["action"].as_string()
        query = select(archive, code).join(CNManualPosition, CNManualPosition.id == archive.position_id)
        query = query.where(action.in_(tuple(TRADE_TYPES)))
    else:
        archive = Snapshot
        code = Security.symbol
        action = case(
            (archive.kind == "portfolio_confirmed_sale", archive.payload["action"].as_string()),
            (archive.payload["previous_position"]["quantity"].as_string().is_not(None), "加仓"),
            else_="买入",
        )
        query = select(archive, code).join(Security, Security.id == archive.security_id)
        query = query.where(archive.kind.in_(("portfolio_confirmed_fill", "portfolio_confirmed_sale")))
    fill = archive.payload["fill"]
    session_date = fill["session"].as_string()
    query = query.add_columns(action).where(session_date.is_not(None))
    if symbol:
        query = query.where(code == symbol)
    if kind:
        query = query.where(action.in_([name for name, value in TRADE_TYPES.items() if value == kind]))
    if start:
        query = query.where(session_date >= start.isoformat())
    if end:
        query = query.where(session_date <= end.isoformat())
    query = query.order_by(session_date.desc(), archive.created_at.desc(), archive.id.desc())
    with Session() as db:
        records = db.execute(query.offset((page - 1) * limit).limit(limit + 1)).all()
        items = []
        for row, code, action in records[:limit]:
            fill = row.payload["fill"]
            sold = action in ("减仓", "清仓")
            net = fill.get("net_amount")
            if net is None:
                # 早期美股买入只存数量、价格及费用，从原成交字段计算净支出。
                net = account_equity.display_amount(Decimal(fill["quantity"]) * Decimal(fill["price"])
                                                    + Decimal(fill["fee"]))
            items.append({
                "id": row.id, "symbol": code, "kind": TRADE_TYPES[action], "action": action,
                "session": fill["session"], "quantity": str(fill["quantity"]), "price": fill["price"],
                "fee": fill["fee"], "cash_flow": net if sold else "-" + net,
                "realized_pnl": fill.get("realized_pnl"),
                "remaining_quantity": row.payload["position"]["quantity"],
            })
    return {"items": items, "page": page, "has_more": len(records) > limit,
            "currency": account_equity.CURRENCIES[region]}


def _reduce_us_protection(session, row, old_qty, sold):
    protection = session.get(PositionProtection, row.id)
    if protection is None:
        return
    if not row.active:
        session.delete(protection)
        return
    meta = protection.payload
    remaining = Decimal(row.quantity)
    # 先扣未绑定保护线的旧仓，再按登记顺序扣减带保护线的批次。
    old_lots = meta.get("lots", [])
    unbound = max(0, old_qty - sum(Decimal(lot["quantity"]) for lot in old_lots))
    to_remove = max(0, sold - unbound)
    bound_tactical_sold = 0
    kept = []
    for lot in old_lots:
        quantity = Decimal(lot["quantity"])
        removed = min(to_remove, quantity)
        to_remove -= removed
        if lot.get("tactical"):
            bound_tactical_sold += removed
        if quantity > removed:
            kept.append({**lot, "quantity": format(quantity - removed, "f")})
    # 旧批次没有资金池标签。剩余股数内保守保留其机动归属，避免低估风险。
    tactical = max(0, Decimal(meta.get("tactical_qty", "0")) - bound_tactical_sold)
    protection.payload = {**meta, "lots": kept, "tactical_qty": str(min(remaining, tactical))}


def record(region, position_id, data, *, sell):
    cn = region == "CN"
    quantity = number(data["quantity"], "成交数量")
    price = number(data["price"], "成交价")
    fee = number(data["fee"], "成交费用")
    if quantity <= 0 or (cn and quantity != quantity.to_integral_value()):
        raise ValueError("A 股成交数量必须为正整数" if cn else "成交数量必须大于 0")
    if price <= 0 or fee < 0 or fee != fee.quantize(account_equity.CENT):
        raise ValueError("成交价必须大于 0，成交费用最多两位小数且不能为负")
    day = data["execution_session"]
    zone = account_equity.ZONES[region]
    calendar, _ = market.calendar_context(region)
    if day > now().astimezone(zone).date() or not calendar.is_session(day.isoformat()):
        raise ValueError("请填写成交市场当地的实际交易日")

    model, view = (CNManualPosition, cn_holdings._view) if cn else (ManualPosition, workspace._position)
    opening = position_id is None
    code = cn_sources.symbol(data["symbol"], allow_etf=True) if opening else None
    with workspace.position_lock(), Session() as session, localcontext() as context:
        context.prec = 64
        if opening:
            row = session.scalar(select(model).where(model.symbol == code))
            if row and row.active:
                raise workspace.Conflict("该证券已有持仓，请从持仓行登记加仓")
            if row is None:
                row = model(symbol=code, name="", notes="")
                session.add(row)
            previous, old_qty, basis = None, Decimal(0), Decimal(0)
        else:
            row = session.get(model, position_id)
            if row is None or not row.active:
                raise LookupError("持仓不存在或已清仓，请刷新持仓")
            if row.version != data["expected_version"]:
                raise workspace.Conflict("持仓已变化，请重新核对股数，避免重复登记")
            previous = view(row)
            old_qty, basis = Decimal(row.quantity), Decimal(row.average_cost)
        new_qty = old_qty - quantity if sell else old_qty + quantity
        if new_qty < 0:
            raise ValueError("卖出数量不能超过当前持仓")
        if new_qty > Decimal("1e18"):
            raise ValueError("成交后持仓数量超出范围")
        action = ("清仓" if not new_qty else "减仓") if sell else "买入" if opening else "加仓"
        amount = Decimal(account_equity.display_amount(quantity * price + (-fee if sell else fee)))
        if amount <= 0:
            raise ValueError("扣除费用后的卖出净额必须大于 0" if sell else "加仓金额必须大于 0")
        if amount > Decimal("1e18"):
            raise ValueError("成交金额超出范围")

        account = account_equity._cash(session, region)
        if account is None or account.payload.get("total_cash") is None:
            raise ValueError("请先按券商余额填写该市场的账户现金")
        previous_cash = {**account.payload, "version": account.version}
        booked = data["cash_already_recorded"]
        if booked:
            cash_day = datetime.fromisoformat(account.payload["cash_as_of"]).astimezone(zone).date()
            if cash_day < day:
                raise ValueError("现金确认日期早于成交日，请核对现金是否已包含该笔成交")
        if not booked:
            total = Decimal(account.payload["total_cash"]) + (amount if sell else -amount)
            if not 0 <= total <= Decimal("1e18"):
                raise ValueError("成交后总现金超出范围，请先核对券商余额")
            account.payload = {**account.payload, "total_cash": account_equity.display_amount(total)}
            if not cn and data["proceeds_available"]:
                available = Decimal(account.payload["available_cash"]) + amount
                account.payload = {**account.payload, "available_cash": account_equity.display_amount(available)}

        recorded_at = stamp()
        account.version, account.updated_at = uid(), recorded_at
        row.quantity = format(new_qty.to_integral_value() if cn else new_qty, "f")
        row.active = new_qty > 0
        row.version, row.updated_at = uid(), recorded_at
        if not sell:
            average = ((old_qty * basis + amount) / new_qty).quantize(Decimal("0.000000000001"))
            if not 0 < average <= Decimal("1e18"):
                raise ValueError("成交后成本均价超出范围")
            row.average_cost = format(average, "f")
        session.flush()
        if not cn and sell:
            _reduce_us_protection(session, row, old_qty, quantity)
            if not row.active:
                plans = session.scalars(select(PositionPlan).where(PositionPlan.symbol == row.symbol,
                                                                  PositionPlan.status == "active"))
                for plan in plans:
                    plan.status, plan.version = "cancelled", uid()
                    plan.payload = {**plan.payload, "cancelled_at": recorded_at, "cancel_reason": "已登记清仓"}

        fill = {"action": action, "session": day.isoformat(), "quantity": format(quantity, "f"),
                "price": format(price, "f"), "fee": account_equity.display_amount(fee),
                "net_amount": str(amount), "realized_pnl": account_equity.display_amount(amount - quantity * basis) if sell else None,
                "cash_already_recorded": booked,
                "proceeds_added_to_available_cash": not booked and not cn and data["proceeds_available"],
                "recorded_at": recorded_at}
        position = view(row)
        payload = {"action": action, "fill": fill, "previous": previous, "position": position,
                   "previous_cash": previous_cash, "cash": {**account.payload, "version": account.version}}
        if cn:
            session.add(CNPositionChange(position_id=row.id, payload=payload))
        else:
            save_snapshot(session, row.security_id, "portfolio_confirmed_sale", payload)
        session.commit()
        return {"position": position, "fill": fill}

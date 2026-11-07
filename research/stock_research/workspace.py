"""本地美股工作台：手工持仓、共享扫描名单和已有研究；不产生真实成交。"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
import re

from sqlalchemy import select

from . import band_strategies, futu_macd, market, watchlist
from .config import settings
from .service import security_for
from .store import (ManualPosition, PositionProtection, Security, Session, Snapshot, WatchlistEntry, WatchlistScan, latest,
                    save_snapshot, stamp, uid)
from .valuation import number


class Conflict(ValueError):
    pass


def symbol(value):
    value = value.strip().upper().removeprefix("$")
    if re.fullmatch(r"\d{6}(?:\.(?:SS|SZ|BJ))?", value) or value.endswith((".SS", ".SZ", ".BJ")):
        raise ValueError("A股暂未开放，本版请添加美股代码")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
        raise ValueError("请输入有效美股代码，例如GOOG、NVDA或BRK-B")
    return value


@contextmanager
def position_lock():
    folder = settings.storage; folder.mkdir(parents=True, exist_ok=True)
    with (folder / "manual-positions.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _position(row):
    return {"id":row.id,"symbol":row.symbol,"name":row.name,"quantity":row.quantity,
            "average_cost":row.average_cost,"currency":row.currency,"notes":row.notes,
            "version":row.version,"created_at":row.created_at,"updated_at":row.updated_at}


def quote(session, security):
    source = latest(session, security.id, "market") if security else None
    state = market.cache_state(source)
    result = {"price":None,"date":None,"currency":None,"state":state["state"],
              "message":state["message"],"snapshot_id":source.id if source else None}
    if source:
        result.update(date=source.payload.get("last_complete_session"),
                      currency=(source.payload.get("profile") or {}).get("currency"),
                      fetched_at=source.payload.get("fetched_at"), expected_session=state.get("expected_session"))
        # 行情缓存可能保留供应商浮点展开后的长小数，按有效报价读取，不套用手工输入精度。
        try:
            price = Decimal(str(source.payload.get("last_price")))
        except InvalidOperation:
            price = None
        if price is not None and price.is_finite() and 0 < price <= Decimal("1e18"):
            result["price"] = str(price)
        else:
            result.update(state="unknown",message="行情缓存的收盘价缺失或无效，请更新该股行情")
    return result


def holdings():
    with Session() as session, localcontext() as ctx:
        ctx.prec = 48
        rows = []; cost = Decimal(0); current_value = Decimal(0); priced = 0
        for row in session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True)).order_by(ManualPosition.created_at, ManualPosition.id)):
            security = session.get(Security, row.security_id); q = quote(session, security)
            qty = number(row.quantity,"持股数量"); basis = qty * number(row.average_cost,"成本均价")
            item = {**_position(row),"name":row.name or security.profile.get("name") or row.symbol,
                    "quote":q,"cost_value":str(basis),"market_value":None,"reference_market_value":None,
                    "unrealized_pnl":None,"unrealized_return":None,"weight":None}
            cost += basis
            if q["price"] is not None and q["currency"] == row.currency:
                value = qty * Decimal(q["price"])
                item["reference_market_value"] = str(value)
                if q["state"] == "fresh":
                    priced += 1; current_value += value
                    item.update(market_value=str(value),unrealized_pnl=str(value-basis),
                                unrealized_return=str((value-basis)/basis))
            rows.append(item)
        complete = bool(rows) and priced == len(rows)
        if complete and current_value > 0:
            for item in rows:
                item["weight"] = str(Decimal(item["market_value"]) / current_value)
        return {"market":"US","currency":"USD","items":rows,"summary":{
            "count":len(rows),"priced_count":priced,"complete":complete,"cost_value":str(cost),
            "market_value":str(current_value) if complete else None,
            "unrealized_pnl":str(current_value-cost) if complete else None,
            "unrealized_return":str((current_value-cost)/cost) if complete and cost else None},
            "message":"用户录入的持仓快照；浮盈亏按录入均价及有效收盘计算，不含费用、分红和现金，不代表账户总资产"}


def save_position(data, position_id=None):
    code = symbol(data["symbol"])
    qty = number(data["quantity"],"持股数量"); avg = number(data["average_cost"],"成本均价")
    if qty <= 0 or avg <= 0:
        raise ValueError("持股数量和成本均价必须大于0；移除持仓请使用移除操作")
    security = security_for(code, create=True)
    if security.profile.get("currency") and security.profile["currency"] != "USD":
        raise ValueError("当前持仓仅支持美元报价美股，请核对代码和币种")
    with position_lock(), Session() as session:
        row = session.get(ManualPosition, position_id) if position_id else session.scalar(select(ManualPosition).where(ManualPosition.symbol==code))
        if position_id:
            if row is None or not row.active:
                raise LookupError("持仓不存在或已移除")
            if row.symbol != code:
                raise ValueError("编辑时不能替换股票代码，请先移除再添加另一只股票")
            if data.get("expected_version") != row.version:
                raise Conflict("持仓已在其他窗口更新，请重新读取后编辑")
        elif row and row.active:
            raise Conflict("这只股票已在持仓中，请编辑已有持仓")
        previous = _position(row) if row else None
        if row is None:
            row = ManualPosition(security_id=security.id,symbol=code)
            session.add(row)
        row.name=data.get("name","").strip(); row.quantity=format(qty,"f"); row.average_cost=format(avg,"f")
        row.notes=data.get("notes","").strip(); row.currency="USD"; row.active=True; row.version=uid(); row.updated_at=stamp()
        session.flush()
        protection = session.get(PositionProtection,row.id)
        if protection or data.get("tactical") is True:
            if protection is None:
                protection = PositionProtection(position_id=row.id,payload={})
                session.add(protection)
            prior = protection.payload
            preserve = bool(position_id and previous and Decimal(previous["quantity"])==qty)
            tactical = data.get("tactical")
            assigned = (qty if tactical is True else Decimal(0) if tactical is False else
                        min(qty,Decimal(prior.get("tactical_qty","0"))) if position_id else Decimal(0))
            protection.payload = {"lots":prior.get("lots",[]) if preserve else [],"tactical_qty":str(assigned),
                                  "reconciled":bool(prior.get("lots")) and not preserve}
        result = _position(row)
        save_snapshot(session,security.id,"manual_position_change",{"action":"EDIT" if position_id else "ADD",
            "previous":previous,"position":result,"source":"用户维护持仓快照，不是成交"})
        session.commit()
        return result


def remove_position(position_id, version):
    with position_lock(), Session() as session:
        row = session.get(ManualPosition,position_id)
        if row is None or not row.active:
            raise LookupError("持仓不存在或已移除")
        if row.version != version:
            raise Conflict("持仓已变化，请重新读取后移除")
        previous = _position(row); row.active=False; row.version=uid(); row.updated_at=stamp()
        protection = session.get(PositionProtection,row.id)
        if protection:
            session.delete(protection)
        save_snapshot(session,row.security_id,"manual_position_change",{"action":"REMOVE","previous":previous,
            "position":_position(row),"source":"仅移除持仓快照，不记录卖出成交"})
        session.commit()
    return {"message":"已移除持仓，历史快照保留"}


def pool():
    try:
        run = watchlist.scan_record()
    except ValueError:
        run = None
    # 只读当前配置；参数改变后的旧扫描不能冒充当前有效信号。
    configuration_error = None
    try:
        strategy_cfg = band_strategies.policy().model_dump()
        indicator_cfg = futu_macd.policy()
    except (ValueError,OSError):
        strategy_cfg = indicator_cfg = None
        configuration_error = "策略配置暂不可用，旧扫描只作历史参考"
    with Session() as session:
        members = list(session.scalars(select(WatchlistEntry).where(WatchlistEntry.active.is_(True)).order_by(WatchlistEntry.symbol)))
        # 单股CLI扫描不能抹掉其他成员的展示；每只股票取最近一次参与的扫描。
        scan_rows = {}; remaining = {m.symbol for m in members}
        if remaining:
            for record in session.scalars(select(WatchlistScan).order_by(WatchlistScan.created_at.desc(),WatchlistScan.id.desc())):
                for item in record.payload.get("results",[]):
                    if item["symbol"] in remaining:
                        scan_rows[item["symbol"]] = item
                        remaining.remove(item["symbol"])
                if not remaining:
                    break
        result = []
        for member in members:
            security = session.scalar(select(Security).where(Security.symbol==member.symbol))
            q = quote(session,security); row = scan_rows.get(member.symbol)
            evaluation = (row or {}).get("evaluation") or {}
            trend = session.get(Snapshot,evaluation["source_trend_id"]) if evaluation.get("source_trend_id") else None
            configuration_current = (strategy_cfg is not None and evaluation.get("strategy_algorithm") == band_strategies.ALGORITHM
                and evaluation.get("strategy_configuration") == strategy_cfg
                and evaluation.get("indicator_parameters") == indicator_cfg
                and trend is not None and trend.payload.get("algorithm") == futu_macd.ALGORITHM)
            current = bool(row and row.get("status")=="OK" and q["state"]=="fresh"
                and row.get("data_as_of")==q["date"] and row.get("freshness_source_market_id")==q["snapshot_id"]
                and configuration_current)
            scan_view = {k:row[k] for k in ("status","data_as_of","strategies","warnings","error","current_exit_alerts") if k in row} if row else None
            result.append({"symbol":member.symbol,"name":security.profile.get("name") if security else None,
                "quote":q,"created_at":member.created_at,"scan":scan_view,"scan_is_current":current,
                "current_signals":row.get("current_signals",[]) if current else []})
    run_view = {k:run[k] for k in ("scan_id","created_at","finished_at","status","scope") if k in run} if run else None
    return {"market":"US","items":result,"last_scan":run_view,"configuration_error":configuration_error,
            "summary":{"count":len(result),"signal_stocks":sum(bool(r["current_signals"]) for r in result)}}


def change_pool(code, active):
    code = symbol(code)
    return {"items":watchlist.change_symbols([code],active)}

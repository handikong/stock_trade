"""本地美股工作台：手工持仓、共享扫描名单和已有研究；不产生真实成交。"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
import re

from sqlalchemy import select

from . import band_strategies, futu_macd, market, market_caps, watchlist
from .config import settings
from .service import security_for
from .store import (ManualPosition, PositionProtection, Security, Session, Snapshot, WatchlistEntry, WatchlistScan, latest,
                    save_snapshot, stamp, uid)
from .valuation import number


class Conflict(ValueError):
    pass


def symbol(value):
    value = value.strip().upper().removeprefix("$")
    if re.fullmatch(r"(?:SH|SZ|BJ)?[0-9]{6}",value) or value.endswith((".SH",".SS",".SZ",".BJ")):
        raise ValueError("此处使用美股代码，沪深持仓请切换到 A 股")
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


def _quote_price(value):
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and 0 < price <= Decimal("1e18") else None


def quote(session, security, region="US", *, daily_change=False):
    source = latest(session, security.id, market.snapshot_kind(region)) if security else None
    state = market.cache_state(source,region)
    if (source and region=="CN" and state["state"]=="stale" and state.get("last_complete_session")
            and state.get("last_complete_session")==state.get("expected_session") and source.payload.get("calendar")=="XSHG"):
        # 有历史停牌缺口时可展示目标日收盘；策略仍通过完整行情缓存单独校验。
        state = {**state,"state":"fresh","message":"当前收盘价可用，历史日线待补"}
    result = {"price":None,"date":None,"currency":None,"state":state["state"],
              "message":state["message"],"snapshot_id":source.id if source else None}
    if source:
        result.update(date=source.payload.get("last_complete_session"),
                      currency=(source.payload.get("profile") or {}).get("currency"),
                      fetched_at=source.payload.get("fetched_at"), expected_session=state.get("expected_session"))
        # 行情缓存可能保留供应商浮点展开后的长小数，按有效报价读取，不套用手工输入精度。
        price = _quote_price(source.payload.get("last_price"))
        if price is not None:
            result["price"] = str(price)
            if daily_change and result["state"] == "fresh":
                cal, _ = market.calendar_context(region)
                day = result["date"]
                if day and cal.is_session(day) and day != cal.first_session.date().isoformat():
                    previous_day = cal.previous_session(day).date().isoformat()
                    bars = {b["date"]:b for b in source.payload.get("bars", [])
                            if b.get("complete") is True and b.get("date") in (previous_day, day)}
                    previous = _quote_price(bars.get(previous_day, {}).get("close"))
                    latest_bar = bars.get(day)
                    if previous is not None and latest_bar and latest_bar.get("split") in (None, "0", "0.0", 0):
                        result["previous_close"] = str(previous)
        else:
            result.update(state="unknown",message="行情缓存的收盘价缺失或无效，请更新该股行情")
    return result


def day_move(price, previous_close, quantity):
    change = price - previous_close
    return change * quantity, change / previous_close


def summarize_holdings(items, region, currency):
    with localcontext() as ctx:
        ctx.prec = 48
        rows = []; cost = Decimal(0); current_value = Decimal(0); priced = 0
        for record in items:
            q = record["quote"]
            qty = number(record["quantity"],"持股数量"); basis = qty * number(record["average_cost"],"成本均价")
            item = {**record,"cost_value":str(basis),"market_value":None,"reference_market_value":None,
                    "unrealized_pnl":None,"unrealized_return":None,"weight":None,
                    "day_pnl":None,"day_return":None}
            cost += basis
            if q["price"] is not None and q["currency"] == currency:
                value = qty * Decimal(q["price"])
                item["reference_market_value"] = str(value)
                if q["state"] == "fresh":
                    priced += 1; current_value += value
                    item.update(market_value=str(value),unrealized_pnl=str(value-basis),
                                unrealized_return=str((value-basis)/basis))
                    if q.get("previous_close") is not None:
                        day_pnl, day_return = day_move(Decimal(q["price"]), Decimal(q["previous_close"]), qty)
                        item.update(day_pnl=str(day_pnl),day_return=str(day_return))
            rows.append(item)
        complete = bool(rows) and priced == len(rows)
        if complete and current_value > 0:
            for item in rows:
                item["weight"] = str(Decimal(item["market_value"]) / current_value)
        return {"market":region,"currency":currency,"items":rows,"summary":{
            "count":len(rows),"priced_count":priced,"complete":complete,"cost_value":str(cost),
            "market_value":str(current_value) if complete else None,
            "unrealized_pnl":str(current_value-cost) if complete else None,
            "unrealized_return":str((current_value-cost)/cost) if complete and cost else None},
            "message":"用户录入的持仓快照；浮盈亏按录入均价及有效收盘计算，不含费用、分红和现金，不代表账户总资产"}


def holdings():
    with Session() as session:
        return read_holdings(session)


def read_holdings(session):
    items = []
    for row in session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True)).order_by(ManualPosition.created_at, ManualPosition.id)):
        security = session.get(Security,row.security_id)
        items.append({**_position(row),"name":row.name or security.profile.get("name") or row.symbol,
                      "quote":quote(session,security,daily_change=True)})
    return summarize_holdings(items,"US","USD")


def cn_pool():
    from . import cn_income
    try:
        scan = watchlist.scan_record(region="CN")
    except ValueError:
        scan = None
    try:
        strategy = band_strategies.policy().model_dump()
        indicator = futu_macd.policy()
    except (ValueError,OSError):
        strategy = indicator = None
    results = ({row["symbol"]:row for row in scan["results"]}
               if scan and scan["status"] in ("complete","partial") else {})
    with Session() as session:
        pool = cn_income.read(session)
        caps = market_caps.cached(session,"CN")
        values = caps.get("values",{})
        symbols = [row["symbol"] for row in pool["members"]]
        securities = {row.symbol:row for row in session.scalars(select(Security).where(Security.symbol.in_(symbols)))}
        members = []
        for row in pool["members"]:
            code = row["symbol"]
            q = quote(session,securities.get(code),"CN")
            latest_scan = results.get(code) or {}
            evaluation = latest_scan.get("evaluation") or {}
            current = (q["state"]=="fresh" and latest_scan.get("status")=="OK"
                       and q["date"]==latest_scan.get("data_as_of")
                       and q["snapshot_id"]==latest_scan.get("freshness_source_market_id")
                       and strategy is not None and evaluation.get("strategy_algorithm")==band_strategies.ALGORITHM
                       and evaluation.get("strategy_configuration")==strategy
                       and evaluation.get("indicator_parameters")==indicator)
            names = [item["name"] for item in latest_scan.get("current_signals",[])
                     if item.get("event_kind","BUY")=="BUY" and item.get("name")] if current else []
            members.append({**row,"quote":q,"buy_signals":names,
                            "market_cap_cny":values.get(code) if row["kind"]=="dividend" else None})
    return {**pool,"members":members,"last_scan":(scan or {}).get("finished_at"),
            "market_caps":{key:caps[key] for key in ("source","fetched_at") if key in caps}}


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
        caps = market_caps.cached(session)
        cap_values = caps.get("values",{})
        # 单股CLI扫描不能抹掉其他成员的展示；每只股票取最近一次参与的扫描。
        scan_rows = {}; remaining = {m.symbol for m in members}
        if remaining:
            for record in session.scalars(select(WatchlistScan).where(watchlist.scan_market("US"))
                                          .order_by(WatchlistScan.created_at.desc(),WatchlistScan.id.desc())):
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
                "quote":q,"market_cap_usd":cap_values.get(member.symbol),"created_at":member.created_at,"scan":scan_view,"scan_is_current":current,
                "current_signals":row.get("current_signals",[]) if current else []})
    run_view = {k:run[k] for k in ("scan_id","created_at","finished_at","status","scope") if k in run} if run else None
    return {"market":"US","items":result,"last_scan":run_view,"configuration_error":configuration_error,
            "market_caps":{key:caps[key] for key in ("source","fetched_at") if key in caps},
            "summary":{"count":len(result),"signal_stocks":sum(bool(r["current_signals"]) for r in result)}}


def change_pool(code, active):
    code = symbol(code)
    return {"items":watchlist.change_symbols([code],active)}

"""独立的美元、人民币收盘净值及外部资金流调整。"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import cn_holdings, market, portfolio, workspace
from .providers import failure_detail
from .service import security_for
from .store import AccountEquity, Job, ManualPosition, PortfolioAccount, Security, Session, digest, latest, now, stamp, uid
from .valuation import number

SCHEDULED_KIND = "scheduled_account_equity"
MANUAL_KINDS = {"US":"account_equity_capture","CN":"cn_account_equity_capture"}
ACCOUNT_IDS = {"US":portfolio.ACCOUNT,"CN":"cn-cash"}
CURRENCIES = {"US":"USD","CN":"CNY"}
MARKET_LABELS = {"US":"美股","CN":"A 股"}
ZONES = {"US":ZoneInfo("America/New_York"),"CN":ZoneInfo("Asia/Shanghai")}
RANGE_DAYS = {"week":7,"month":30,"year":365}
CENT = Decimal("0.01")
CASH_REVIEW = "持仓数量有变化，请按券商余额重新确认总现金"


def _cash(session, region):
    return session.get(PortfolioAccount,ACCOUNT_IDS[region])


def _cash_view(row):
    if row is None or row.payload.get("total_cash") is None:
        return None
    return {key:row.payload.get(key) for key in ("total_cash","net_flows","cash_as_of")} | {"version":row.version}


def _cash_matches(cash, baseline, quantities):
    if baseline is None or baseline.payload.get("cash_version")!=cash["version"]:
        return True
    previous = {mark["symbol"]:Decimal(mark["quantity"]) for mark in baseline.payload["positions"]}
    return previous == quantities


def intraday_cash(session, positions):
    row = _cash(session,"US")
    cash = _cash_view(row)
    if cash is None:
        return None,"请先填写账户总现金"
    baseline = session.scalar(_latest_query("US").limit(1))
    quantities = {code:Decimal(row.quantity) for code,row in positions.items()}
    return {**row.payload,**cash}, "" if _cash_matches(cash,baseline,quantities) else CASH_REVIEW


def display_amount(value):
    with localcontext() as ctx:
        ctx.prec = 54
        return format(value.quantize(CENT,rounding=ROUND_HALF_UP),"f")


def equity_amount(market_value, cash):
    with localcontext() as ctx:
        ctx.prec = 54
        return display_amount(market_value+Decimal(cash["total_cash"]))


def principal_view(account, equity):
    if not account or account.get("principal") is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 54
        metrics = portfolio.principal_metrics(account,Decimal(equity) if equity is not None else None)
        return {**{key:str(value) if value is not None else None for key,value in metrics.items()},
                "loss_percent":account["loss_percent"]}


def _cny(value, label, *, signed=False):
    value = number(value,label)
    if (value<0 and not signed) or value!=value.quantize(CENT):
        raise ValueError(f"{label}须为有效人民币金额，最多两位小数")
    return format(value,".2f")


def save_cn_cash(data):
    total = _cny(data["total_cash"],"总现金")
    flows = _cny(data["net_flows"],"累计净入金",signed=True)
    with workspace.position_lock(), Session() as session:
        row = _cash(session,"CN")
        if data.get("expected_version")!=(row.version if row else None):
            raise workspace.Conflict("A 股现金已变化，请重新读取后保存")
        if row is None:
            row = PortfolioAccount(id=ACCOUNT_IDS["CN"])
            session.add(row)
        row.updated_at = stamp()
        row.version = uid()
        row.payload = {"currency":"CNY","total_cash":total,"net_flows":flows,"cash_as_of":row.updated_at}
        session.commit()
        return _cash_view(row)


def capture_session(region):
    cal,target = market.calendar_context(region)
    # 当前市场已经开始下一场交易时，当前持仓不能再标记到前一日。
    if target != cal.last_session.date().isoformat() and now()>=cal.session_open(cal.next_session(target)).to_pydatetime():
        raise ValueError("新交易日已开盘，请等待本次收盘日线后再保存净值")
    return cal,target


def _positions(session, region):
    if region=="CN":
        return cn_holdings.positions(session)
    return list(session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True))
                                .order_by(ManualPosition.symbol)))


def _last_close(session, security, region, target):
    source = latest(session,security.id,market.snapshot_kind(region)) if security else None
    if source is None:
        return None
    value = source.payload
    if (value.get("last_complete_session")!=target
            or (value.get("profile") or {}).get("currency")!=CURRENCIES[region]):
        return None
    bar = next((b for b in reversed(value.get("bars",[])) if b.get("date")==target and b.get("complete") is True),None)
    try:
        price = Decimal(str(bar["close"])) if bar else None
    except (InvalidOperation,KeyError):
        return None
    return (source.id,price) if price is not None and price.is_finite() and 0<price<=Decimal("1e18") else None


def _estimate(session, region, target, cash):
    rows = _positions(session,region)
    symbols = [row.symbol for row in rows]
    securities = {row.symbol:row for row in session.scalars(select(Security).where(Security.symbol.in_(symbols)))}
    missing, marks = [],[]
    with localcontext() as ctx:
        ctx.prec = 54
        value = Decimal(0)
        for row in rows:
            close = _last_close(session,securities.get(row.symbol),region,target)
            if close is None:
                missing.append(row.symbol)
                continue
            source_id, price = close
            value += Decimal(row.quantity)*price
            marks.append({"symbol":row.symbol,"quantity":row.quantity,"close":str(price),
                          "position_version":row.version,"market_snapshot_id":source_id})
        if missing:
            return None,missing
        return _marked_equity(value,cash,marks),[]


def _marked_equity(value, cash, marks):
    return {"equity":equity_amount(value,cash),"market_value":display_amount(value),
            "total_cash":cash["total_cash"],"net_flows":cash["net_flows"],"cash_as_of":cash["cash_as_of"],
            "cash_version":cash["version"],"positions":marks}


def _estimate_from_holdings(holdings, target, cash):
    missing, marks = [],[]
    with localcontext() as ctx:
        ctx.prec = 54
        value = Decimal(0)
        for row in sorted(holdings["items"],key=lambda item:item["symbol"]):
            quote = row["quote"]
            if row["market_value"] is None or quote["date"]!=target:
                missing.append(row["symbol"])
                continue
            value += Decimal(row["market_value"])
            marks.append({"symbol":row["symbol"],"quantity":row["quantity"],"close":quote["price"],
                          "position_version":row["version"],"market_snapshot_id":quote["snapshot_id"]})
        return (None,missing) if missing else (_marked_equity(value,cash,marks),[])


def _daily_change(payload, prior):
    if payload["net_flows"] is None or prior["net_flows"] is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 54
        flow = Decimal(payload["net_flows"])-Decimal(prior["net_flows"])
        return format(Decimal(payload["equity"])-Decimal(prior["equity"])-flow,".2f")


def _view(row, previous):
    payload = row.payload
    daily = _daily_change(payload,previous.payload) if previous and previous.session_date==payload["previous_session"] else None
    return {"session":row.session_date,"equity":payload["equity"],"daily_pnl":daily,
            "previous_session":payload["previous_session"],"updated_at":row.updated_at}


def _latest_query(region):
    return select(AccountEquity).where(AccountEquity.market==region).order_by(AccountEquity.session_date.desc())


def overview(region):
    with Session() as session:
        return _overview(session,region)


def holdings_overview(region):
    with Session() as session:
        holdings = (workspace if region=="US" else cn_holdings).read_holdings(session)
        return {"holdings":holdings,"equity":_overview(session,region,holdings=holdings)}


def _overview(session, region, *, holdings=None):
    try:
        cal,target = market.calendar_context(region)
    except ValueError:
        cal,target = None,None
    cash_row = _cash(session,region)
    cash = _cash_view(cash_row)
    row = session.scalar(_latest_query(region).limit(1))
    previous = session.get(AccountEquity,(region,row.payload["previous_session"])) if row and row.payload["previous_session"] else None
    latest_view = _view(row,previous) if row else None
    estimate,missing = None,[]
    reason, daily = "", None
    if cash is not None and target:
        estimate,missing = (_estimate(session,region,target,cash) if holdings is None else
                            _estimate_from_holdings(holdings,target,cash))
    if estimate:
        quantities = {mark["symbol"]:Decimal(mark["quantity"]) for mark in estimate["positions"]}
        if not _cash_matches(cash,row,quantities):
            estimate,reason = None,CASH_REVIEW
        elif target!=cal.first_session.date().isoformat():
            prior_day = cal.previous_session(target).date().isoformat()
            prior = session.get(AccountEquity,(region,prior_day))
            if prior:
                daily = _daily_change(estimate,prior.payload)
    estimate_is_saved = bool(estimate and row and row.session_date==target
                             and all(row.payload.get(key)==value for key,value in estimate.items()))
    amount = estimate["equity"] if estimate else None
    return {"market":region,"currency":CURRENCIES[region],"target_session":target,"cash":cash,"latest":latest_view,
            "estimate":amount,"estimate_is_saved":estimate_is_saved,
            "principal":principal_view(cash_row.payload,amount) if region=="US" and cash_row else None,
            "estimate_daily_pnl":daily,"estimate_reason":reason,"missing":missing}


def history(region, period):
    day = now().astimezone(ZONES[region]).date()
    start = (day-timedelta(days=RANGE_DAYS[period]-1)).isoformat()
    with Session() as session:
        query = select(AccountEquity).where(AccountEquity.market==region,AccountEquity.session_date>=start)
        rows = list(session.scalars(query.order_by(AccountEquity.session_date)))
        previous = session.scalar(_latest_query(region).where(AccountEquity.session_date<start).limit(1))
        output = []
        for row in rows:
            output.append(_view(row,previous))
            previous = row
    return {"market":region,"currency":CURRENCIES[region],"period":period,"items":output}


def capture(job, progress, allowed=lambda:True):
    region,target = (job.payload[key] for key in ("market","target_session"))
    cal,current = capture_session(region)
    if current!=target or not allowed():
        raise ValueError("本次净值记录窗口已结束")
    with Session() as session:
        if _cash_view(_cash(session,region)) is None:
            raise ValueError("请先填写该市场的账户现金")
        symbols = [row.symbol for row in _positions(session,region)]
    warnings = []
    for index,code in enumerate(symbols,1):
        if not allowed():
            raise ValueError("本次净值记录窗口已结束")
        progress(int(90*index/len(symbols)),f"{MARKET_LABELS[region]}核对收盘行情 {index}/{len(symbols)} · {code}")
        try:
            security = security_for(code,create=region=="CN")
            with Session() as session:
                close = _last_close(session,security,region,target)
            if close is None:
                market.refresh_market(security.id,region=region)
        except Exception as exc:
            warnings.append({"symbol":code,**failure_detail(exc)})
    with workspace.position_lock(), Session() as session:
        cal,current = capture_session(region)
        if current!=target or not allowed():
            raise ValueError("本次净值记录窗口已结束")
        cash = _cash_view(_cash(session,region))
        if cash is None:
            raise ValueError("请先填写该市场的账户现金")
        payload,missing = _estimate(session,region,target,cash)
        if missing:
            problems = "；".join(f"{row['symbol']} {row['message']}" for row in warnings[:2])
            tail = f"（{problems}）" if problems else ""
            raise ValueError(f"缺少 {len(missing)} 只持仓的有效收盘，净值未保存 · {'、'.join(missing[:8])}{tail}")
        payload["previous_session"] = (None if target==cal.first_session.date().isoformat()
                                       else cal.previous_session(target).date().isoformat())
        row = session.get(AccountEquity,(region,target))
        baseline = row or session.scalar(_latest_query(region).where(AccountEquity.session_date<target).limit(1))
        quantities = {mark["symbol"]:Decimal(mark["quantity"]) for mark in payload["positions"]}
        if not _cash_matches(cash,baseline,quantities):
            raise ValueError(CASH_REVIEW+"后再保存净值")
        if row is None:
            row = AccountEquity(market=region,session_date=target)
            session.add(row)
        if row.payload!=payload:
            row.payload = payload
            row.updated_at = stamp()
        session.commit()
    return {"message":f"{MARKET_LABELS[region]}收盘净值已保存 · {target}","warnings":warnings}


def anchor(region):
    return security_for("SPY" if region=="US" else "510300.SH",create=True)


def manual_payload(region):
    _,target = capture_session(region)
    with Session() as session:
        if _cash_view(_cash(session,region)) is None:
            raise ValueError("请先填写该市场的账户现金")
    return {"market":region,"target_session":target}


def enqueue(region, target, local_day):
    with Session() as session:
        if _cash_view(_cash(session,region)) is None or session.get(AccountEquity,(region,target)):
            return None
        first = digest([SCHEDULED_KIND,region,target,1])[:32]
        previous = session.get(Job,first)
        if previous is None:
            job_id = first
        elif previous.status=="failed" and now()-datetime.fromisoformat(previous.updated_at)>=timedelta(minutes=15):
            job_id = digest([SCHEDULED_KIND,region,target,2])[:32]
            if session.get(Job,job_id):
                return None
        else:
            return None
    security_id = anchor(region).id
    with Session() as session:
        session.add(Job(id=job_id,security_id=security_id,kind=SCHEDULED_KIND,
                        active_key=f"account-equity:{region}",message=f"{MARKET_LABELS[region]}收盘净值排队中 · {target}",
                        payload={"market":region,"target_session":target,"local_date":local_day}))
        try:
            session.commit()
        except IntegrityError:
            return None
    return job_id

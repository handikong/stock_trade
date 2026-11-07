"""工作日实际持仓复盘及飞书摘要；保存证据，操作由用户决定。"""
from decimal import Decimal, localcontext
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import band_strategies, feishu, futu_macd, macd_arrow, market, market_conditions, portfolio, technical
from .config import settings
from .notification_cards import _price, _text
from .providers import failure_detail
from .service import security_for
from .store import Job, ManualPosition, PortfolioAccount, PositionPlan, Security, Session, digest, latest, stamp
from .strategy_exits import _common_state
from .workspace import position_lock

logger = logging.getLogger(__name__)
KIND = "scheduled_holdings_brief"
BJ = ZoneInfo("Asia/Shanghai")
TITLE = "持仓复盘"
ACTIONS = {"stop":"止损复核","profit":"止盈复核","reduce":"减仓复核","add":"可考虑加仓",
           "hold":"继续持有","unknown":"资料待补"}
BATCH_SIZE = 8


def _positions(session):
    return list(session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True)).order_by(ManualPosition.symbol)))


def _input_version(session, rows):
    from .weekly_valuation import KIND as valuation_kind
    account = session.get(PortfolioAccount,portfolio.ACCOUNT)
    plans = list(session.execute(select(PositionPlan.id,PositionPlan.version).where(PositionPlan.status=="active")
                                 .order_by(PositionPlan.id)))
    sources = {}
    for row in rows:
        market_data = latest(session,row.security_id,"market")
        valuation = latest(session,row.security_id,valuation_kind)
        sources[row.symbol] = (market_data.id if market_data else None,valuation.id if valuation else None)
    for security in session.scalars(select(Security).where(Security.symbol.in_(("SPY","QQQ")))):
        market_data = latest(session,security.id,"market")
        sources[security.symbol+"-benchmark"] = market_data.id if market_data else None
    return digest({"positions":[(p.id,p.version) for p in rows],"account":account.version if account else None,
                   "plans":[tuple(row) for row in plans],"sources":sources})


def _refresh(target,allowed):
    with Session() as session:
        held = [row.symbol for row in _positions(session)]
        if not held:
            return []
        from .portfolio_risk import asset, policy
        cfg = policy()
        symbols = list(dict.fromkeys([*held,"SPY","QQQ"]))
        missing = [code for code in symbols if asset(session,code,target,cfg)["price"] is None]
    warnings = []
    for code in missing:
        if not allowed():
            raise ValueError("持仓复盘窗口已结束")
        try:
            market.refresh_market(security_for(code,create=True).id)
        except Exception as exc:
            warnings.append({"symbol":code,"message":failure_detail(exc)["message"]})
    return warnings


def _zone(view,kind,*,confirmed_only=False):
    choices = [z for z in view.get("zones",[]) if z.get("kind")==kind and z.get("quote_low") and z.get("quote_high")]
    zone = next((z for z in choices if z.get("role_status")=="confirmed"),None)
    if zone is None and choices and not confirmed_only:
        zone = choices[0]
    if zone is None:
        return None
    return {"low":zone["quote_low"],"high":zone["quote_high"],"label":zone["strength_label"],
            "confirmed":zone.get("role_status")=="confirmed"}


def _technical(item,ctx,strategy,params):
    bars = item["bars"]
    if len(bars)<60 or item["atr"] is None:
        return None
    closes = [float(b["adjusted_close"]) for b in bars]
    trend, averages = market_conditions._trend(closes)
    exit_state = {"level":None,"confirmed":False}
    if strategy and strategy.exits.enabled:
        trend_cfg = params["policy"]
        price_field = trend_cfg["price_field"]
        minimum = max(40,strategy.exits.slow_ema_period,strategy.macd_arrow.long+strategy.macd_arrow.m,trend_cfg["long_slow"])
        recent = [b for b in bars if b["date"]>=ctx["target"][:4]+"-01-01"]
        if len(recent)<minimum:
            recent = bars
        if len(recent)>=minimum:
            color = futu_macd.calculate(recent,params)[-1]
            arrow = macd_arrow.calculate(recent,strategy.macd_arrow,price_field)[-1]
            input_prices = [float(b[price_field]) for b in recent]
            fast,slow = (futu_macd._ema(input_prices,n)[-1] for n in (strategy.exits.fast_ema_period,strategy.exits.slow_ema_period))
            exit_state = _common_state({"band_indicator":{"dif":color["dif_long"],"dea":color["dea_long"]},"strategies":[arrow]},
                                       fast,slow,strategy.exits)
    view = item["technical"]
    if not technical.is_current(view) or view.get("as_of")!=ctx["target"]:
        view = market.technical_view(bars)
    previous = market.technical_view(bars[:-1])
    prior_support = _zone(previous,"support",confirmed_only=True)
    prior_pressure = _zone(previous,"resistance",confirmed_only=True)
    # 旧区间先换回复权尺度，再用最新复权比换成报价；事件由当时已知的区间判定。
    for zone in (prior_support,prior_pressure):
        if zone:
            ratio = previous["quote_conversion_factor"]/float(item["factor"])
            zone.update(low=zone["low"]*ratio,high=zone["high"]*ratio)
    today = bars[-1]
    price = float(item["price"])
    atr = float(item["atr"])
    guard = float(ctx["cfg"].stop_buffer_atr)*atr
    broken = bool(prior_support and price<prior_support["low"]-guard)
    rejected = bool(prior_pressure and float(today["high"])>=prior_pressure["low"] and
                    price<prior_pressure["low"] and price<float(today["open"]))
    turnover = market_conditions._volume(bars,len(bars)-1,ctx["shortened"])
    return {"trend":trend,"five_day_pct":round((closes[-1]/closes[-6]-1)*100,2),
            "ema20":round(averages["EMA20"]/float(item["factor"]),2),"exit_level":exit_state["level"],
            "exit_confirmed":exit_state["confirmed"],"support":_zone(view,"support"),"resistance":_zone(view,"resistance"),
            "prior_support":prior_support,"prior_resistance":prior_pressure,"broken":broken,"rejected":rejected,
            "volume_ratio":round(turnover,2) if turnover is not None else None,
            "volume_high":turnover is not None and turnover>=settings.market_brief_volume_high}


def _add(session,ctx,code,technical_view):
    active = [p for p in ctx["plans"] if p["symbol"]==code and p["status"]=="active"]
    for plan in active:
        if plan["override"]:
            continue
        for stage in plan["stages"]:
            if stage["remaining"]<=0 or (stage["id"]=="entry" and plan["evaluation"]["kind"]!="加仓"):
                continue
            check = portfolio._check(session,ctx,plan,stage)
            price = ctx["assets"][code]["price"]
            if check["ready"] and Decimal(stage["min_price"])<=price<=Decimal(stage["max_price"]):
                return {"quantity":stage["remaining"],"price_low":stage["min_price"],"price_high":stage["max_price"],
                        "stop":stage["stop"],"source":stage["label"],"plan_id":plan["id"]}
    if active or ctx["risk_free"] is None or ctx["risk_free"]<=0 or technical_view["exit_level"] is None:
        return None
    evaluation,_ = portfolio._evaluate(session,code,context=ctx)
    if evaluation["decision"]=="建议执行":
        stage = next(s for s in evaluation["stages"] if s["id"]=="entry" and s["quantity"]>0)
        price = ctx["assets"][code]["price"]
        if Decimal(stage["min_price"])<=price<=Decimal(stage["max_price"]):
            return {"quantity":stage["quantity"],"price_low":stage["min_price"],"price_high":stage["max_price"],
                    "stop":stage["stop"],"source":evaluation["signal"]["name"],"plan_id":None}
    return None


def _recommend(session,ctx,row,position,view):
    alerts = [a for a in row["alerts"] if a["state"]=="保护位已触及"]
    if alerts:
        levels = "、".join(dict.fromkeys(_price(a["stop"]) for a in alerts))
        return "stop",f"已触及记录的保护位 {levels}，请核对券商成交与剩余仓位。",None
    if view is None:
        return "unknown","目标日完整行情或技术历史不足，等待补齐后再评估。",None
    price = ctx["assets"][row["symbol"]]["price"]
    profitable = price>Decimal(position.average_cost)
    if view["exit_confirmed"]:
        action = "profit" if profitable else "stop"
        return action,"红带、短期均线偏空及 MACD 死叉状态共同确认，复核是否退出。",None
    env = ctx["environment"]
    if view["broken"]:
        support = view["prior_support"]
        return "reduce",f"收盘跌破此前确认支撑 {_price(support['low'])}，复核降风险；收回区间后再评估。",None
    pressure = view["prior_resistance"]
    if profitable and view["rejected"] and ((view["exit_level"] or 0)>=2 or view["volume_high"]):
        return "profit",f"持仓仍有浮盈，反抽确认压力 {_price(pressure['low'])}–{_price(pressure['high'])} 后走弱，可复核分批锁利。",None
    if ctx["equity"] is not None:
        actual = {**ctx,"reservations":[]}
        _,similar,single,aggressive,_ = portfolio._concentration(actual,ctx["assets"][row["symbol"]])
        cfg,multiplier = ctx["cfg"],Decimal(env["multiplier"])
        over = [label for value,fraction,label in ((similar,cfg.similar_fraction,"同向集中度"),(single,cfg.single_fraction,"单股集中度"))
                if value>ctx["equity"]*fraction*multiplier]
        if row["counts_as_aggressive"] and aggressive>ctx["equity"]*cfg.aggressive_fraction*multiplier:
            over.append("进攻型占用")
        if over and (env["state"]=="偏弱" or env["hold_new"] or (view["exit_level"] or 0)>=2):
            return "reduce","、".join(over)+"已超当前额度，且大盘或该股转弱，优先复核减仓。",None
        if ctx["buffer"]<=0 and (row["counts_as_aggressive"] or over):
            return "reduce","账户已触及本金保护参考线，优先复核高波动或集中的仓位。",None
    if (view["exit_level"] or 0)>=2 or any(a["state"]=="预警" for a in row["alerts"]):
        return "hold","已触发预警，观察收盘能否收复关键区间，暂缓追加。",None
    if view["exit_level"] is None:
        return "unknown","共同退出指标未就绪，先核对已有保护位和技术区间。",None
    if addition:=_add(session,ctx,row["symbol"],view):
        return "add",f"{addition['source']}通过当前收盘与资金复核；按下列价位检查下一交易日。",addition
    if ctx["risk_free"] is None:
        return "hold","继续观察关键区间；账户或组合行情待补，加仓额度尚未核实。",None
    if ctx["risk_free"]<=0 or env["hold_new"]:
        return "hold","继续观察关键区间；当前组合风险或大盘状态限制加仓。",None
    return "hold","当前未出现确认的退出或加仓条件，按关键区间继续观察。",None


def prepare(day,target,*,warnings=None):
    report = {"schema":"holdings-brief-v1","local_date":day,"as_of_session":target,"prepared_at":stamp(),
              "items":[],"warnings":list(warnings or [])}
    with position_lock(), Session() as session, localcontext() as decimal_ctx:
        decimal_ctx.prec = 48
        positions = _positions(session)
        report["input_version"] = _input_version(session,positions)
        if not positions:
            return report
        ctx = portfolio._context(session)
        if ctx["target"]!=target:
            raise ValueError("目标美股交易日已变，请等待下一次持仓复盘")
        calendar = ctx["calendar"]
        report["next_session"] = calendar.next_session(target).date().isoformat()
        report["reused_close"] = calendar.session_close(target).tz_convert(BJ).date().isoformat()!=day
        report["market"] = ctx["environment"]
        ctx["shortened"] = {x.date().isoformat() for x in calendar.early_closes}
        by_symbol = {p["symbol"]:p for p in ctx["positions"]}
        try:
            strategy = band_strategies.policy()
            params = futu_macd.policy()
            params = {**params,"policy":{**params["policy"],"trend":None}}
        except (ValueError,OSError):
            strategy = params = None
            report["warnings"].append({"message":"共同退出策略配置不可用，继续检查冻结保护位及已有技术证据"})
        for position in positions:
            row,item = by_symbol[position.symbol],ctx["assets"][position.symbol]
            try:
                view = _technical(item,ctx,strategy,params)
            except (ValueError,ArithmeticError,OSError) as exc:
                view = None
                report["warnings"].append({"symbol":position.symbol,"message":failure_detail(exc)["message"]})
            try:
                action,reason,add = _recommend(session,ctx,row,position,view)
            except (ValueError,ArithmeticError,OSError) as exc:
                action,reason,add = "unknown","当前资料无法完成评估，请核对行情和策略配置。",None
                report["warnings"].append({"symbol":position.symbol,"message":failure_detail(exc)["message"]})
            report["items"].append({"symbol":position.symbol,"price":str(item["price"]) if item["price"] is not None else None,
                "source_market_id":item["id"],"technical":view,"protective_alerts":row["alerts"],"action":action,"reason":reason,"add":add})
    order = {key:i for i,key in enumerate(ACTIONS)}
    report["items"].sort(key=lambda row:(order[row["action"]],row["symbol"]))
    return report


def _level(zone):
    if not zone:
        return "待补"
    return f"{_price(zone['low'])}–{_price(zone['high'])}（{zone['label']}）"


def _detail(row):
    t = row["technical"]
    if not t:
        return [f"收盘 {_price(row['price'])} · 技术资料待补",row["reason"]]
    lines = [f"收盘 {_price(row['price'])} · 近5日 {t['five_day_pct']:+.2f}% · {t['trend']}",
             f"支撑 {_level(t['support'])} ｜ 压力 {_level(t['resistance'])}",row["reason"]]
    tracked = [a for a in row["protective_alerts"] if a["state"] in ("跟踪中","预警","保护位已触及")]
    if tracked:
        levels = "、".join(dict.fromkeys(_price(a["stop"]) for a in tracked))
        lines.append("冻结保护位 " + levels)
    if row["add"]:
        add = row["add"]
        lines.append(f"加仓参考 {_price(add['price_low'])}–{_price(add['price_high'])} · 最多 {add['quantity']} 股 · 保护 {_price(add['stop'])}")
    return lines


def _heading(report):
    note = " · 沿用最近收盘" if report.get("reused_close") else ""
    next_session = f" · 下个美股交易日 {report['next_session']}" if report.get("next_session") else ""
    return f"{report['local_date']} 北京时间 · 美股收盘 {report['as_of_session']}{note}{next_session} · 价格 USD"


def render(report):
    lines = [f"{TITLE} · {_heading(report)}"]
    if not report["items"]:
        lines.append("当前没有登记持仓。")
    for row in report["items"]:
        lines.extend(["",f"{row['symbol']} · {ACTIONS[row['action']]}",*_detail(row)])
    for warning in report.get("warnings",[]):
        lines.append(f"提示 {warning.get('symbol','')} · {warning['message']}")
    for part in report.get("delivery",[]):
        lines.append(f"飞书 {part['part']} · {part['status']}"+(f" · {part['message']}" if part.get("message") else ""))
    return "\n".join(lines)


def _cards(report):
    rows = report["items"]
    groups = [rows[i:i+BATCH_SIZE] for i in range(0,len(rows),BATCH_SIZE)]
    for index, group in enumerate(groups,1):
        label = TITLE + (f" · {index}/{len(groups)}" if len(groups)>1 else "")
        elements = [{"tag":"markdown","content":_text(_heading(report),160)}]
        for row in group:
            title = f"**{_text(row['symbol'],15)} · {ACTIONS[row['action']]}**"
            body = "\n".join(_text(line,240) for line in _detail(row))
            elements.extend([{"tag":"hr"},{"tag":"markdown","content":title+"\n"+body}])
        elements.append({"tag":"markdown","content":"按完整日线复盘 · 加仓共用账户额度，操作前逐只复核"})
        yield index,{"schema":"2.0","header":{"template":"red" if any(r["action"]=="stop" for r in group) else "blue",
                            "title":{"tag":"plain_text","content":label}},"body":{"elements":elements}}


def _unchanged(report):
    with position_lock(), Session() as session:
        return _input_version(session,_positions(session))==report["input_version"]


def run(job,result,*,allowed,delivery_enabled,checkpoint):
    report = result.get("report")
    if report:
        for part in report.get("delivery",[]):
            if part["status"]=="发送中":
                part.update(status="结果未知",message="上次发送被中断，请核对群消息")
    if not allowed():
        return {**result,"status":"expired","message":"持仓复盘已过窗口或已停用"}
    if report is None:
        target = job.payload["target_session"]
        warnings = _refresh(target,allowed)
        report = prepare(job.payload["local_date"],target,warnings=warnings)
        result["report"] = report
        checkpoint(job.id,result,70,"持仓复盘已生成")
    delivery = report.setdefault("delivery",[])
    known = {row["part"] for row in delivery}
    for part,card in _cards(report):
        if any(row["status"]!="已发送" for row in delivery):
            break
        if part in known:
            continue
        if not delivery_enabled():
            break
        if part>settings.feishu_max_messages_per_run:
            delivery.append({"part":part,"status":"跳过","message":"超出单次消息上限，其余持仓见本地日报"})
            break
        if not allowed() or not _unchanged(report):
            delivery.append({"part":part,"status":"跳过","message":"持仓、现金、计划、数据或运行窗口已变化，旧建议留作档案"})
            break
        outcome = {"part":part,"status":"发送中","started_at":stamp(),"card":card}
        delivery.append(outcome)
        checkpoint(job.id,result,85,f"持仓卡片 {part} 待飞书确认")
        try:
            if not allowed() or not delivery_enabled():
                outcome.update(status="跳过",message="投递前窗口结束或发送已关闭")
            else:
                feishu.send(card)
                outcome["status"] = "已发送"
        except feishu.DeliveryFailure as exc:
            outcome.update(status="结果未知" if exc.status=="UNKNOWN" else "失败",message=str(exc))
        except ValueError as exc:
            outcome.update(status="失败",message=str(exc))
        outcome["finished_at"] = stamp()
        checkpoint(job.id,result,95,f"持仓卡片 {part} · {outcome['status']}")
        if outcome["status"]!="已发送":
            break
    warnings = [*report["warnings"],*({"message":p["message"]} for p in delivery if p.get("message"))]
    partial = warnings or any(r["action"]=="unknown" for r in report["items"])
    sent = sum(p["status"]=="已发送" for p in delivery)
    result.update(warnings=warnings,status="partial" if partial else "complete",
                  message=f"持仓复盘已保存 · {len(report['items'])}只 · 已发送飞书 {sent} 张")
    return result


def saved(local_date=None):
    with Session() as session:
        rows = session.scalars(select(Job).where(Job.kind==KIND,Job.status.in_(("completed","failed")))
                               .order_by(Job.created_at.desc()))
        for row in rows:
            if local_date and row.payload.get("local_date")!=local_date:
                continue
            if report:=row.result.get("report"):
                return report
    return None


def preview():
    from .store import now
    _,target = market.calendar_context()
    return prepare(now().astimezone(BJ).date().isoformat(),target)

"""可审计的账户快照、买入计划与人工成交确认。"""
from datetime import datetime, timedelta
from decimal import localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import band_strategies, futu_macd, market, portfolio_risk as risk, technical, weekly_valuation, workspace
from .service import security_for
from .store import (ManualPosition, PortfolioAccount, PositionPlan, PositionProtection, Session, digest, now,
                    save_snapshot, stamp, uid)
from .valuation import number

D, ZERO, cash = risk.D, risk.ZERO, risk.cash
ACCOUNT = "us-cash"
STAGES = {"entry":"首仓","left":"左侧加仓","right":"右侧加仓","manual":"人工开仓/加仓"}


def _money(value,label,*,minimum=ZERO):
    result = number(value,label)
    if result<minimum or result!=result.quantize(risk.CENT):
        raise ValueError(f"{label}须为有效美元金额，最多两位小数")
    return result


def _account_view(row):
    return {**row.payload,"version":row.version,"updated_at":row.updated_at} if row else None


def save_account(data):
    principal = _money(data["principal"],"参考本金",minimum=risk.CENT)
    total = _money(data["total_cash"],"总现金")
    available = _money(data["available_cash"],"可用现金")
    flows = _money(data["net_flows"],"基准后累计净入金",minimum=-D("1e18"))
    loss = number(data["loss_percent"],"本金亏损比例")
    if available>total or not ZERO<=loss<100 or principal*(1-loss/100)+flows<=0:
        raise ValueError("可用现金不能超过总现金，亏损比例须在0到100%之间，本金参考线须大于0")
    anchor = security_for("SPY",create=True)
    with workspace.position_lock(), Session() as session:
        row = session.get(PortfolioAccount,ACCOUNT)
        if data.get("expected_version")!=(row.version if row else None):
            raise workspace.Conflict("账户已更新，请重新读取后保存")
        previous = _account_view(row)
        if row is None:
            row = PortfolioAccount(id=ACCOUNT)
            session.add(row)
        row.payload = {"currency":"USD","principal":cash(principal),"total_cash":cash(total),"available_cash":cash(available),
                       "net_flows":cash(flows),"loss_percent":str(loss),"cash_as_of":stamp()}
        row.version=uid(); row.updated_at=stamp()
        current = _account_view(row)
        save_snapshot(session,anchor.id,"portfolio_account_change",{"previous":previous,"current":current})
        session.commit()
        return current


def _plan_status(plan,item):
    if plan.status!="active":
        return plan.status
    data = plan.payload
    if now()>=datetime.fromisoformat(plan.expires_at):
        return "expired"
    initial = next((p for p in data["stages"] if p["id"] in ("entry","manual")),None)
    if initial and any((b.get("split") not in (None,"0","0.0",0) or D(b["low"])<D(initial["stop"]))
                       for b in item["bars"] if b["date"]>data["as_of"]):
        return "invalidated"
    return "active"


def _plan_view(row,status):
    return {"id":row.id,"symbol":row.symbol,"version":row.version,"created_at":row.created_at,
            "expires_at":row.expires_at,"status":status,**row.payload}


def _remaining(plans):
    return [(p,stage) for p in plans if p["status"]=="active" for stage in p["stages"] if stage["remaining"]>0]


def _context(session, code=None):
    cfg = risk.policy()
    cal,target = market.calendar_context()
    stored = list(session.scalars(select(PositionPlan).where(PositionPlan.status=="active",PositionPlan.expires_at>stamp())
                                  .order_by(PositionPlan.created_at,PositionPlan.id)))
    symbols = {"SPY","QQQ",*(p.symbol for p in stored),*session.scalars(select(ManualPosition.symbol).where(ManualPosition.active.is_(True)))}
    if code:
        symbols.add(code)
    assets = {key:risk.asset(session,key,target,cfg) for key in sorted(symbols)}
    plans = [_plan_view(p,_plan_status(p,assets[p.symbol])) for p in stored]
    reservations = _remaining(plans)
    qqq = assets["QQQ"]
    positions = risk.exposure(session,assets,qqq,cfg)
    account = _account_view(session.get(PortfolioAccount,ACCOUNT))
    reserved = sum((D(s["cash_per_share"])*s["remaining"] for _,s in reservations),ZERO)
    reserved_risk = sum((D(s["risk_per_share"])*s["remaining"] for _,s in reservations),ZERO)
    result = {"cfg":cfg,"target":target,"calendar":cal,"assets":assets,"plans":plans,"account":account,
              "reservations":reservations,"reserved_cash":reserved,"reserved_risk":reserved_risk,**positions}
    env = risk.market_context(session,assets["SPY"],qqq,target,cfg)
    result["environment"] = env
    result["equity"] = result["floor"] = result["buffer"] = result["risk_free"] = result["cash_free"] = None
    if account:
        principal = D(account["principal"])
        result["floor"] = principal*(1-D(account["loss_percent"])/100)+D(account["net_flows"])
        if not positions["unknown"]:
            equity = D(account["total_cash"])+positions["market_value"]
            result["equity"] = equity
            result["buffer"] = equity-result["floor"]
            result["risk_free"] = (max(ZERO,result["buffer"]-max(positions["guard_risk"],positions["stress_risk"])-reserved_risk)
                                   if D(account["loss_percent"])>0 else ZERO)
            result["cash_free"] = max(ZERO,D(account["available_cash"])-reserved-equity*cfg.cash_reserve_fraction)
    return result


def _summary(ctx):
    keys = ("market_value","equity","floor","buffer","guard_risk","stress_risk","risk_free","cash_free",
            "reserved_cash","reserved_risk","aggressive_value","tactical_value")
    return {**{k:cash(ctx[k]) if ctx[k] is not None else None for k in keys},
            "stress_samples":ctx["stress_samples"],"unknown":ctx["unknown"],"position_count":len(ctx["positions"])}


def overview():
    with Session() as session, localcontext() as context:
        context.prec=48
        ctx = _context(session)
        active = {p["id"]:p for p in ctx["plans"]}
        recent = []
        for row in session.scalars(select(PositionPlan).order_by(PositionPlan.created_at.desc(),PositionPlan.id.desc()).limit(20)):
            recent.append(active.get(row.id) or _plan_view(row,"expired" if row.status=="active" else row.status))
        return {"account":ctx["account"],"as_of":ctx["target"],"summary":_summary(ctx),"positions":ctx["positions"],
                "market":ctx["environment"],"plans":recent}


def _levels(item, cfg, target):
    price,atr,view = item["price"],item["atr"],item["technical"]
    if price is None or atr is None or view.get("as_of")!=target or not technical.is_current(view):
        return {},None
    zones = [z for z in view.get("zones",[]) if z.get("role_status")=="confirmed" and z.get("quote_low") and z.get("quote_high")]
    support = next((z for z in zones if z["kind"]=="support"),None)
    pressure = next((z for z in zones if z["kind"]=="resistance"),None)
    params = futu_macd.policy()
    params = {**params,"policy":{**params["policy"],"trend":None}}
    bars = [b for b in item["bars"] if b["date"]>=target[:4]+"-01-01"]
    sig = None
    if len(bars)>=15:
        strategy_policy = band_strategies.policy()
        last = band_strategies.calculate(bars,futu_macd.calculate(bars,params),params,strategy_policy)[-1]
        for sid in ("EMA_PULLBACK","MACD_ICON1"):
            s = next((s for s in last["strategies"] if s["strategy_id"]==sid and s["buy_signal"]),None)
            if s and (s.get("exit_plan") or {}).get("initial_protection_price"):
                sig = s
                break
    levels = {}
    gap = atr*cfg.stop_buffer_atr
    extension = atr*cfg.entry_buffer_atr
    if sig:
        scale = item["factor"] if params["policy"]["price_field"]=="adjusted_close" else D(1)
        stop = risk.amount(sig["exit_plan"]["initial_protection_price"])/scale
        entry_cap = sig["levels"].get("entry_max")
        max_price = min(price+extension,risk.amount(entry_cap)/scale) if entry_cap is not None else price+extension
        warning = max(stop+gap,min(price,risk.amount(sig["levels"].get("confirmation_price") or price*scale)/scale))
        levels["entry"] = (max(stop+risk.CENT,price-extension),max_price,stop,warning,price,sig["strategy_id"]=="MACD_ICON1")
    elif support:
        low, high = (risk.amount(support[k]) for k in ("quote_low","quote_high"))
        levels["entry"] = (high,high+extension,low-gap,high,high,False)
    if support and levels.get("entry"):
        low, high = (risk.amount(support[k]) for k in ("quote_low","quote_high"))
        original = levels["entry"][2]
        if original<low<=high<price:
            levels["left"] = (high,high+extension,max(original,low-gap),high,high,True)
    if pressure:
        low, high = (risk.amount(pressure[k]) for k in ("quote_low","quote_high"))
        levels["right"] = (high,high+extension,low-gap,high,high,False)
    return levels,{"name":sig["name"],"id":sig["strategy_id"],"as_of":target} if sig else None


def _concentration(ctx,item):
    cfg = ctx["cfg"]
    members = risk.index_members()
    related, group_value, single_value = [],ZERO,ZERO
    for row in ctx["positions"]:
        if row["market_value"] is None:
            continue
        value = D(row["market_value"])
        if row["symbol"]==item["symbol"]:
            single_value += value
        counted,why = risk.similarity(item,ctx["assets"][row["symbol"]],cfg,members)
        if counted:
            group_value += value
        if why:
            related.append({"symbol":row["symbol"],"value":row["market_value"],"reasons":why,"counted":counted})
    aggressive_pending = tactical_pending = ZERO
    for plan,stage in ctx["reservations"]:
        other = ctx["assets"][plan["symbol"]]
        value = D(stage["max_price"])*stage["remaining"]
        if plan["symbol"]==item["symbol"]:
            single_value += value
        if risk.similarity(item,other,cfg,members)[0]:
            group_value += value
        if plan.get("aggressive") or risk.aggression(other,ctx["assets"]["QQQ"],cfg)["counts_as_aggressive"]:
            aggressive_pending += value
        if stage["tactical"]:
            tactical_pending += value
    return related,group_value,single_value,ctx["aggressive_value"]+aggressive_pending,ctx["tactical_value"]+tactical_pending


def _stage(stage, bounds, item, cfg, qty=0):
    low, high, stop, warning, trigger, tactical = bounds
    low, high, stop = (D(cash(low,up=True)),D(cash(high)),D(cash(stop)))
    if not ZERO<stop<low<=high:
        return None
    warning = max(stop,min(high,warning))
    price_risk = high-stop+high*(cfg.gap_fraction+2*cfg.fee_fraction)
    stress = high*risk.stress_fraction(item,cfg)
    return {"id":stage,"label":STAGES[stage],"min_price":cash(low),"max_price":cash(high),"stop":cash(stop),
            "warning":cash(warning),"trigger":cash(trigger,up=True),"tactical":tactical,"quantity":qty,"remaining":qty,
            "stop_percent":str((high-stop)/high),"cash_per_share":cash(high*(1+cfg.fee_fraction),up=True),
            "risk_per_share":cash(max(price_risk,stress),up=True),"stop_risk_per_share":cash(price_risk,up=True)}


def _size(stages,cfg,spend,risk_free,tactical_free):
    available = sum(cfg.stage_fractions[s["id"]] for s in stages)
    tactical_left = tactical_free
    for stage in stages:
        share = cfg.stage_fractions[stage["id"]]/available
        per_share = D(stage["cash_per_share"])
        limits = [spend*share/per_share,risk_free*share/D(stage["risk_per_share"])]
        if stage["tactical"]:
            limits.append(tactical_left/per_share)
        qty = max(0,int(min(limits)))
        stage.update(quantity=qty,remaining=qty)
        if stage["tactical"]:
            tactical_left -= qty*per_share


def _manual(data,item,cfg):
    price = _money(data["price"],"人工买入价",minimum=risk.CENT)
    stop = _money(data["stop"],"人工保护位",minimum=risk.CENT)
    note = data["reason"].strip()
    if stop>=price or not note:
        raise ValueError("人工方案需要说明理由，保护位须低于买入价")
    stage = _stage("manual",(price,price,stop,(price+stop)/2,price,data["tactical"]),item,cfg,data["quantity"])
    return {"stage":stage,"reason":note}


def _evaluate(session,code,manual=None):
    ctx = _context(session,code)
    cfg,account,env = (ctx[k] for k in ("cfg","account","environment"))
    item = ctx["assets"][code]
    price = item["price"]
    limits, missing, notes = [],[],[]
    if account is None:
        missing.append("请先填写参考本金和现金")
    elif now()-datetime.fromisoformat(account["cash_as_of"])>timedelta(hours=cfg.cash_max_hours):
        missing.append("请重新确认券商可用现金")
    if ctx["unknown"]:
        missing.append("持仓缺少目标日有效收盘："+"、".join(ctx["unknown"]))
    if price is None:
        missing.append("请更新这只股票的完整美元日线")
    if env["state"]=="资料待补":
        missing.append("大盘资料待补，请先生成当日大盘简报或更新 SPY、QQQ")
    if env["hold_new"]:
        missing.append("大盘最新收盘破位且波动升高，等待企稳")
    if any(p["symbol"]==code and p["status"]=="active" for p in ctx["plans"]):
        limits.append("该股已有进行中的计划，请先检查或取消")
    if price is not None and price<cfg.min_price:
        limits.append(f"收盘 ${cash(price)} 低于价格门槛 ${cash(cfg.min_price)}")
    if item.get("liquidity") is None:
        missing.append("20日成交金额资料不足")
    elif item["liquidity"]<cfg.min_dollar_volume:
        limits.append(f"20日均成交金额 ${cash(item['liquidity'])} 低于门槛 ${cash(cfg.min_dollar_volume)}")
    profile = item["profile"]
    valuation = {"state":"待核对","id":None,"prices":None}
    if profile.get("type")=="ETF":
        valuation["state"] = "基金使用价格结构和组合风险评估"
    elif profile.get("type")=="EQUITY":
        from .store import Security
        security = session.scalar(select(Security).where(Security.symbol==code))
        current = weekly_valuation.read(session,security.id)
        valuation["id"] = current.get("id")
        if current.get("scenarios") and not current.get("stale") and current.get("currency")=="USD":
            prices = [D(row["price"]) for row in current["scenarios"]]
            valuation.update(state="财年 EPS × Forward PE 情景参考",prices=[cash(p) for p in prices])
            if price is not None and price>prices[-1]:
                limits.append("收盘价已高于当前乐观估值情景")
            elif price is not None and price>prices[1]:
                notes.append("收盘已高于基准估值，核对估值空间")
        else:
            missing.append("估值资料缺失、过期或美元口径待核对")
    else:
        missing.append("证券类型待行情确认")
    related,group,single,aggressive,tactical = _concentration(ctx,item)
    tag = risk.aggression(item,ctx["assets"]["QQQ"],cfg)
    held = any(p["symbol"]==code for p in ctx["positions"])
    headroom = {}
    multiplier = D(env["multiplier"])
    if ctx["equity"] is not None:
        equity = ctx["equity"]
        headroom = {"可用资金":ctx["cash_free"],"单股空间":max(ZERO,equity*cfg.single_fraction*multiplier-single),
                    "同向组合空间":max(ZERO,equity*cfg.similar_fraction*multiplier-group)}
        if tag["counts_as_aggressive"]:
            headroom["进攻型空间"] = max(ZERO,equity*cfg.aggressive_fraction*multiplier-aggressive)
        for label, value in headroom.items():
            if value<=0:
                limits.append(f"{label}已用完")
        if ctx["risk_free"]<=0:
            limits.append(f"扣除组合压力和已有计划后，剩余新增风险为 $0；本金缓冲 ${cash(ctx['buffer'])}")
    try:
        levels,signal = _levels(item,cfg,ctx["target"])
    except (ValueError,OSError):
        levels,signal = {},None
        missing.append("技术或策略配置需复核")
    if not signal:
        missing.append("未出现当前收盘确认的买点，价位仅作观察")
    if valuation["prices"]:
        cap = D(valuation["prices"][-1])
        levels = {k:(low,min(high,cap),stop,warning,trigger,tactical)
                  for k,(low,high,stop,warning,trigger,tactical) in levels.items()}
    stages = [s for key,bounds in levels.items() if (s:=_stage(key,bounds,item,cfg))]
    first_stage = next((s for s in stages if s["id"]=="entry"),None)
    if first_stage is None:
        missing.append("首仓尚无可用的入场区间及保护位")
    if stages and headroom:
        tactical_free = max(ZERO,D(account["principal"])*cfg.tactical_fraction-tactical)
        _size(stages,cfg,min(headroom.values()),min(ctx["risk_free"],D(account["principal"])*cfg.trade_risk_fraction*multiplier),tactical_free)
        if first_stage is not None and first_stage["quantity"]==0:
            limits.append("综合现金、集中度、保护位距离或机动额度后，首仓不足一股")
    for stage in stages:
        stage["stop_risk"] = cash(D(stage["stop_risk_per_share"])*stage["quantity"],up=True)
    if held:
        for stage in stages:
            if stage["id"]=="entry":
                stage["label"] = "本次加仓"
    decision = "拒绝" if limits else "等待确认" if missing else "建议执行"
    override = _manual(manual,item,cfg) if manual else None
    if override:
        stage = override["stage"]
        cost = stage["quantity"]*D(stage["cash_per_share"])
        added = stage["quantity"]*D(stage["risk_per_share"])
        override.update(cash=cash(cost),added_risk=cash(added),
                        combined_risk=None if ctx["unknown"] else cash(max(ctx["guard_risk"],ctx["stress_risk"])+ctx["reserved_risk"]+added),
                        similar_after=None if ctx["unknown"] else cash(group+cost),
                        available_after=cash(D(account["available_cash"])-ctx["reserved_cash"]-cost) if account else None)
    cal,target = ctx["calendar"],ctx["target"]
    first = cal.next_session(target)
    if len(cal.sessions_in_range(first,cal.last_session))<cfg.plan_sessions:
        from .trend import _calendar
        cal = _calendar(first.date(),first.date()+timedelta(days=21))
    expires = cal.session_close(cal.sessions_window(first,cfg.plan_sessions)[-1]).isoformat()
    result = {"symbol":code,"kind":"加仓" if held else "新开仓","as_of":target,"next_session":first.date().isoformat(),
              "expires_at":expires,"decision":decision,"limits":limits,"waiting":missing,"notes":notes,
              "price":cash(price) if price is not None else None,"valuation":valuation,"signal":signal,"stages":stages,
              "related":related,"risk_profile":tag,"market":env,"account_summary":_summary(ctx),
              "headroom":{k:cash(v) for k,v in headroom.items()},"similar_value":cash(group),"manual":override,
              "planned_cash":cash(sum((D(s["cash_per_share"])*s["quantity"] for s in stages),ZERO),up=True),
              "planned_risk":cash(sum((D(s["risk_per_share"])*s["quantity"] for s in stages),ZERO),up=True),
              "config":cfg.model_dump(mode="json"),"market_versions":{k:v["id"] for k,v in ctx["assets"].items()}}
    result["version"] = digest({"result":result,"account":account["version"] if account else None,
                                "positions":[p["version"] for p in ctx["positions"]],
                                "plans":[(p["version"],p["status"]) for p in ctx["plans"]],
                                "market":{k:v["id"] for k,v in ctx["assets"].items()}})
    return result,ctx


def evaluate(code,manual=None):
    with Session() as session, localcontext() as context:
        context.prec=48
        return _evaluate(session,workspace.symbol(code),manual)[0]


def create_plan(code,version,manual=None):
    code = workspace.symbol(code)
    security = security_for(code,create=True)
    with workspace.position_lock(), Session() as session, localcontext() as context:
        context.prec=48
        result,ctx = _evaluate(session,code,manual)
        if result["version"]!=version:
            raise workspace.Conflict("估值、行情或账户已变动，请重新评估")
        if any(p["symbol"]==code and p["status"]=="active" for p in ctx["plans"]):
            raise workspace.Conflict("该股已有进行中的计划")
        if now()>=datetime.fromisoformat(result["expires_at"]):
            raise workspace.Conflict("本次计划有效期已结束，请重新评估")
        if security.profile.get("currency") not in (None,"USD"):
            raise ValueError("当前计划仅支持美元报价")
        if manual:
            override = result["manual"]
            if override["available_after"] is None or D(override["available_after"])<0:
                raise ValueError("人工方案也需填写现金，且金额不能超过扣除其他计划后的券商可用现金")
            if now()-datetime.fromisoformat(ctx["account"]["cash_as_of"])>timedelta(hours=ctx["cfg"].cash_max_hours):
                raise ValueError("请重新确认券商现金后再保存人工计划")
            stages = [override["stage"]]
        else:
            if result["decision"]!="建议执行":
                raise ValueError("当前建议尚不可执行，可等待复评或明确保存人工方案")
            stages = [s for s in result["stages"] if s["quantity"]>0]
        row = PositionPlan(security_id=security.id,symbol=code,expires_at=result["expires_at"],payload={
            "as_of":result["as_of"],"next_session":result["next_session"],
            "aggressive":result["risk_profile"]["counts_as_aggressive"],"stages":stages,"fills":[],
            "override":bool(manual),"evaluation":result})
        session.add(row); session.flush()
        output = _plan_view(row,"active")
        session.commit()
        return output


def _find_stage(plan,stage_id):
    result = next((row for row in plan["stages"] if row["id"]==stage_id),None)
    if result is None or result["remaining"]<=0:
        raise ValueError("此档没有待确认的股数")
    return result


def _check(session,ctx,plan,stage):
    cfg, account, env = (ctx[key] for key in ("cfg","account","environment"))
    item = ctx["assets"][plan["symbol"]]
    price = item["price"]
    messages = []
    if plan["status"]!="active":
        messages.append("原计划已过期、取消或失效")
    if account is None or now()-datetime.fromisoformat(account["cash_as_of"])>timedelta(hours=cfg.cash_max_hours):
        messages.append("请先核对券商现金")
    elif D(account["available_cash"])-ctx["reserved_cash"]<(ctx["equity"] or ZERO)*cfg.cash_reserve_fraction:
        messages.append("扣除待执行计划后，可用现金低于当前预留要求")
    if account and D(account["loss_percent"])==0:
        messages.append("账户本金风险容忍度设为零")
    if ctx["unknown"]:
        messages.append("组合行情待补："+"、".join(ctx["unknown"]))
    if ctx["buffer"] is not None and max(ctx["guard_risk"],ctx["stress_risk"])+ctx["reserved_risk"]>ctx["buffer"]:
        messages.append("现有组合及待执行计划的压力损失已超过本金缓冲")
    if env["state"]=="资料待补" or env["hold_new"]:
        messages.append("大盘资料待补或市场已触发暂停新增风险提示")
    if ctx["equity"] is not None:
        multiplier = D(env["multiplier"])
        equity = ctx["equity"]
        planned_risk = sum((D(s["risk_per_share"])*s["remaining"] for s in plan["stages"]),ZERO)
        if planned_risk>D(account["principal"])*cfg.trade_risk_fraction*multiplier:
            messages.append("剩余计划风险已超过当前大盘状态允许的单笔额度")
        _, group, single, aggressive, tactical = _concentration(ctx,item)
        caps = [(single,equity*cfg.single_fraction*multiplier,"单股"),
                (group,equity*cfg.similar_fraction*multiplier,"同向持仓")]
        if plan["aggressive"] or risk.aggression(item,ctx["assets"]["QQQ"],cfg)["counts_as_aggressive"]:
            caps.append((aggressive,equity*cfg.aggressive_fraction*multiplier,"进攻型"))
        if stage["tactical"]:
            caps.append((tactical,D(account["principal"])*cfg.tactical_fraction,"机动池"))
        messages.extend(f"{label}已超过当前额度" for actual,cap,label in caps if actual>cap)
    if price is None:
        messages.append("缺少该股当前完整日线")
    elif price<=D(stage["stop"]):
        messages.append("收盘已触及该档保护位")
    elif stage["id"]!="manual" and price>D(stage["max_price"]):
        messages.append("收盘高于这档最高接受价，等待回踩")
    if item["profile"].get("type")=="EQUITY" and not plan["override"]:
        security_id = item.get("security_id")
        if security_id:
            current = weekly_valuation.read(session,security_id)
            if not current.get("scenarios") or current.get("stale") or D(stage["max_price"])>D(current["scenarios"][-1]["price"]):
                messages.append("当前估值未齐、过期，或本档最高价超过乐观情景")
    if stage["id"] in ("left","right"):
        held = any(row["symbol"]==plan["symbol"] for row in ctx["positions"])
        if not held or (plan["evaluation"]["kind"]=="新开仓" and not any(f["stage"]=="entry" for f in plan["fills"])):
            messages.append("请先确认首仓成交")
        bars = [b for b in item["bars"] if b["date"]>plan["as_of"]]
        trigger = D(stage["trigger"])
        if stage["id"]=="left":
            ready = bool(bars and D(bars[-1]["low"])<=trigger<=D(bars[-1]["close"]) and D(bars[-1]["close"])>D(bars[-1]["open"]))
            if not ready:
                messages.append("左侧等待触及支撑后收回区间上沿并收阳")
        elif len(bars)<2 or not all(D(b["close"])>trigger for b in bars[-2:]):
            messages.append("右侧等待连续两个完整收盘站上突破位")
    return {"ready":not messages,"reasons":messages,"as_of":ctx["target"],"price":cash(price) if price is not None else None}


def check_plan(plan_id,stage_id):
    with Session() as session, localcontext() as context:
        context.prec=48
        row = session.get(PositionPlan,plan_id)
        if row is None:
            raise LookupError("计划不存在")
        ctx = _context(session,row.symbol)
        plan = _plan_view(row,_plan_status(row,ctx["assets"][row.symbol]))
        return {**_check(session,ctx,plan,_find_stage(plan,stage_id)),"version":row.version,"status":plan["status"]}


def cancel_plan(plan_id,version):
    with workspace.position_lock(), Session() as session:
        row = session.get(PositionPlan,plan_id)
        if row is None:
            raise LookupError("计划不存在")
        if row.version!=version or row.status!="active":
            raise workspace.Conflict("计划已变动，请刷新后重试")
        row.status="cancelled"; row.version=uid()
        row.payload={**row.payload,"cancelled_at":stamp()}
        session.commit()
        return _plan_view(row,row.status)


def record_fill(plan_id,data):
    price = _money(data["price"],"成交价",minimum=risk.CENT)
    fee = _money(data["fee"],"成交费用")
    qty = data["quantity"]
    reason = data["reason"].strip()
    executed = data["execution_session"]
    total = qty*price+fee
    with workspace.position_lock(), Session() as session, localcontext() as context:
        context.prec=48
        plan = session.get(PositionPlan,plan_id)
        if plan is None:
            raise LookupError("计划不存在")
        if plan.version!=data["expected_version"]:
            raise workspace.Conflict("计划已更新，请刷新后核对，避免重复登记")
        ctx = _context(session,plan.symbol)
        current = _plan_view(plan,_plan_status(plan,ctx["assets"][plan.symbol]))
        stage = _find_stage(current,data["stage"])
        if executed>now().astimezone(ZoneInfo("America/New_York")).date() or not ctx["calendar"].is_session(executed.isoformat()):
            raise ValueError("请填写实际成交的美东交易日")
        checked = _check(session,ctx,current,stage)
        warnings = list(checked["reasons"])
        if qty>stage["remaining"] or not D(stage["min_price"])<=price<=D(stage["max_price"]):
            warnings.append("成交价或数量偏离原计划")
        if executed.isoformat()<current["next_session"]:
            warnings.append("成交早于计划生效交易日")
        if warnings and not (reason or current["override"]):
            raise ValueError("请填写实际成交偏离说明："+"；".join(warnings))
        account = session.get(PortfolioAccount,ACCOUNT)
        if account is None:
            raise ValueError("请先填写账户现金")
        previous_cash = _account_view(account)
        booked = data["cash_already_recorded"]
        if booked:
            checked_cash_day = datetime.fromisoformat(account.payload["cash_as_of"]).astimezone(ZoneInfo("America/New_York")).date()
            if checked_cash_day<executed:
                raise ValueError("现金确认时间早于该笔成交；请取消‘现金已包含’或先重新核对余额")
        else:
            available,total_cash = (D(account.payload[key])-total for key in ("available_cash","total_cash"))
            if min(available,total_cash)<0:
                raise ValueError("扣除成交额后现金将为负，请先按券商记录对账")
            account.payload = {**account.payload,"available_cash":cash(available),"total_cash":cash(total_cash)}
        account.version=uid(); account.updated_at=stamp()
        row = session.scalar(select(ManualPosition).where(ManualPosition.symbol==plan.symbol))
        previous = workspace._position(row) if row and row.active else None
        if row is None:
            row = ManualPosition(security_id=plan.security_id,symbol=plan.symbol,currency="USD")
            session.add(row)
        if row.currency!="USD":
            raise ValueError("已有持仓币种与计划不一致")
        old_qty = D(previous["quantity"]) if previous else ZERO
        old_basis = old_qty*D(previous["average_cost"]) if previous else ZERO
        new_qty = old_qty+qty
        row.quantity = format(new_qty,"f")
        row.average_cost = format(((old_basis+total)/new_qty).quantize(D("0.00000001")),"f")
        row.active=True; row.version=uid(); row.updated_at=stamp()
        session.flush()
        protection = session.get(PositionProtection,row.id)
        if protection is None:
            protection = PositionProtection(position_id=row.id,payload={})
            session.add(protection)
        old_protection = protection.payload if previous else {}
        lot = {"session":executed.isoformat(),"quantity":str(qty),"stop":stage["stop"],"warning":stage["warning"],"plan_id":plan.id}
        protection.payload = {"lots":[*old_protection.get("lots",[]),lot],
                              "tactical_qty":str(D(old_protection.get("tactical_qty","0"))+(qty if stage["tactical"] else 0))}
        fill = {"stage":stage["id"],"session":executed.isoformat(),"quantity":qty,"price":cash(price),"fee":cash(fee),
                "recorded_at":stamp(),"warning":stage["warning"],"stop":stage["stop"],"cash_already_recorded":booked,
                "reason":reason or (plan.payload["evaluation"].get("manual") or {}).get("reason"),"checks":warnings}
        stages = [{**s,"remaining":max(0,s["remaining"]-qty)} if s["id"]==stage["id"] else s for s in plan.payload["stages"]]
        plan.payload = {**plan.payload,"stages":stages,"fills":[*plan.payload["fills"],fill]}
        if all(s["remaining"]<=0 for s in stages):
            plan.status="filled"
        elif current["status"] in ("invalidated","expired"):
            plan.status=current["status"]
        plan.version=uid()
        position = workspace._position(row)
        save_snapshot(session,plan.security_id,"portfolio_confirmed_fill",{"plan_id":plan.id,"fill":fill,
                      "previous_position":previous,"position":position,"previous_cash":previous_cash,"cash":_account_view(account)})
        session.commit()
        return {"plan":_plan_view(plan,plan.status),"position":position,"fill":fill}

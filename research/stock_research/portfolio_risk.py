"""单账户 USD 多头开仓风控。风险金额是有口径的情景估算。"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import json
import math
from statistics import correlation, variance

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from . import market_conditions, technical
from .config import ROOT, settings
from .price_features import wilder_atr
from .store import Job, ManualPosition, PositionProtection, Security, latest, now

D = Decimal
ZERO = D(0)
CENT = D("0.01")


def amount(value):
    return D(str(value))


def cash(value, *, up=False):
    return str(value.quantize(CENT,rounding=ROUND_UP if up else ROUND_DOWN))


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid",allow_inf_nan=False)
    version: str
    cash_max_hours: int = Field(ge=1,le=720)
    cash_reserve_fraction: Decimal = Field(ge=0,lt=1)
    tactical_fraction: Decimal = Field(ge=0,le=1)
    trade_risk_fraction: Decimal = Field(gt=0,lt=1)
    single_fraction: Decimal = Field(gt=0,le=1)
    similar_fraction: Decimal = Field(gt=0,le=1)
    aggressive_fraction: Decimal = Field(gt=0,le=1)
    risk_multiplier_cautious: Decimal = Field(gt=0,le=1)
    risk_multiplier_weak: Decimal = Field(gt=0,le=1)
    gap_fraction: Decimal = Field(ge=0,lt=1)
    fee_fraction: Decimal = Field(ge=0,lt=1)
    stop_buffer_atr: Decimal = Field(gt=0,le=5)
    entry_buffer_atr: Decimal = Field(gt=0,le=5)
    stress_atr: Decimal = Field(gt=0,le=20)
    fallback_stress: Decimal = Field(gt=0,le=1)
    aggressive_atr_fraction: Decimal = Field(gt=0,lt=1)
    aggressive_beta: Decimal = Field(gt=0,le=10)
    similar_correlation: Decimal = Field(gt=0,le=1)
    similar_downside: Decimal = Field(gt=0,le=1)
    history_sessions: int = Field(ge=25,le=120)
    min_common_sessions: int = Field(ge=15,le=120)
    min_price: Decimal = Field(gt=0)
    min_dollar_volume: Decimal = Field(gt=0)
    plan_sessions: int = Field(ge=1,le=10)
    stage_fractions: dict[str,Decimal]
    sector_tags: dict[str,list[str]]

    @model_validator(mode="after")
    def validate_limits(self):
        if (set(self.stage_fractions)!={"entry","left","right"} or min(self.stage_fractions.values())<=0
                or sum(self.stage_fractions.values())!=1):
            raise ValueError("首仓与两档加仓分配需均为正且合计100%")
        if self.min_common_sessions>self.history_sessions or self.risk_multiplier_weak>self.risk_multiplier_cautious:
            raise ValueError("风险窗口或大盘缩减系数配置无效")
        return self


def policy():
    try:
        return RiskPolicy.model_validate_json(settings.portfolio_policy_file.read_text(encoding="utf-8"))
    except (OSError,ValueError) as exc:
        raise ValueError("仓位风控配置无效，请核对 PORTFOLIO_POLICY_FILE") from exc


def asset(session, code, target, cfg):
    security = session.scalar(select(Security).where(Security.symbol==code))
    source = latest(session,security.id,"market") if security else None
    payload = source.payload if source else {}
    bars = [b for b in payload.get("bars",[]) if b.get("complete") is True and b["date"]<=target]
    profile = security.profile if security else {}
    valid = (payload.get("coverage_ok") and bars and bars[-1]["date"]==target
             and (payload.get("profile") or {}).get("currency")=="USD")
    tags = {k for k, symbols in cfg.sector_tags.items() if code in symbols}
    if profile.get("sector"):
        tags.add(str(profile["sector"]))
    row = {"symbol":code,"id":source.id if source else None,"security_id":security.id if security else None,
           "profile":profile,"bars":bars if valid else [],
           "technical":payload.get("technical") or {},"tags":tags,"price":None,"atr":None,"daily":{},"stress":{}}
    if not valid:
        return row
    bars = bars[-max(200,cfg.history_sessions+6):]
    raw = [amount(b["adjusted_close"]) for b in bars]
    price = amount(bars[-1]["close"])
    if price<=0 or any(p<=0 for p in raw):
        row["bars"] = []
        return row
    factor = raw[-1]/price
    atr = wilder_atr(bars,14)
    start = max(1,len(raw)-cfg.history_sessions)
    daily = {bars[i]["date"]:float(raw[i]/raw[i-1]-1) for i in range(start,len(raw))}
    stress = {bars[i]["date"]:max(ZERO,1-raw[i]/raw[i-5]) for i in range(max(5,start),len(raw))}
    liquidity = sum(amount(b["close"])*amount(b.get("volume") or 0) for b in bars[-20:])/20 if len(bars)>=20 else None
    row.update(price=price,atr=amount(atr)/factor if atr and math.isfinite(atr) else None,daily=daily,stress=stress,
               factor=factor,liquidity=liquidity)
    return row


def _aligned(left,right):
    days = sorted(left.keys() & right.keys())
    return [left[d] for d in days],[right[d] for d in days]


def aggression(item, qqq, cfg):
    if item["price"] is None:
        return {"label":"待核对","beta":None,"atr_pct":None,"counts_as_aggressive":True}
    atr_ratio = item["atr"]/item["price"] if item["atr"] else None
    x,y = _aligned(item["daily"],qqq["daily"])
    beta = None
    downside = None
    if len(x)>=cfg.min_common_sessions and (den:=variance(y))>0:
        average_x, average_y = sum(x)/len(x),sum(y)/len(y)
        beta = sum((a-average_x)*(b-average_y) for a,b in zip(x,y))/((len(x)-1)*den)
        down_days = [(a,b) for a,b in zip(x,y) if b<0]
        if len(down_days)>=10:
            downside = sum(a for a,_ in down_days)/sum(b for _,b in down_days)
    aggressive = (item["symbol"] in {"QQQ","QQQM","SMH","SOXX"} or
                  (atr_ratio is not None and atr_ratio>=cfg.aggressive_atr_fraction) or
                  any(v is not None and amount(v)>=cfg.aggressive_beta for v in (beta,downside)))
    unknown = beta is None or atr_ratio is None
    return {"label":"进攻型" if aggressive else "待核对" if unknown else "普通",
            "beta":round(beta,3) if beta is not None else None,"downside_capture":round(downside,3) if downside is not None else None,
            "atr_pct":float(atr_ratio) if atr_ratio is not None else None,
            "counts_as_aggressive":bool(aggressive or unknown)}


def index_members():
    try:
        raw = json.loads((ROOT/"config"/"us-index-members.json").read_text(encoding="utf-8"))
        if now()-datetime.fromisoformat(raw["retrieved_at"])>timedelta(days=30):
            return {}
        return {s["index"]:{m["symbol"] for m in s["members"]} for s in raw["sources"]}
    except (OSError,ValueError,KeyError,TypeError):
        return {}


def similarity(left,right,cfg,members):
    if left["symbol"]==right["symbol"]:
        return True,["同一股票"]
    reasons = sorted(left["tags"] & right["tags"])
    overlap = []
    for a,b in ((left["symbol"],right["symbol"]),(right["symbol"],left["symbol"])):
        index = {"SPY":"SP500","QQQ":"NASDAQ100","QQQM":"NASDAQ100"}.get(a)
        if b in members.get(index,set()):
            overlap.append(f"{b}出现在{a}对应指数/持仓名单（权重未知）")
    x,y = _aligned(left["daily"],right["daily"])
    if len(x)>=cfg.min_common_sessions:
        try:
            corr = correlation(x,y)
        except ValueError:
            corr = None
        if corr is not None and amount(corr)>=cfg.similar_correlation:
            reasons.append(f"日收益相关性 {corr:.2f}")
        down = [b for a,b in zip(x,y) if a<0]
        if len(down)>=10 and amount(sum(b<0 for b in down))/len(down)>=cfg.similar_downside:
            reasons.append(f"候选股下跌时同跌 {sum(b<0 for b in down)}/{len(down)} 日")
    return bool(reasons),reasons+overlap


def stress_fraction(item,cfg):
    historical = max(item["stress"].values(),default=ZERO)
    volatility = item["atr"]/item["price"]*cfg.stress_atr if item["atr"] and item["price"] else ZERO
    fallback = cfg.fallback_stress if len(item["stress"])<cfg.min_common_sessions or not volatility else ZERO
    return min(D(1),max(historical,volatility,fallback))


def market_context(session, spy, qqq, target, cfg):
    report = None
    for job in session.scalars(select(Job).where(Job.kind=="scheduled_market_brief",Job.status=="completed")
                               .order_by(Job.created_at.desc()).limit(10)):
        value = job.result.get("report") or {}
        if value.get("as_of_session")==target and set(value.get("benchmarks",{}))=={"SPY","QQQ"}:
            report = value
            break
    vix = report.get("vix") if report else None
    aligned = report and all(report.get("market_data",{}).get(row["symbol"],{}).get("snapshot_id")==row["id"]
                             for row in (spy,qqq))
    if aligned:
        benchmarks = report["benchmarks"]
    else:
        benchmarks = {}
        for row in (spy,qqq):
            if len(row["bars"])>=60:
                try:
                    benchmarks[row["symbol"]] = market_conditions.benchmark(row["bars"],weekly=False,shortened=set())
                except (ValueError,OSError):
                    continue
    state = market_conditions.assessment(benchmarks,None,vix,[])["state"]
    notes = []
    if vix is None:
        notes.append("缺当日 VIX，额度按谨慎档")
    elif vix.get("elevated"):
        notes.append(f"VIX {vix['close']:.2f}，波动预期升高")
    broken = False
    for code,row in benchmarks.items():
        pressure = row.get("resistance")
        if pressure and 0<=pressure["low"]-row["close"]<=0.5*row["atr"]:
            notes.append(f"{code} 接近压力 {pressure['low']:.2f}–{pressure['high']:.2f}")
        for event in row.get("events",[]):
            if event["date"]==target and event["kind"]=="破位":
                notes.append(f"{code} 最新收盘破位")
                broken = True
                break
    multiplier = (cfg.risk_multiplier_weak if broken or state=="偏弱" else
                  cfg.risk_multiplier_cautious if notes or state!="偏强" else D(1))
    return {"state":state,"notes":notes,"vix":vix,"multiplier":str(multiplier),"hold_new":broken and bool(vix and vix["elevated"]),
            "source":report.get("prepared_at") if report else None}


def exposure(session, assets, qqq, cfg):
    rows, weighted, unknown = [], [], []
    market_value = guard_risk = tactical = aggressive_value = ZERO
    for position in session.scalars(select(ManualPosition).where(ManualPosition.active.is_(True)).order_by(ManualPosition.symbol)):
        item = assets[position.symbol]
        qty = amount(position.quantity)
        profile = aggression(item,qqq,cfg)
        protection = session.get(PositionProtection,position.id)
        meta = protection.payload if protection else {}
        lots = meta.get("lots",[])
        price = item["price"]
        row = {"symbol":position.symbol,"id":position.id,"version":position.version,"quantity":position.quantity,
               "market_value":None,"guard_risk":None,"alerts":[],**profile}
        if price is None or position.currency!="USD":
            unknown.append(position.symbol)
        else:
            value = qty*price
            market_value += value
            if profile["counts_as_aggressive"]:
                aggressive_value += value
            tactical += min(qty,amount(meta.get("tactical_qty","0")))*price
            fraction = stress_fraction(item,cfg)
            remaining = qty
            guard = ZERO
            for lot in lots:
                if item["bars"][-1]["date"]<lot["session"]:
                    row["alerts"].append({"state":"等待成交日完整收盘","stop":lot["stop"],"warning":lot["warning"]})
                    continue
                if (lot["session"]<item["bars"][0]["date"] or any(
                        b.get("split") not in (None,"0","0.0",0) for b in item["bars"] if b["date"]>=lot["session"])):
                    row["alerts"].append({"state":"保护线待复核（拆股或历史不足）","stop":lot["stop"],"warning":lot["warning"]})
                    continue
                allocated = min(remaining,amount(lot["quantity"]))
                stop = amount(lot["stop"])
                warning = amount(lot["warning"])
                breached = any(amount(b["low"])<stop for b in item["bars"] if b["date"]>lot["session"])
                level = "保护位已触及" if price<=stop or breached else "预警" if price<=warning else "跟踪中"
                row["alerts"].append({"state":level,"stop":lot["stop"],"warning":lot["warning"],"quantity":str(allocated)})
                per_share = max(ZERO,price-stop)+price*(cfg.gap_fraction+cfg.fee_fraction)
                guard += allocated*(max(per_share,price*fraction) if level=="保护位已触及" else per_share)
                remaining -= allocated
                if remaining<=0:
                    break
            guard += remaining*price*fraction
            guard_risk += guard
            if meta.get("reconciled") and remaining>0:
                row["alerts"].append({"state":"导入对账后，未绑定数量按波动估算","stop":None,"warning":None})
            row.update(market_value=cash(value),guard_risk=cash(guard,up=True),unguarded_quantity=str(remaining),
                       stress_fraction=str(fraction))
            weighted.append((value,item["stress"],fraction))
        rows.append(row)
    days = set.intersection(*(set(stress) for _,stress,_ in weighted)) if weighted else set()
    shock = max((sum(value*stress[day] for value,stress,_ in weighted) for day in days),default=ZERO)
    if len(days)<cfg.min_common_sessions:
        shock = sum((value*fraction for value,_,fraction in weighted),ZERO)
    return {"positions":rows,"unknown":unknown,"market_value":market_value,"guard_risk":guard_risk,
            "stress_risk":shock,"stress_samples":len(days),"aggressive_value":aggressive_value,"tactical_value":tactical}

"""第二模块第一阶段：确定性的日线/完整周线趋势，不进行策略匹配或交易决策。"""
import copy
import fcntl
import json
import math
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError, model_validator
from sqlalchemy import select

from . import market
from .config import settings
from .price_features import confirmed_pivots, wilder_atr
from .store import Session, Snapshot, digest, latest, save_snapshot, serialize_snapshot, stamp

ALGORITHM="three-family-completed-bar-trend-v2.1-progress"
LABELS={"UP":"上涨","RANGE":"震荡","DOWN":"下降"}
STATUSES={"CONFIRMED":"已确认","PENDING":"待确认","INSUFFICIENT_DATA":"数据不足","STALE":"数据过期/不可确认"}
FAMILY_LABELS={"structure":"结构","moving_average":"均线","persistence":"持续性"}


@contextmanager
def _calculation_lock(security_id,cached_only):
    if cached_only:
        yield
        return
    directory=settings.storage/"trend-locks"
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/(digest(security_id)+".lock")).open("a") as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("该股票已有趋势计算正在进行，请等待其结束，避免重复生成同版本评估") from exc
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)


class StrictPolicy(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)


class DirectionPolicy(StrictPolicy):
    structure_delta_atr: FiniteFloat=Field(ge=0)
    slope_min: FiniteFloat=Field(gt=0)
    efficiency_min: FiniteFloat=Field(gt=0,le=1)
    displacement_atr_min: FiniteFloat=Field(gt=0)
    price_buffer_atr: FiniteFloat
    alignment_buffer_atr: FiniteFloat


class RangePolicy(StrictPolicy):
    lookback: int=Field(ge=10,le=500)
    minimum_touches: int=Field(ge=2,le=10)
    touch_spacing: int=Field(ge=1,le=100)
    touch_tolerance_atr: FiniteFloat=Field(gt=0)
    minimum_width_atr: FiniteFloat=Field(gt=0)
    maximum_width_atr: FiniteFloat=Field(gt=0)
    containment_min: FiniteFloat=Field(gt=0,le=1)
    breakout_buffer_atr: FiniteFloat=Field(ge=0)
    slope_max: FiniteFloat=Field(ge=0)
    efficiency_max: FiniteFloat=Field(ge=0,lt=1)
    crossing_window: int=Field(ge=2,le=200)
    minimum_crossings: int=Field(ge=1,le=100)


class TimeframePolicy(StrictPolicy):
    ma_fast: int=Field(ge=2,le=500)
    ma_slow: int=Field(ge=3,le=1000)
    slope_lookback: int=Field(ge=1,le=100)
    structure_lookback: int=Field(ge=10,le=1000)
    pivot_count: int=Field(ge=2,le=10)
    efficiency_window: int=Field(ge=2,le=200)
    enter: DirectionPolicy
    hold: DirectionPolicy
    range: RangePolicy
    confirmation_bars: int=Field(ge=1,le=20)
    minimum_dwell: int=Field(ge=0,le=100)

    @model_validator(mode="after")
    def ordered(self):
        if self.ma_fast>=self.ma_slow:
            raise ValueError("趋势快均线窗口必须小于慢均线")
        if any(getattr(self.hold,k)>getattr(self.enter,k) for k in DirectionPolicy.model_fields):
            raise ValueError("保持门槛不能严于进入门槛")
        if self.range.efficiency_max>=self.enter.efficiency_min or self.range.slope_max>=self.enter.slope_min:
            raise ValueError("震荡与方向进入门槛必须留出待确认区间")
        if self.range.minimum_width_atr>=self.range.maximum_width_atr:
            raise ValueError("震荡最小宽度必须小于最大宽度")
        if self.range.minimum_crossings>=self.range.crossing_window:
            raise ValueError("均线交叉次数不能达到或超过交叉窗口")
        if self.range.minimum_width_atr<=2*self.range.touch_tolerance_atr:
            raise ValueError("区间宽度必须大于上下沿触及容差之和")
        return self


class TrendPolicy(StrictPolicy):
    version: str
    review_status: str
    note: str
    daily: TimeframePolicy
    weekly: TimeframePolicy


def policy(policy_file=None):
    chosen=Path(policy_file or settings.trend_policy_file).expanduser()
    try:
        cfg=TrendPolicy.model_validate(json.loads(chosen.read_text()))
    except (OSError,ValueError,ValidationError) as exc:
        raise ValueError(f"趋势配置无效，请检查{chosen.name}的字段、数值和阈值顺序") from exc
    spacing=settings.pivot_left+settings.pivot_right+1
    for frame in (cfg.daily,cfg.weekly):
        if frame.structure_lookback<(frame.pivot_count-1)*spacing+settings.pivot_right+1:
            raise ValueError("结构窗口不足以容纳所需独立确认拐点，请扩大structure_lookback")
        if frame.range.lookback<(frame.range.minimum_touches-1)*max(spacing,frame.range.touch_spacing)+settings.pivot_right+1:
            raise ValueError("震荡窗口不足以容纳所需独立边界触及，请扩大range.lookback")
    return {"policy":cfg.model_dump(),"shared_features":{"pivot_left":settings.pivot_left,
        "pivot_right":settings.pivot_right,"atr_window":settings.atr_window},"ma_type":"SMA"}


def _bars(payload):
    result=[dict(b) for b in payload.get("bars",[]) if b.get("complete") is True]
    previous=None
    for bar in result:
        day=date.fromisoformat(bar["date"])
        if previous is not None and day<=previous:
            raise ValueError("行情日期重复或顺序异常，不能确认趋势")
        previous=day
        for key in ("adjusted_open","adjusted_high","adjusted_low","adjusted_close"):
            number=float(bar[key])
            if not math.isfinite(number) or number<=0:
                raise ValueError("调整价格缺失或无效，不能确认趋势")
        if not (float(bar["adjusted_low"])<=min(float(bar["adjusted_open"]),float(bar["adjusted_close"]))
                <=max(float(bar["adjusted_open"]),float(bar["adjusted_close"]))<=float(bar["adjusted_high"])):
            raise ValueError("调整价格OHLC关系不一致，不能确认趋势")
    return result


def _calendar(start,end):
    # 日历实例首尾实际落在交易日。预留两侧日期，才能查询元旦/周末等非交易日边界。
    return xcals.get_calendar("XNYS",start=(start-timedelta(days=7)).isoformat(),
        end=(end+timedelta(days=14)).isoformat())


def completed_weeks(bars):
    """按交易所计划交易日聚合，排除开头残缺周和当前尚未完成的周。"""
    if not bars:
        return []
    first=date.fromisoformat(bars[0]["date"]);last=date.fromisoformat(bars[-1]["date"])
    start=first-timedelta(days=first.weekday());end=last+timedelta(days=6-last.weekday())
    cal=_calendar(start,end)
    schedule={}
    for session in cal.sessions_in_range(start.isoformat(),end.isoformat()):
        d=session.date();week=d-timedelta(days=d.weekday())
        schedule.setdefault(week,[]).append(d.isoformat())
    by_date={b["date"]:b for b in bars};result=[]
    expected={d for dates in schedule.values() for d in dates if first.isoformat()<=d<=last.isoformat()}
    if set(by_date)!=expected:
        raise ValueError("行情存在交易日缺口或非交易日记录，不能聚合为有效日/周趋势")
    for dates in schedule.values():
        if dates[-1]>last.isoformat() or not all(d in by_date for d in dates):
            continue
        items=[by_date[d] for d in dates]
        result.append({"date":dates[-1],"week_start":dates[0],"complete":True,
            "adjusted_open":items[0]["adjusted_open"],"adjusted_close":items[-1]["adjusted_close"],
            "adjusted_high":max(float(b["adjusted_high"]) for b in items),
            "adjusted_low":min(float(b["adjusted_low"]) for b in items),
            "volume":sum(float(b.get("volume",0)) for b in items),"source_sessions":dates})
    return result


def _spaced(points, spacing):
    selected=[]
    for point in points:
        if not selected or point["index"]-selected[-1]["index"]>=spacing:
            selected.append(point)
    return selected


def _check(code, passed, actual, requirement):
    return {"code":code,"passed":bool(passed),"actual":actual,"requirement":requirement}


def classify(bars,cfg,shared):
    closes=[float(b["adjusted_close"]) for b in bars]
    atr=wilder_atr(bars,shared["atr_window"])
    warmup=max(cfg["ma_slow"],cfg["ma_fast"]+cfg["slope_lookback"],
        cfg["ma_fast"]+cfg["range"]["crossing_window"]-1,shared["atr_window"]+1,
        shared["pivot_left"]+shared["pivot_right"]+1,cfg["efficiency_window"]+1,cfg["range"]["lookback"])
    if len(bars)<warmup or atr is None or atr<=0 or not math.isfinite(atr):
        return {"ready":False,"required_bars":warmup,"available_bars":len(bars),
            "reason_codes":["INSUFFICIENT_WARMUP" if len(bars)<warmup else "INVALID_ATR"],"checks":{}}
    fast=cfg["ma_fast"];slow=cfg["ma_slow"];k=cfg["slope_lookback"];n=cfg["efficiency_window"]
    ma=sum(closes[-fast:])/fast;long_ma=sum(closes[-slow:])/slow
    past_ma=sum(closes[-fast-k:-k])/fast
    slope=(ma-past_ma)/(k*atr)
    displacement=(closes[-1]-closes[-n-1])/atr
    travel=sum(abs(closes[i]-closes[i-1]) for i in range(len(closes)-n,len(closes)))
    er=abs(closes[-1]-closes[-n-1])/travel if travel else 0.0
    spacing=shared["pivot_left"]+shared["pivot_right"]+1
    points=confirmed_pivots(bars,shared["pivot_left"],shared["pivot_right"],
        max(cfg["structure_lookback"],cfg["range"]["lookback"]))
    groups={kind:_spaced([p for p in points if p["kind"]==kind and
        p["index"]>=len(bars)-cfg["structure_lookback"]],spacing)[-cfg["pivot_count"]:] for kind in ("high","low")}
    enough=all(len(p)>=cfg["pivot_count"] for p in groups.values())
    independent=enough and len({p["index"] for g in groups.values() for p in g})==2*cfg["pivot_count"]
    deltas={kind:[(p[i]["price"]-p[i-1]["price"])/atr for i in range(1,len(p))] for kind,p in groups.items()}
    checks={};hold={}
    for direction,sign in (("UP",1),("DOWN",-1)):
        for mode,target in (("enter",checks),("hold",hold)):
            t=cfg[mode]
            target[direction]={
                "structure":[_check("INDEPENDENT_PIVOTS",independent,{k:len(v) for k,v in groups.items()},f"各{cfg['pivot_count']}个独立高/低点"),
                    *[_check("HIGHER_"+kind.upper() if sign==1 else "LOWER_"+kind.upper(),
                        len(groups[kind])>=cfg["pivot_count"] and all(sign*x>t["structure_delta_atr"] for x in deltas[kind]),deltas[kind],
                        f"方向调整后的相邻变化均大于{t['structure_delta_atr']} ATR") for kind in ("high","low")]],
                "moving_average":[_check("MA_SLOPE",sign*slope>=t["slope_min"],slope,f"方向斜率≥{t['slope_min']} ATR/根"),
                    _check("PRICE_POSITION",sign*(closes[-1]-ma)/atr>=t["price_buffer_atr"],(closes[-1]-ma)/atr,f"方向价格距快均线≥{t['price_buffer_atr']} ATR"),
                    _check("MA_ALIGNMENT",sign*(ma-long_ma)/atr>=t["alignment_buffer_atr"],(ma-long_ma)/atr,f"方向快慢均线差≥{t['alignment_buffer_atr']} ATR")],
                "persistence":[_check("EFFICIENCY",er>=t["efficiency_min"],er,f"ER≥{t['efficiency_min']}"),
                    _check("DIRECTIONAL_DISPLACEMENT",sign*displacement>=t["displacement_atr_min"],displacement,f"方向位移≥{t['displacement_atr_min']} ATR")]}
    r=cfg["range"];boundary_points=[p for p in points if p["index"]>=len(bars)-r["lookback"]]
    highs=[p for p in boundary_points if p["kind"]=="high"];lows=[p for p in boundary_points if p["kind"]=="low"]
    upper=max((p["price"] for p in highs),default=None);lower=min((p["price"] for p in lows),default=None)
    upper_touches=[];lower_touches=[];width=None;containment=0.0;inside=False
    if upper is not None and lower is not None and upper>lower:
        upper_touches=_spaced([p for p in highs if upper-p["price"]<=r["touch_tolerance_atr"]*atr],max(spacing,r["touch_spacing"]))
        occupied={p["index"] for p in upper_touches}
        lower_touches=_spaced([p for p in lows if p["index"] not in occupied and p["price"]-lower<=r["touch_tolerance_atr"]*atr],max(spacing,r["touch_spacing"]))
        width=(upper-lower)/atr
        margin=r["breakout_buffer_atr"]*atr
        containment=sum(lower-margin<=x<=upper+margin for x in closes[-r["lookback"]:])/r["lookback"]
        inside=lower-margin<=closes[-1]<=upper+margin
    signs=[]
    for end in range(len(closes)-r["crossing_window"]+1,len(closes)+1):
        delta=closes[end-1]-sum(closes[end-fast:end])/fast
        if delta:
            signs.append(1 if delta>0 else -1)
    crossings=sum(signs[i]!=signs[i-1] for i in range(1,len(signs)))
    range_checks={
        "structure":[_check("RANGE_BOUNDARY_TOUCHES",len(upper_touches)>=r["minimum_touches"] and len(lower_touches)>=r["minimum_touches"],
            {"upper":len(upper_touches),"lower":len(lower_touches)},f"上下边各≥{r['minimum_touches']}次独立已确认触及"),
            _check("RANGE_WIDTH",width is not None and r["minimum_width_atr"]<=width<=r["maximum_width_atr"],width,f"宽度{r['minimum_width_atr']}–{r['maximum_width_atr']} ATR"),
            _check("RANGE_CONTAINMENT",containment>=r["containment_min"] and inside,containment,f"收盘包含率≥{r['containment_min']}且当前未有效越界")],
        "moving_average":[_check("FLAT_MA",abs(slope)<=r["slope_max"],slope,f"绝对斜率≤{r['slope_max']} ATR/根"),
            _check("MA_CROSSINGS",crossings>=r["minimum_crossings"],crossings,f"最近{r['crossing_window']}根至少{r['minimum_crossings']}次穿越快均线")],
        "persistence":[_check("LOW_EFFICIENCY",er<=r["efficiency_max"],er,f"ER≤{r['efficiency_max']}")]}
    checks["RANGE"]=range_checks;hold["RANGE"]=range_checks
    def passed(families):
        return all(c["passed"] for items in families.values() for c in items)
    matches=[name for name,items in checks.items() if passed(items)]
    numbers=[ma,long_ma,slope,displacement,er]
    if any(not math.isfinite(x) for x in numbers):
        raise ValueError("趋势归一化指标无效，停止生成判断")
    return {"ready":True,"candidate":matches[0] if len(matches)==1 else None,"matches":matches,
        "hold_matches":{name:passed(items) for name,items in hold.items()},"checks":checks,"hold_checks":hold,
        "family_support":{name:{family:all(c["passed"] for c in items) for family,items in value.items()} for name,value in checks.items()},
        "features":{"atr":atr,"ma_fast":ma,"ma_slow":long_ma,"ma_slope_atr_per_bar":slope,"efficiency_ratio":er,
            "signed_displacement_atr":displacement,"latest_close_adjusted":closes[-1],"pivots":groups,
            "range":{"lower":lower,"upper":upper,"width_atr":width,"containment":containment,
                "upper_touches":upper_touches,"lower_touches":lower_touches,"ma_crossings":crossings}},
        "reason_codes":(["MULTIPLE_TRENDS_MATCH"] if len(matches)>1 else ["NO_COMPLETE_THREE_FAMILY_MATCH"] if not matches else ["THREE_FAMILIES_MATCH"])}


def _progress(evidence, current, candidate, status, mode, count, required):
    """解释尚未确认期间已有的方向证据；不替代三族确认，不成为策略放行条件。"""
    if status=="INSUFFICIENT_DATA":
        return {"code":"INSUFFICIENT_DATA","label":"数据不足","direction":None,
            "blocking_families":[],"blocking_checks":[],"explanation":"完整历史或有效ATR不足，尚不能比较方向证据"}
    if len(evidence.get("matches",[]))>1:
        return {"code":"MIXED","label":"证据冲突/方向不明","direction":None,
            "blocking_families":[],"blocking_checks":[],"explanation":"多个完整分类同时满足，需要核对规则，不能据此确认趋势"}
    support=evidence.get("family_support",{})
    focus=current or candidate
    if current:
        code="CONFIRMED_"+current;label="已确认"+LABELS[current]
    elif candidate:
        code="CONFIRMING_"+candidate;label=LABELS[candidate]+"确认中"
    else:
        # 优先报告均线与动能是否共同转向，再报告单独动能。没有多数投票晋级趋势。
        aligned=[d for d in ("UP","DOWN") if support.get(d,{}).get("moving_average")]
        impulse=[d for d in ("UP","DOWN") if support.get(d,{}).get("persistence")]
        repairs=[]
        for direction in impulse:
            ma={c["code"]:c["passed"] for c in evidence["checks"][direction]["moving_average"]}
            if ma.get("MA_SLOPE") and ma.get("PRICE_POSITION"):
                repairs.append(direction)
        if len(aligned)==1 and (not impulse or impulse==aligned):
            focus=aligned[0]
            if support[focus]["persistence"]:
                code=focus+"_FORMING";label=LABELS[focus]+"形成中"
            elif support[focus]["structure"]:
                code=focus+"_MOMENTUM_WEAK";label=("上涨" if focus=="UP" else "下降")+"动能减弱"
            else:
                code=focus+"_MA_BIAS";label="均线偏"+("多" if focus=="UP" else "空")+"，持续性不足"
        elif len(repairs)==1:
            focus=repairs[0];code=focus+"_REPAIR";label="向"+("上" if focus=="UP" else "下")+"修复"
        elif len(impulse)==1:
            focus=impulse[0];code=focus+"_IMPULSE";label="短期"+("上行" if focus=="UP" else "下行")+"动能"
        elif sum(bool(v) for v in support.get("RANGE",{}).values())>=2:
            focus="RANGE";code="RANGE_FORMING";label="震荡证据未齐"
        else:
            code="MIXED";label="证据冲突/方向不明"
    source=evidence.get("hold_checks" if mode=="hold" else "checks",{}).get(focus,{})
    blocked=[family for family,items in source.items() if not all(c["passed"] for c in items)]
    failures=[{"family":family,**item} for family,items in source.items() for item in items if not item["passed"]]
    if current:
        explanation="三族"+("保持" if mode=="hold" else "进入")+"条件满足"
    elif candidate:
        explanation=f"三族进入条件已满足；连续确认{count}/{required}根，另需满足最短保持时段"
    elif focus:
        explanation="已有部分"+LABELS[focus]+"证据；尚缺"+"、".join(FAMILY_LABELS[f] for f in blocked)
    else:
        explanation="三族证据未形成一致方向；不将未知自动归为震荡"
    return {"code":code,"label":label,"direction":focus,"blocking_families":blocked,
        "blocking_checks":failures,"explanation":explanation}


def assess_series(bars,cfg,shared, *, include_history=False):
    """按每根完整K线顺序重放确认状态；重复命令不会额外增加确认根数。"""
    last=None;last_at=None;regime_index=None;regime_start=None
    candidate=None;since=None;count=0;events=[];previous_signature=None;result=None;records=[]
    for i,bar in enumerate(bars):
        evidence=classify(bars[:i+1],cfg,shared)
        raw=evidence.get("candidate");current=None;mode=None
        if not evidence["ready"]:
            status="INSUFFICIENT_DATA";candidate=None;since=None;count=0
        elif last and result and result.get("current_trend")==last and raw in (None,last) and evidence["hold_matches"].get(last) and len(evidence["matches"])<=1:
            current=last;status="CONFIRMED";candidate=None;since=None;count=0;last_at=bar["date"];mode="hold"
        elif raw:
            if candidate==raw:
                count+=1
            else:
                candidate=raw;count=1;since=bar["date"]
            dwell_ok=regime_index is None or i-regime_index>=cfg["minimum_dwell"]
            if count>=cfg["confirmation_bars"] and dwell_ok:
                current=raw;status="CONFIRMED";last_at=bar["date"];mode="enter"
                if last!=raw or not result or result.get("current_trend")!=raw:
                    regime_start=bar["date"];regime_index=i
                last=raw;candidate=None;since=None;count=0
            else:
                status="PENDING"
        else:
            status="PENDING";candidate=None;since=None;count=0
        progress=_progress(evidence,current,candidate,status,mode,count,cfg["confirmation_bars"])
        signature=(current,candidate,status,last,progress["code"],tuple(progress["blocking_families"]))
        reasons=(["HYSTERESIS_HOLD"] if mode=="hold" else evidence["reason_codes"])
        reasons=reasons+(["WAITING_FOR_CONFIRMATION_OR_DWELL"] if candidate else [])
        if signature!=previous_signature:
            events.append({"session":bar["date"],"current_trend":current,"candidate_trend":candidate,
                "assessment_status":status,"last_confirmed_trend":last,"reason_codes":reasons,"progress":progress})
            previous_signature=signature
        result={"as_of":bar["date"],"current_trend":current,"candidate_trend":candidate,
            "last_confirmed_trend":last,"last_confirmed_at":last_at,"confirmed_since":regime_start,
            "assessment_status":status,"candidate_since":since,"confirmation_count":count,
            "confirmation_required":cfg["confirmation_bars"],"decision_mode":mode,
            "evidence_strength":"一致" if current or raw else "部分一致，尚未确认" if progress["direction"] else "证据不足或冲突",
            "reason_codes":reasons,"progress":progress,
            "evidence":evidence,"trend_dependency_ready":status=="CONFIRMED"}
        if include_history:
            record={k:v for k,v in result.items() if k!="evidence"}
            features=evidence.get("features",{})
            record["features"]={k:features.get(k) for k in ("latest_close_adjusted","atr","ma_fast","ma_slow",
                "ma_slope_atr_per_bar","efficiency_ratio","signed_displacement_atr","pivots","range") if k in features}
            record["family_support"]=evidence.get("family_support",{})
            if mode=="hold":
                record["family_support_used"]={name:all(c["passed"] for c in items)
                    for name,items in evidence.get("hold_checks",{}).get(current,{}).items()}
            records.append(record)
    if result is None:
        result={"as_of":None,"current_trend":None,"candidate_trend":None,"last_confirmed_trend":None,
            "assessment_status":"INSUFFICIENT_DATA","reason_codes":["NO_COMPLETE_BARS"],"evidence":{},"trend_dependency_ready":False}
    result["transitions"]=events
    if include_history:
        result["assessments"]=records
    return result


def _input_key(snapshot,params):
    return digest({"algorithm":ALGORITHM,"parameters":params,"calendar_version":xcals.__version__,
        "bars":[b for b in snapshot.payload.get("bars",[]) if b.get("complete") is True],"adjustment":snapshot.payload.get("adjustment"),
        "calendar":snapshot.payload.get("calendar")})


def _decorate(payload,snapshot,params):
    result=copy.deepcopy(payload)
    state=market.cache_state(snapshot)
    obsolete=(not snapshot or result.get("input_key")!=_input_key(snapshot,params))
    result["market_cache"]=state
    result["stale"]=state["state"]!="fresh" or obsolete
    for name in ("daily","weekly"):
        view=result.get(name,{"last_confirmed_trend":None})
        if result["stale"]:
            view["historical_assessment"]={k:view.get(k) for k in ("current_trend","candidate_trend","assessment_status","as_of","confirmation_count","candidate_since","progress")}
            view.update(current_trend=None,candidate_trend=None,assessment_status="STALE",trend_dependency_ready=False,
                confirmation_count=0,candidate_since=None)
            view["reason_codes"]=["INPUT_OR_CONFIG_CHANGED" if obsolete else "MARKET_NOT_FRESH"]
            view["progress"]={"code":"STALE","label":"数据过期/不可确认","direction":None,
                "blocking_families":[],"blocking_checks":[],"explanation":"历史阶段证据不能作为当前有效方向"}
        view["trend_label"]=LABELS.get(view.get("current_trend"),"无有效方向")
        view["candidate_label"]=LABELS.get(view.get("candidate_trend"),"无")
        view["last_confirmed_label"]=LABELS.get(view.get("last_confirmed_trend"),"无")
        view["status_label"]=STATUSES[view["assessment_status"]]
        result[name]=view
    return result


def evaluate_trend(security_id, *, cached_only=False, policy_file=None):
    """公共判断方法：只读第一模块行情；获取由命令行/未来调用方独立负责。"""
    with _calculation_lock(security_id,cached_only):
        return _evaluate_trend(security_id,cached_only=cached_only,policy_file=policy_file)


def _evaluate_trend(security_id, *, cached_only, policy_file):
    params=policy(policy_file)
    with Session() as session:
        source=latest(session,security_id,"market")
        if not source:
            raise ValueError("尚无行情缓存，请先执行stock-trend 股票代码获取日线")
        key=_input_key(source,params)
        saved=session.scalar(select(Snapshot).where(Snapshot.security_id==security_id,Snapshot.kind=="trend",
            Snapshot.fingerprint==key).order_by(Snapshot.created_at.desc()).limit(1))
        if saved:
            return {**_decorate(serialize_snapshot(saved),source,params),"cached":True}
        old=latest(session,security_id,"trend")
        if cached_only:
            if not old:
                raise ValueError("尚无已保存趋势，请先运行一次不带--cached的趋势查询")
            return {**_decorate(serialize_snapshot(old),source,params),"cached":True}
        if market.cache_state(source)["state"]!="fresh":
            historical=serialize_snapshot(old) if old else {"daily":{},"weekly":{}}
            return {**_decorate(historical,source,params),"cached":bool(old),"message":"行情不可确认有效，只展示历史状态，不生成新的有效趋势"}
        bars=_bars(source.payload)
        if not bars:
            raise ValueError("没有完整日线，不能判断趋势")
        expected=market.cache_state(source).get("expected_session")
        if bars[-1]["date"]!=source.payload.get("last_complete_session") or (expected and bars[-1]["date"]!=expected):
            raise ValueError("完整日线截止日与交易所日历不一致，不能生成有效趋势")
        weeks=completed_weeks(bars)
        payload={"algorithm":ALGORITHM,"input_key":key,"created_at":stamp(),"parameters":params,
            "source_market_id":source.id,"source_market_version":source.fingerprint,
            "last_complete_session":source.payload.get("last_complete_session"),"daily_count":len(bars),"weekly_count":len(weeks),
            "daily":assess_series(bars,params["policy"]["daily"],params["shared_features"]),
            "weekly":assess_series(weeks,params["policy"]["weekly"],params["shared_features"]),
            "review_status":"draft","research_only":True,"strategy_evaluated":False,
            "message":"日线主趋势与周线背景分别判断；证据强度不是上涨概率；未进行策略、账户风控或买卖判断",
            "replay_note":"状态确认按当前存档的每根完整K线顺序重建；使用现有复权数据，不等于取得当时版本的无偏历史回测"}
        item=save_snapshot(session,security_id,"trend",payload);item.fingerprint=key
        session.commit()
        return {**_decorate(serialize_snapshot(item),source,params),"cached":False}


def _intervals(records, start):
    """按状态、证据阶段和阻塞项分段再裁剪；确认日绝不回填到候选开始日。"""
    groups=[]
    for row in records:
        progress=row.get("progress",{})
        signature=(row["current_trend"],row["candidate_trend"],row["assessment_status"],progress.get("code"),
            tuple(progress.get("blocking_families",[])),
            tuple((c["family"],c["code"]) for c in progress.get("blocking_checks",[])))
        if groups and groups[-1]["signature"]==signature:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"signature":signature,"rows":[row]})
    result=[]
    for i,group in enumerate(groups):
        visible=[row for row in group["rows"] if row["as_of"]>=start]
        if not visible:
            continue
        first=visible[0];last=visible[-1]
        result.append({"start_session":first["as_of"],"end_session":last["as_of"],"bar_count":len(visible),
            "current_trend":first["current_trend"],"candidate_trend":first["candidate_trend"],
            "assessment_status":first["assessment_status"],"trend_label":LABELS.get(first["current_trend"],"无有效方向"),
            "candidate_label":LABELS.get(first["candidate_trend"],"无"),"status_label":STATUSES[first["assessment_status"]],
            "observed_state_started":group["rows"][0]["as_of"],"continues_from_warmup":group["rows"][0]["as_of"]<start,
            "confirmed_since":first.get("confirmed_since"),"candidate_since":first.get("candidate_since"),
            "progress":first.get("progress"),"end_progress":last.get("progress"),
            "start_reason_codes":first["reason_codes"],"end_reason_codes":last["reason_codes"],
            "end_is_observation_cutoff":i==len(groups)-1})
    return result


def evaluate_timeline(security_id, *, history_start, start, end=None, cached_only=False, policy_file=None):
    """历史公共方法：预热历史可用，展示从start开始；每个判断只接收当时价格前缀。"""
    with _calculation_lock(security_id,cached_only):
        return _evaluate_timeline(security_id,history_start=history_start,start=start,end=end,
            cached_only=cached_only,policy_file=policy_file)


def _evaluate_timeline(security_id, *, history_start, start, end, cached_only, policy_file):
    try:
        seed=date.fromisoformat(history_start);begin=date.fromisoformat(start)
        until=date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValueError("日期必须使用YYYY-MM-DD格式") from exc
    if seed>begin or (until and until<begin):
        raise ValueError("需要满足历史起点≤展示起点≤展示终点")
    history_start=seed.isoformat();start=begin.isoformat();end=until.isoformat() if until else None
    chosen_policy=policy_file or settings.trend_history_policy_file
    params=policy(chosen_policy)
    with Session() as session:
        source=latest(session,security_id,"market")
        if not source:
            raise ValueError("尚无行情档案，请先获取日线")
        # 先裁剪，再校验/计算；即使本地有2025年行情，也不参与2026样本预热。
        chosen=[b for b in source.payload.get("bars",[]) if b.get("complete") is True
            and b["date"]>=history_start and (not end or b["date"]<=end)]
        bars=_bars({"bars":chosen})
        if not bars or bars[-1]["date"]<start:
            raise ValueError("展示窗口内没有完整日线，已有数据保留；请核对股票和起止日期")
        # 固定历史窗口不使用实时TTL判断；来源修订、参数或日历版本改变会产生新键。
        key=digest({"algorithm":ALGORITHM,"mode":"timeline-v1","history_start":history_start,"start":start,"end":end,
            "parameters":params,"bars":bars,"calendar_version":xcals.__version__,"adjustment":source.payload.get("adjustment")})
        saved=session.scalar(select(Snapshot).where(Snapshot.security_id==security_id,Snapshot.kind=="trend_timeline",
            Snapshot.fingerprint==key).order_by(Snapshot.created_at.desc()).limit(1))
        if saved:
            return {**serialize_snapshot(saved),"cached":True}
        if cached_only:
            raise ValueError("该历史窗口/配置还没有匹配的趋势结果，请去掉--cached计算；本次未联网或重算")
        weeks=completed_weeks(bars)
        daily=assess_series(bars,params["policy"]["daily"],params["shared_features"],include_history=True)
        weekly=assess_series(weeks,params["policy"]["weekly"],params["shared_features"],include_history=True)
        # 日线记录只挂截至当天已完成的最近周线，不能将周五结论提前放到周一。
        weekly_records=weekly["assessments"];j=-1
        for row in daily["assessments"]:
            while j+1<len(weekly_records) and weekly_records[j+1]["as_of"]<=row["as_of"]:
                j+=1
            background=weekly_records[j] if j>=0 else None
            row["weekly_background"]={k:background.get(k) for k in ("as_of","current_trend","candidate_trend","assessment_status")} if background else {
                "as_of":None,"current_trend":None,"candidate_trend":None,"assessment_status":"INSUFFICIENT_DATA"}
        warnings=[]
        cal=_calendar(seed,date.fromisoformat(bars[-1]["date"]))
        if seed<begin:
            expected_seed={s.date().isoformat() for s in cal.sessions_in_range(history_start,(begin-timedelta(days=1)).isoformat())}
            actual_seed={b["date"] for b in bars if b["date"]<start}
            if expected_seed-actual_seed:
                warnings.append({"message":"预热期有未取得的交易日（可能晚于请求起点上市或来源档案不全）；按实际历史计算，长度不足时保留数据不足"})
        if until and bars[-1]["date"]<end:
            cal=_calendar(seed,until)
            sessions=cal.sessions_in_range(start,end)
            expected_end=sessions[-1].date().isoformat() if len(sessions) else None
            if expected_end and bars[-1]["date"]<expected_end:
                warnings.append({"message":"现有日线未覆盖请求终点，只展示实际取得的历史，不预测未取得的日期"})
        daily_visible=[row for row in daily["assessments"] if row["as_of"]>=start]
        weekly_visible=[row for row in weekly_records if row["as_of"]>=start]
        # 无足够周线就是数据不足，不为两个月预热伪造40周背景。
        payload={"algorithm":ALGORITHM,"mode":"timeline","created_at":stamp(),"input_key":key,
            "source_market_id":source.id,"source_market_version":source.fingerprint,"source_fetched_at":source.payload.get("fetched_at"),
            "history_start":history_start,"requested_start":start,"requested_end":end,"actual_history_start":bars[0]["date"],
            "actual_start":daily_visible[0]["as_of"],"actual_end":bars[-1]["date"],"parameters":params,"policy_file":str(chosen_policy),
            "warmup_daily_count":sum(b["date"]<start for b in bars),"warmup_weekly_count":sum(b["date"]<start for b in weeks),
            "daily_intervals":_intervals(daily["assessments"],start),"weekly_intervals":_intervals(weekly_records,start),
            "daily_assessments":daily_visible,"weekly_assessments":weekly_visible,
            "daily_latest":{k:v for k,v in daily.items() if k not in ("assessments","transitions")},
            "weekly_latest":{k:v for k,v in weekly.items() if k not in ("assessments","transitions")},
            "warnings":warnings,"review_status":"draft","research_only":True,"strategy_evaluated":False,
            "message":"历史区间是按当时可用价格逐根判断后的展示；区间末日为事后汇总，不是提前预测转折；最后区间只代表观察截止",
            "replay_note":"仅使用指定历史起点及之后的现有调整价档案；未取得历史供应商版本，不声称是无偏收益回测。"}
        item=save_snapshot(session,security_id,"trend_timeline",payload);item.fingerprint=key
        session.commit()
        return {**serialize_snapshot(item),"cached":False}

"""MACD主趋势状态机：保留原色带，以结构和价格确认管理回踩/反抽。"""
import copy
import math

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from .price_features import confirmed_pivots, wilder_atr

TREND_LABELS={"UP":"上涨","DOWN":"下降"}
PHASE_LABELS={
    "INSUFFICIENT_DATA":"数据不足","WAITING":"方向待确认","ANCHOR_UNAVAILABLE":"缺少有效结构保护位",
    "UP_CONFIRMING":"上涨确认中","DOWN_CONFIRMING":"下降确认中",
    "UP_ACTIVE":"上涨延续","DOWN_ACTIVE":"下降延续",
    "UP_RECOVERED":"上涨恢复","DOWN_RECOVERED":"下行恢复",
    "UP_PULLBACK":"回踩观察","DOWN_PULLBACK":"反抽观察",
    "UP_WEAKENING":"上涨转弱观察","DOWN_WEAKENING":"下降转弱观察",
    "UP_NEUTRAL":"上涨背景，动能等值","DOWN_NEUTRAL":"下降背景，动能等值",
    "UP_INVALIDATED":"上涨失效","DOWN_INVALIDATED":"下降失效",
    "UP_RECLAIM_WAIT":"等待收复原上涨结构","DOWN_RECLAIM_WAIT":"等待跌回原下降结构",
    "REVERSAL_PRICE_WAIT":"反向价格证据待确认"}
REASON_LABELS={
    "INSUFFICIENT_ATR_OR_HISTORY":"完整历史或ATR不足",
    "PROTECTION_BROKEN":"收盘突破原结构失效线",
    "SUSTAINED_OPPOSITE_WITH_PRICE":"反向色带持续加深且价格连续确认",
    "WAITING_FOR_REVERSAL_PRICE_EVIDENCE":"反向价格破坏尚未持续，暂不确认新方向",
    "ANCHOR_TIGHTENED":"新确认结构使保护位收紧",
    "ALIGNED_BAND_AND_INTACT_PROTECTION":"色带同向且结构保护位有效",
    "EQUAL_DIF_DEA":"DIF与DEA等值，方向动能不明确",
    "OPPOSITE_BAND_WITH_INTACT_PROTECTION":"色带反向，原结构保护位尚未破坏",
    "COUNTER_MOVE_ESCALATED":"逆向时间或深度已升级为转弱观察",
    "NO_USABLE_ANCHOR":"缺少可用的结构保护位",
    "BAND_CONFIRMED_WITH_PROTECTION":"同向色带确认完成，已有有效保护位",
    "WAITING_FOR_BAND_CONFIRMATION":"等待连续色带确认",
    "WAITING_FOR_PRICE_RECLAIM":"等待收复原结构及缓冲价位",
    "NO_DIRECTIONAL_EVIDENCE":"没有满足进入条件的方向证据"}


class MacdTrendPolicy(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    enabled: bool
    confirmation_bars: int=Field(ge=1,le=20)
    enter_depth_atr: FiniteFloat=Field(ge=0)
    anchor_lookback: int=Field(ge=10,le=500)
    fallback_lookback: int=Field(ge=2,le=250)
    break_buffer_atr: FiniteFloat=Field(ge=0)
    reclaim_buffer_atr: FiniteFloat=Field(ge=0)
    weakening_bars: int=Field(ge=1,le=50)
    weakening_depth_atr: FiniteFloat=Field(ge=0)
    max_pullback_bars: int=Field(ge=1,le=100)
    reversal_bars: int=Field(ge=2,le=100)
    reversal_depth_atr: FiniteFloat=Field(gt=0)
    price_confirmation_bars: int=Field(ge=1,le=50)
    price_buffer_atr: FiniteFloat=Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.reversal_bars<self.weakening_bars or self.max_pullback_bars<self.weakening_bars:
            raise ValueError("反转及回踩升级根数不能小于转弱观察根数")
        if self.reversal_depth_atr<self.weakening_depth_atr:
            raise ValueError("反转深度门槛不能小于转弱观察门槛")
        if self.price_confirmation_bars>self.reversal_bars:
            raise ValueError("价格确认根数不能大于反转根数")
        if self.fallback_lookback>self.anchor_lookback:
            raise ValueError("结构保护位回看范围不能小于备用窗口")
        return self


def _aligned_bars(bars,price_field):
    """ATR与结构必须和MACD使用同一价格口径，不混用原价与调整价。"""
    result=[]
    for bar in bars:
        values={key:float(bar[("adjusted_" if price_field=="adjusted_close" else "")+key])
            for key in ("open","high","low","close")}
        if any(not math.isfinite(v) or v<=0 for v in values.values()) or not (
            values["low"]<=min(values["open"],values["close"])<=max(values["open"],values["close"])<=values["high"]):
            raise ValueError("主趋势所需OHLC无效或价格口径不一致")
        result.append({"date":bar["date"],"complete":True,"volume":bar.get("volume",0),
            **{"adjusted_"+key:value for key,value in values.items()}})
    return result


def _make_anchor(point,direction,row,atr,cfg,method):
    sign=1 if direction=="UP" else -1
    level=point["price"]-sign*cfg["break_buffer_atr"]*atr
    if not math.isfinite(level) or level<=0 or sign*(row["price"]-point["price"])<0:
        return None
    return {"price":point["price"],"date":point["date"],"confirmed_at":point.get("confirmed_at"),
        "known_at":point.get("confirmed_at") or point["known_at"],"selected_at":row["as_of"],
        "method":method,"kind":"low" if direction=="UP" else "high", "atr_at_set":atr,
        "buffer_atr":cfg["break_buffer_atr"],"break_level":level,"price_field":row["price_field"]}


def _initial_anchor(prefix,points,direction,row,atr,cfg):
    kind="low" if direction=="UP" else "high"
    for point in reversed(points):
        if point["kind"]==kind:
            anchor=_make_anchor(point,direction,row,atr,cfg,"CONFIRMED_PIVOT")
            if anchor:
                return anchor
    # 备用锚点仅使用今天之前的窗口极值；它不是右侧确认拐点，必须明确标识。
    prior=prefix[-cfg["fallback_lookback"]-1:-1]
    if len(prior)<cfg["fallback_lookback"]:
        return None
    chosen=(min if direction=="UP" else max)(prior,key=lambda b:b["adjusted_"+kind])
    return _make_anchor({"price":chosen["adjusted_"+kind],"date":chosen["date"],
        "known_at":prior[-1]["date"]},direction,row,atr,cfg,"PRIOR_WINDOW_EXTREME")


def _tighten_anchor(anchor,points,direction,row,atr,cfg):
    sign=1 if direction=="UP" else -1
    for point in reversed(points):
        if point["kind"]!=anchor["kind"] or point["confirmed_at"]<=anchor["selected_at"] or point["date"]<=anchor["date"]:
            continue
        proposed=_make_anchor(point,direction,row,atr,cfg,"CONFIRMED_PIVOT")
        if proposed and sign*(proposed["price"]-anchor["price"])>0 and sign*(proposed["break_level"]-anchor["break_level"])>0:
            return proposed
    return anchor


def assess_trend(bars,rows,params):
    """逐日更新，不回填回踩结论；保护位和缓冲价冻结，旧点离窗不会删除它。"""
    cfg=params["policy"]["trend"];shared=params["shared_features"]
    features=_aligned_bars(bars,params["policy"]["price_field"])
    if len(rows)!=len(features) or any(row["as_of"]!=bar["date"] or row["price"]!=bar["adjusted_close"]
            for row,bar in zip(rows,features)):
        raise ValueError("MACD记录与结构行情的日期或价格不一致，不能合并主趋势")
    required=max(shared["atr_window"]+1,cfg["fallback_lookback"]+1)
    current=None;anchor=None;since=None;last=None;last_at=None
    run_direction=None;run_count=0;run_since=None
    band_direction=None;band_count=0;band_since=None
    price_runs={"UP":0,"DOWN":0};barriers={};last_invalidation=None;transition_from=None;previous_phase=None
    output=[]
    for i,row in enumerate(rows):
        prefix=features[:i+1];atr=wilder_atr(prefix,shared["atr_window"])
        candidate=None;candidate_count=0;candidate_since=None;invalidated=None;reasons=[]
        gap=row["dif_long"]-row["dea_long"]
        raw_direction="UP" if gap>0 else "DOWN" if gap<0 else None
        ready=i+1>=required and atr is not None and math.isfinite(atr) and atr>0
        points=[];depth=None;opposite_count=0;price_confirmed=False;strong_reversal=False
        if not ready:
            current=None;anchor=None;since=None;run_direction=None;run_count=0;run_since=None
            band_direction=None;band_count=0;band_since=None;price_runs={"UP":0,"DOWN":0}
            barriers={};transition_from=None
            phase="INSUFFICIENT_DATA";status="INSUFFICIENT_DATA";reasons=["INSUFFICIENT_ATR_OR_HISTORY"]
        else:
            depth=abs(gap)/atr
            if raw_direction and raw_direction==band_direction:
                band_count+=1
            else:
                band_direction=raw_direction;band_count=1 if raw_direction else 0;band_since=row["as_of"] if raw_direction else None
            for direction,sign in (("UP",1),("DOWN",-1)):
                supported=(raw_direction==direction and sign*(row["price"]-row["ema_long_slow"])/atr>=cfg["price_buffer_atr"])
                price_runs[direction]=price_runs[direction]+1 if supported else 0
            points=confirmed_pivots(prefix,shared["pivot_left"],shared["pivot_right"],cfg["anchor_lookback"])
            opposite_count=band_count if current and raw_direction and raw_direction!=current else 0
            price_confirmed=bool(raw_direction and price_runs[raw_direction]>=cfg["price_confirmation_bars"])
            strong_reversal=bool(opposite_count>=cfg["reversal_bars"] and depth>=cfg["reversal_depth_atr"] and price_confirmed)
            # 先检查旧保护位，价格破坏不等待色带或确认根数；随后才考虑收紧新锚点。
            sign=1 if current=="UP" else -1
            broken=bool(current and anchor and sign*(row["price"]-anchor["break_level"])<0)
            if current and (broken or strong_reversal):
                invalidated=current
                last_invalidation={"session":row["as_of"],"trend":current,
                    "reason":"PROTECTION_BROKEN" if broken else "SUSTAINED_OPPOSITE_WITH_PRICE",
                    "anchor":copy.deepcopy(anchor),"price":row["price"]}
                transition_from=last_invalidation
                if broken:
                    barriers[current]={"price":anchor["price"]+sign*cfg["reclaim_buffer_atr"]*atr,
                        "set_at":row["as_of"],"original_anchor":copy.deepcopy(anchor)}
                reasons.append(last_invalidation["reason"])
                current=None;anchor=None;since=None
            eligible=raw_direction if raw_direction and depth>=cfg["enter_depth_atr"] else None
            if current is None and eligible and transition_from and eligible!=transition_from["trend"]:
                old_sign=1 if transition_from["trend"]=="UP" else -1
                remains_broken=old_sign*(row["price"]-transition_from["anchor"]["break_level"])<0
                continuing_reversal=band_count>=cfg["reversal_bars"] and depth>=cfg["reversal_depth_atr"] and price_confirmed
                if not remains_broken and not continuing_reversal:
                    eligible=None;reasons.append("WAITING_FOR_REVERSAL_PRICE_EVIDENCE")
            barrier=barriers.get(eligible)
            if barrier:
                sign=1 if eligible=="UP" else -1
                if sign*(row["price"]-barrier["price"])<0:
                    eligible=None
            if eligible and eligible==run_direction:
                run_count+=1
            else:
                run_direction=eligible;run_count=1 if eligible else 0;run_since=row["as_of"] if eligible else None
            if current:
                if raw_direction==current:
                    updated=_tighten_anchor(anchor,points,current,row,atr,cfg)
                    if updated is not anchor:
                        reasons.append("ANCHOR_TIGHTENED")
                    anchor=updated
                    phase=current+("_RECOVERED" if previous_phase in (current+"_PULLBACK",current+"_WEAKENING",current+"_NEUTRAL") else "_ACTIVE")
                    status="CONFIRMED";last_at=row["as_of"];reasons.append("ALIGNED_BAND_AND_INTACT_PROTECTION")
                elif raw_direction is None:
                    phase=current+"_NEUTRAL";status="CHALLENGED";reasons.append("EQUAL_DIF_DEA")
                else:
                    weak=(opposite_count>=cfg["weakening_bars"] and depth>=cfg["weakening_depth_atr"]) or opposite_count>=cfg["max_pullback_bars"]
                    phase=current+("_WEAKENING" if weak else "_PULLBACK");status="CHALLENGED"
                    reasons.append("OPPOSITE_BAND_WITH_INTACT_PROTECTION")
                    if weak:
                        reasons.append("COUNTER_MOVE_ESCALATED")
            else:
                phase=invalidated+"_INVALIDATED" if invalidated else "WAITING";status="PENDING"
                if eligible:
                    candidate=eligible;candidate_count=run_count;candidate_since=run_since
                    proposed=_initial_anchor(prefix,points,candidate,row,atr,cfg)
                    if not proposed:
                        phase="ANCHOR_UNAVAILABLE";reasons.append("NO_USABLE_ANCHOR")
                    elif run_count>=cfg["confirmation_bars"]:
                        current=candidate;anchor=proposed;since=row["as_of"];last=current;last_at=row["as_of"]
                        phase=current+"_ACTIVE";status="CONFIRMED";barriers={};transition_from=None
                        reasons.append("BAND_CONFIRMED_WITH_PROTECTION")
                        candidate=None;candidate_count=0;candidate_since=None
                    else:
                        phase=candidate+"_CONFIRMING";reasons.append("WAITING_FOR_BAND_CONFIRMATION")
                elif raw_direction in barriers:
                    phase=raw_direction+"_RECLAIM_WAIT";reasons.append("WAITING_FOR_PRICE_RECLAIM")
                elif "WAITING_FOR_REVERSAL_PRICE_EVIDENCE" in reasons:
                    phase="REVERSAL_PRICE_WAIT"
                else:
                    reasons.append("NO_DIRECTIONAL_EVIDENCE")
        previous_phase=phase
        state={"current_trend":current,"trend_label":TREND_LABELS.get(current,"无有效方向"),
            "assessment_status":status,"phase":phase,"phase_label":PHASE_LABELS[phase],
            "confirmed_since":since,"last_confirmed_trend":last,"last_confirmed_at":last_at,
            "candidate_trend":candidate,"candidate_count":candidate_count,"candidate_since":candidate_since,
            "confirmation_required":cfg["confirmation_bars"],"anchor":copy.deepcopy(anchor),
            "atr":atr,"required_history_bars":required,"band_direction":raw_direction,"band_run":band_count,
            "band_since":band_since,
            "counter_band_bars":band_count if current and raw_direction and raw_direction!=current else 0,
            "counter_bars_before_decision":opposite_count,"gap_depth_atr":depth,
            "price_confirmation_counts":dict(price_runs),"price_confirmation_passed":price_confirmed,
            "strong_reversal_evidence":strong_reversal,"invalidated_trend":invalidated,
            "last_invalidation":copy.deepcopy(last_invalidation),"reclaim_barriers":copy.deepcopy(barriers),
            "pending_invalidation":copy.deepcopy(transition_from),
            "reason_codes":reasons,"new_risk_allowed":False,"strategy_evaluated":False}
        output.append(state)
    return output


def trend_intervals(rows,start):
    """先形成完整历史状态，再裁剪展示；不将后来的恢复结论回填为已知回踩。"""
    groups=[]
    for row in rows:
        state=row["trend"]
        signature=(state["current_trend"],state["assessment_status"],state["phase"],state["candidate_trend"])
        if groups and groups[-1]["signature"]==signature:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"signature":signature,"rows":[row]})
    result=[]
    for i,group in enumerate(groups):
        visible=[r for r in group["rows"] if r["as_of"]>=start]
        if not visible:
            continue
        first,last=visible[0],visible[-1];a,b=first["trend"],last["trend"]
        result.append({"start_session":first["as_of"],"end_session":last["as_of"],"bar_count":len(visible),
            "current_trend":a["current_trend"],"trend_label":a["trend_label"],"assessment_status":a["assessment_status"],
            "phase":a["phase"],"phase_label":a["phase_label"],"candidate_trend":a["candidate_trend"],
            "observed_state_started":group["rows"][0]["as_of"],"continues_from_warmup":group["rows"][0]["as_of"]<start,
            "start_anchor":a["anchor"],"end_anchor":b["anchor"],"start_reason_codes":a["reason_codes"],
            "end_reason_codes":b["reason_codes"],"end_is_observation_cutoff":i==len(groups)-1})
    return result

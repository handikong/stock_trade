"""大盘日线与已完成周线的证据计算，不读取个股策略或模型。"""
from datetime import date, timedelta
from statistics import median

from .config import settings
from .futu_macd import _ema
from .price_features import confirmed_pivots, wilder_atr
from .trend import completed_weeks

BENCHMARKS = ("SPY", "QQQ", "RSP")
ATR_PERIOD = 14


def parameters():
    return {"algorithm":"market-conditions-v1","ema":[20,50],"slope_sessions":settings.market_brief_slope_sessions,
            "volume_high":settings.market_brief_volume_high,"volume_low":settings.market_brief_volume_low,
            "level_buffer_atr":settings.market_brief_level_buffer_atr,"atr_period":ATR_PERIOD}


def _trend(closes):
    fast, slow = (_ema(closes,period) for period in (20,50))
    prior = fast[-1-settings.market_brief_slope_sessions]
    direction = ("偏强" if closes[-1]>fast[-1]>prior and closes[-1]>slow[-1] else
                 "偏弱" if closes[-1]<fast[-1]<prior and closes[-1]<slow[-1] else "过渡")
    return direction, {"EMA20":fast[-1],"EMA50":slow[-1]}


def _references(bars, averages, atr):
    width = atr * settings.market_brief_level_buffer_atr
    references = [(name,value-width,value+width,0) for name,value in averages.items()]
    pivots = confirmed_pivots(bars,3,3,80)
    for kind, name, line in (("high","前高","下降趋势线"),("low","前低","上升趋势线")):
        points = [p for p in pivots if p["kind"]==kind]
        if not points:
            continue
        last = points[-1]
        references.append((name,last["price"]-width,last["price"]+width,0))
        earlier = next((p for p in reversed(points[:-1]) if last["index"]-p["index"]>=5),None)
        if earlier is None:
            continue
        slope = (last["price"]-earlier["price"])/(last["index"]-earlier["index"])
        if (kind=="high" and slope<0) or (kind=="low" and slope>0):
            projected = last["price"] + slope*(len(bars)-1-last["index"])
            if projected+min(0,slope)-width>0:
                references.append((line,projected-width,projected+width,slope))
    # 区间沿用研究模块的复权价尺度，报告才换成对应交易日的报价尺度。
    from .technical import structures
    for zone in structures(bars)["zones"][:6]:
        references.append(("历史结构区间",zone["low"],zone["high"],0))
    return references


def _volume(bars, index, shortened):
    if bars[index]["date"] in shortened:
        return None
    normal = [b for b in bars[max(0,index-30):index] if b["date"] not in shortened][-20:]
    previous = [float(b.get("volume") or 0) for b in normal]
    current = float(bars[index].get("volume") or 0)
    return current/(sum(previous)/20) if current>0 and len(previous)==20 and min(previous)>0 else None


def _event(bars, index, references, atr, shortened):
    previous = float(bars[index-1]["adjusted_close"])
    bar = bars[index]
    low, high, close = (float(bar[f"adjusted_{key}"]) for key in ("low","high","close"))
    volume = _volume(bars,index,shortened)
    location = (close-low)/(high-low) if high>low else None
    possible = []
    for label, old_low, old_high, slope in references:
        lower, upper = old_low+slope, old_high+slope
        if previous<=old_high and close>upper:
            kind = "突破"
        elif previous>=old_low and close<lower:
            kind = "破位"
        elif previous>=old_low and low<=upper and close>=upper:
            kind = "回踩守住"
        elif previous<=old_high and high>=lower and close<=lower:
            kind = "反抽受阻"
        else:
            continue
        priority = (0 if kind in ("突破","破位") else 1, abs(previous-(old_low+old_high)/2))
        possible.append((priority,label,lower,upper,kind))
    if not possible:
        return None
    _, label, lower, upper, kind = min(possible,key=lambda x:x[0])
    later = bars[index+1:]
    if kind in ("突破","破位"):
        upside = kind=="突破"
        closes = [float(row["adjusted_close"]) for row in later]
        failed = any(v<lower if upside else v>upper for v in closes)
        retest = any(float(row["adjusted_low"])<=upper<=float(row["adjusted_close"]) if upside else
                     float(row["adjusted_high"])>=lower>=float(row["adjusted_close"]) for row in later)
        held = bool(closes) and all(v>upper if upside else v<lower for v in closes)
        state = ("已失效" if failed else ("回踩确认" if upside else "反抽确认") if retest else
                 "连续收盘确认" if held else "待确认")
    else:
        upside = kind=="回踩守住"
        failed = any(float(row["adjusted_close"])<lower if upside else float(row["adjusted_close"])>upper for row in later)
        state = "已失效" if failed else "当日守住" if upside else "当日受阻"
    factor = close/float(bar["close"])
    latest = float(bars[-1]["adjusted_close"])
    return {"date":bar["date"],"level":label,"low":round(lower/factor,2),"high":round(upper/factor,2),
            "kind":kind,"state":state,"volume_ratio":round(volume,2) if volume is not None else None,
            "close_location":round(location,2) if location is not None else None,
            "extension_atr":round(max(0,latest-upper if upside else lower-latest)/atr,2)}


def _zone(references, close, factor, *, support):
    choices = [(name,low,high) for name,low,high,_ in references
               if (((low+high)/2<=close)==support)]
    if not choices:
        return None
    name, low, high = min(choices,key=lambda row:abs(close-(row[1]+row[2])/2))
    return {"name":name,"low":round(low/factor,2),"high":round(high/factor,2)}


def _watch(events, support, resistance):
    latest = events[-1] if events else None
    if latest and latest["state"]!="已失效":
        kind = latest["kind"]
        band = f"{latest['low']:.2f}–{latest['high']:.2f}"
        volume = latest["volume_ratio"] or 0
        location = latest["close_location"]
        if kind=="突破":
            if latest["extension_atr"]>=1:
                action = "收盘已拉离突破位，等回踩"
            elif latest["state"]=="待确认":
                supporting = volume>=settings.market_brief_volume_high and location is not None and location>=0.65
                action = "量价支持突破，等待回踩或后续收盘确认" if supporting else "量价确认不足，等待回踩守住"
            else:
                action = "守住突破区间时关注加仓条件"
            return f"{band} {action}；完整收盘跌破下沿，重新评估突破"
        if kind=="破位":
            qualifier = "放量破位，关注下探风险；" if volume>=settings.market_brief_volume_high else ""
            return f"{band} {qualifier}先等价格收回区间上沿；反抽受阻时继续观察下档支撑"
        if kind=="回踩守住":
            stable = 0<volume<=settings.market_brief_volume_low and location is not None and location>=0.55
            action = "缩量收回，次日守住时观察试仓条件" if stable else "观察下一交易日能否继续守住支撑"
            return f"{band} {action}；收盘跌破下沿则撤销"
    if support and resistance:
        return (f"盯支撑下沿 {support['low']:.2f} 与压力上沿 {resistance['high']:.2f}；"
                "完整收盘越过边界再调整计划")
    return "等待关键区间及收盘信号进一步确认"


def benchmark(bars, *, weekly, shortened):
    """先用当时已经确认的区间识别越线，再从此后的完整日线判断确认或失效。"""
    if len(bars)<60:
        raise ValueError("有效日线不足60根")
    closes = [float(b["adjusted_close"]) for b in bars]
    close, prior = closes[-1], closes[-2]
    factor = close/float(bars[-1]["close"])
    atr = wilder_atr(bars,ATR_PERIOD)
    if not atr or atr<=0:
        raise ValueError("ATR 无效")
    direction, averages = _trend(closes)
    previous, _ = _trend(closes[:-1])
    week_bars = completed_weeks(bars)
    week_direction = "待补"
    if len(week_bars)>=20:
        weekly_closes = [float(b["adjusted_close"]) for b in week_bars]
        ema = _ema(weekly_closes,20)
        week_direction = ("偏强" if weekly_closes[-1]>ema[-1]>ema[-4] else
                          "偏弱" if weekly_closes[-1]<ema[-1]<ema[-4] else "过渡")
    refs = _references(bars,averages,atr)
    day = date.fromisoformat(bars[-1]["date"])
    week_start = (day-timedelta(days=day.weekday())).isoformat()
    week_data = [i for i,b in enumerate(bars) if b["date"]>=week_start]
    candidates = week_data if weekly else range(len(bars)-3,len(bars))
    events = []
    for index in candidates:
        history = bars[:index]
        _, basis = _trend(closes[:index])
        scale = wilder_atr(history,ATR_PERIOD)
        if scale and (event:=_event(bars,index,_references(history,basis,scale),scale,shortened)):
            events.append(event)
    volume = _volume(bars,len(bars)-1,shortened)
    support, resistance = (_zone(refs,close,factor,support=s) for s in (True,False))
    output = {"trend":direction,"prior_trend":previous,"weekly_trend":week_direction,
              "weekly_as_of":week_bars[-1]["date"] if week_bars else None,
              "close":round(close/factor,2),"daily_pct":round((close/prior-1)*100,2),
              "ema":{k:round(v/factor,2) for k,v in averages.items()},
              "atr":round(atr/factor,2),"atr_pct":round(atr/close*100,2),
              "volume_ratio":round(volume,2) if volume is not None else None,
              "volume_state":"缩短交易日" if bars[-1]["date"] in shortened else
                             "放量" if volume and volume>=settings.market_brief_volume_high else
                             "缩量" if volume and volume<=settings.market_brief_volume_low else
                             "量能平常" if volume else "量能缺失",
              "support":support,"resistance":resistance,"events":events,
              "watch":_watch(events,support,resistance)}
    if weekly and week_bars and week_bars[-1]["date"]==bars[-1]["date"]:
        week = week_bars[-1]
        output["week"] = {"start":week["week_start"],"end":week["date"],"sessions":len(week["source_sessions"]),
                          "return_pct":round((close/float(week_bars[-2]["adjusted_close"])-1)*100,2) if len(week_bars)>1 else None,
                          "low":round(week["adjusted_low"]/factor,2),"high":round(week["adjusted_high"]/factor,2)}
    return output


def breadth(spy, rsp):
    if not spy or not rsp or len(spy)<6 or len(rsp)<6 or [b["date"] for b in spy[-6:]] != [b["date"] for b in rsp[-6:]]:
        return None
    spy_move = float(spy[-1]["adjusted_close"])/float(spy[-6]["adjusted_close"])
    rsp_move = float(rsp[-1]["adjusted_close"])/float(rsp[-6]["adjusted_close"])
    change = (rsp_move/spy_move-1)*100
    return {"ratio_change_5d_pct":round(change,2),"state":"等权领先" if change>0.3 else "等权落后" if change<-0.3 else "等权同步"}


def volatility(rows, sessions):
    if len(sessions)<21 or any(day not in rows for day in sessions[-21:]):
        return None
    values = [rows[day] for day in sessions[-21:]]
    change = (values[-1]/values[-2]-1)*100
    baseline = median(values[:-1])
    return {"close":values[-1],"daily_pct":round(change,2),"median_20":round(baseline,2),
            "elevated":change>=10 or values[-1]>=baseline*1.2}


def assessment(benchmarks, participation, vix, events):
    if set(benchmarks)!={"SPY","QQQ"}:
        return {"state":"资料待补","plan":"等待两只指数代理的完整日线后再确认下一交易日计划"}
    spy, qqq = (benchmarks[k]["trend"] for k in ("SPY","QQQ"))
    state = spy if spy==qqq and spy!="过渡" else "分化或过渡"
    cautious = (vix and vix["elevated"]) or (participation and participation["state"]=="等权落后")
    plan = ("等关键支撑企稳或收回后再评估，谨慎看待左侧机会" if state=="偏弱" else
            "观察突破位的回踩和收盘确认，远离计划位时等待" if cautious or state!="偏强" else
            "优先观察守住支撑的回踩，突破延伸后等待重新靠近关键位")
    if events:
        plan += "；临近重要发布时，等公布后重新核对价格结构"
    return {"state":state,"plan":plan}

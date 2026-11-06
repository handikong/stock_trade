"""第一模块的技术区间；基础价格特征与第二模块趋势判断共用。"""
import math
import json
from pathlib import Path
from .config import settings
from .price_features import confirmed_pivots, wilder_atr

ALGORITHM="confirmed-pivots-wilder-atr-strength-v2"


def parameters():
    strength=json.loads(Path(settings.technical_strength_file).expanduser().read_text())
    if not isinstance(strength,dict):
        raise ValueError("技术强弱配置必须为JSON对象")
    for key in ("medium_min_score","strong_min_score","strong_min_touches","strong_max_age_sessions","role_min_tests"):
        if type(strength.get(key)) is not int or strength[key]<=0:
            raise ValueError(f"技术强弱配置{key}需要正整数")
    ratio=strength.get("strong_min_volume_ratio")
    if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not math.isfinite(ratio) or ratio<=0:
        raise ValueError("强结构量能比阈值需要有效正数")
    if not strength["medium_min_score"]<strength["strong_min_score"]<=80:
        raise ValueError("强结构分数应大于一般结构分数，且不超过当前评分上限80")
    return {"left":settings.pivot_left,"right":settings.pivot_right,"atr_window":settings.atr_window,
        "tolerance_atr":settings.zone_tolerance_atr,"lookback":settings.zone_lookback,"strength":strength}


def is_current(view):
    try:
        return view.get("algorithm")==ALGORITHM and view.get("parameters")==parameters()
    except (ValueError,OSError):
        return False


def strength_details(zone, closes, bars, cfg):
    """在结构评分之外检查当前角色及独立证据，不将历史压力直接升级为强支撑。"""
    reasons=[f"{zone['touches']}次独立结构接触，结构评分{zone['score']}",
        "均线重合："+("、".join(zone["moving_average_confluence"]) or "无")]
    against=[]
    ratio=zone["pivot_volume_ratio"]
    reasons.append(f"拐点平均量/当前20日均量={ratio:.2f}" if ratio is not None else "成交量参照缺失")
    if zone["kind"]=="inside":
        return {"strength":"undetermined","strength_label":"现价区间，方向待确认","strength_reasons":reasons,
            "weakness_reasons":["最新完整收盘价仍在区间内部，不能同时认定为支撑或压力"],"role_status":"inside","role_tests":0}
    support=zone["kind"]=="support"
    boundary=zone["high"] if support else zone["low"]
    crosses=[i for i in range(1,len(closes)) if
        (closes[i-1]<=boundary<closes[i] if support else closes[i-1]>=boundary>closes[i])]
    last_cross=crosses[-1] if crosses else -1
    role="low" if support else "high"
    tests=[p for p in zone["sources"] if p["kind"]==role and p["index"]>=last_cross]
    title="支撑" if support else "压力"
    if len(tests)<cfg["role_min_tests"]:
        against.append(f"当前{title}角色尚缺已确认的{'回踩低点' if support else '反抽高点'}，不把角色转换直接当作有效{title}")
        return {"strength":"unconfirmed","strength_label":f"待确认{title}","strength_reasons":reasons,
            "weakness_reasons":against,"role_status":"pending_retest","role_tests":len(tests),
            "role_changed_at":bars[last_cross]["date"] if last_cross>=0 else None}
    last=max(tests,key=lambda p:p["index"])
    # 时效以右侧确认日计算；所有候选点均已具备右侧收盘K线。
    age=len(bars)-1-(last["index"]+settings.pivot_right)
    reasons.append(f"当前角色有{len(tests)}个独立确认点，最近确认于{last['confirmed_at']}（距今{age}个交易日）")
    recent=age<=cfg["strong_max_age_sessions"]
    independent=zone["touches"]>=cfg["strong_min_touches"]
    extra=bool(zone["moving_average_confluence"]) or (ratio is not None and ratio>=cfg["strong_min_volume_ratio"])
    if not recent:
        against.append("最近角色确认较久，强度受时效限制")
    if not independent:
        against.append("独立结构接触次数不足强结构门槛")
    if not extra:
        against.append("缺少均线重合或达到阈值的量能辅助证据")
    level="strong" if zone["score"]>=cfg["strong_min_score"] and recent and independent and extra else "medium" if zone["score"]>=cfg["medium_min_score"] else "weak"
    return {"strength":level,"strength_label":{"strong":"强","medium":"一般","weak":"弱"}[level]+title,
        "strength_reasons":reasons,"weakness_reasons":against,"role_status":"confirmed","role_tests":len(tests),
        "last_role_confirmed_at":last["confirmed_at"],"role_confirmation_age_sessions":age,
        "role_changed_at":bars[last_cross]["date"] if last_cross>=0 else None}


def structures(bars: list[dict]) -> dict:
    params=parameters()
    metadata={"algorithm":ALGORITHM,"parameters":params}
    bars = [b for b in bars if b.get("complete") and b.get("adjusted_close") is not None]
    if len(bars) < max(settings.atr_window + 1, settings.pivot_left + settings.pivot_right + 1):
        return {**metadata,"status": "insufficient", "message": "完整历史不足，暂时无法形成可靠区间", "zones": [], "averages": {}}
    c = [float(b["adjusted_close"]) for b in bars]
    atr = wilder_atr(bars,settings.atr_window)
    averages = {str(w): sum(c[-w:])/w if len(c) >= w else None for w in settings.averages}
    if not math.isfinite(atr) or atr <= 0:
        return {**metadata,"status": "unknown", "message": "波动尺度无效，不能计算结构区间", "zones": [], "averages": averages}
    left, right = settings.pivot_left, settings.pivot_right
    points = confirmed_pivots(bars,left,right,settings.zone_lookback)
    tolerance = atr * settings.zone_tolerance_atr
    groups = []
    for point in sorted(points, key=lambda x: x["price"]):
        # 限制整个簇的跨度，避免链式合并把相距很远的价位接起来。
        if groups and point["price"]-groups[-1][0]["price"] <= 2*tolerance:
            groups[-1].append(point)
        else:
            groups.append([point])
    zones = []
    for group in groups:
        if len(group) < 2:
            continue
        independent = []
        for point in sorted(group, key=lambda x: x["index"]):
            if not independent or point["index"]-independent[-1]["index"] >= left+right+1:
                independent.append(point)
        if len(independent) < 2:
            continue
        center = sum(p["price"] for p in group)/len(group)
        low = min(p["price"] for p in group)-tolerance/2
        high = max(p["price"] for p in group)+tolerance/2
        near_ma = [w for w, value in averages.items() if value is not None and low <= value <= high]
        volume_avg = sum(float(b.get("volume", 0)) for b in bars[-20:])/min(20,len(bars))
        volume_ratio = sum(float(p["volume"]) for p in independent)/len(independent)/volume_avg if volume_avg else None
        score = min(5,len(independent))*10 + min(3,len(near_ma))*5
        last = max(group, key=lambda x:x["index"])
        score += max(0, 15-(len(bars)-1-last["index"])/10)
        kind = "support" if high < c[-1] else "resistance" if low > c[-1] else "inside"
        zone={"low": low, "high": high, "center": center, "kind": kind,
            "distance_pct": (center/c[-1]-1)*100, "touches": len(independent),
            "score": round(score,1), "moving_average_confluence": near_ma,
            "pivot_volume_ratio": volume_ratio, "last_touch": last["date"],
            "sources": independent, "invalidation": "完整日线越过区间及波动缓冲后复查；不是保证成交价"}
        zone.update(strength_details(zone,c,bars,params["strength"]))
        zones.append(zone)
    zones.sort(key=lambda z: (abs(z["distance_pct"]), -z["score"]))
    return {"status": "ready" if zones else "unknown", "as_of": bars[-1]["date"], "atr": atr,
        "averages": averages, "zones": zones[:12], "price_basis": "供应商Adj Close等比调整OHLC",
        **metadata,
        "message": "结构评分表示证据强度，不代表反弹成功概率；日线成交量不等于分价筹码"}

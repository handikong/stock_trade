"""确定性的盘中结构判断，复用已知日线价位和账户约束。"""
from datetime import datetime, timedelta
from decimal import Decimal

from . import holdings_brief, market, market_conditions, technical
from .portfolio import _check, _context
from .store import PositionPlan, PositionProtection, Session, now
from .workspace import position_lock

BENCHMARKS = ("QQQ", "SPY")
D = Decimal
PIVOTS = {"前高", "前低", "上升趋势线", "下降趋势线"}


def split(bar):
    return bar.get("split") not in (None, "0", "0.0", 0)


def references(session, position, asset, target, trading_day):
    bars = asset["bars"]
    result = {"previous_close": asset["price"], "support": None, "resistance": None, "levels": {},
              "atr": asset["atr"], "stops": [], "trend": None}
    if bars and asset["atr"]:
        view = asset["technical"]
        if view.get("as_of") != target or not technical.is_current(view):
            view = market.technical_view(bars)
        for label, kind in (("support", "support"), ("resistance", "resistance")):
            zone = holdings_brief._zone(view, kind, confirmed_only=True)
            if zone:
                result[label] = (D(str(zone["low"])), D(str(zone["high"])))
        if len(bars) >= 60:
            close = [float(b["adjusted_close"]) for b in bars]
            trend, averages = market_conditions._trend(close)
            factor = asset["factor"]
            result["trend"] = trend
            result["levels"]["EMA20"] = D(str(averages["EMA20"])) / factor
            for name, low, high, slope in market_conditions._references(bars, averages, float(asset["atr"]*factor)):
                if name in PIVOTS:
                    level = D(str((low + high) / 2 + slope)) / factor
                    if level > 0:
                        result["levels"][name] = level
    if position is None:
        return result
    protection = session.get(PositionProtection, position.id)
    remaining = D(position.quantity)
    for lot in (protection.payload or {}).get("lots", []) if protection else []:
        day = lot["session"]
        allocated = min(remaining, D(lot["quantity"]))
        remaining -= allocated
        if allocated <= 0 or day > trading_day:
            continue
        comparable = day == trading_day or (bars and day >= bars[0]["date"] and
                     not any(split(b) for b in bars if b["date"] >= day))
        if comparable and D(lot["stop"]) > 0:
            result["stops"].append(D(lot["stop"]))
    result["stops"] = sorted(set(result["stops"]), reverse=True)
    return result


def fresh(sample, current, max_age, opened, closed):
    if not sample:
        return False
    at = datetime.fromisoformat(sample["at"])
    return opened <= at < closed and -2 <= (current-at).total_seconds() <= max_age


def usable_prices(frame, refs, current, policy, opened, closed):
    prices = {}
    scale_review = set()
    if not frame["ready"]:
        return prices, scale_review
    for symbol, row in frame["symbols"].items():
        last = row.get("last")
        if fresh(last, current, policy.quote_max_age_seconds, opened, closed):
            price = D(str(last["price"]))
            prices[symbol] = price
            baseline = refs.get(symbol, {}).get("previous_close")
            if baseline and abs(price / baseline - 1) >= policy.scale_review_fraction:
                scale_review.add(symbol)
    return prices, scale_review


def _number(value):
    return D(str(value))


def market_mode(frame, refs, prices):
    if any(code not in prices for code in BENCHMARKS):
        return "待确认"
    weak, firm = 0, 0
    for code in BENCHMARKS:
        row = frame["symbols"][code]
        if not (row.get("latest") and row.get("vwap") and row.get("opening_low")):
            return "待确认"
        close, vwap = _number(row["latest"]["c"]), _number(row["vwap"])
        opening_low = _number(row["opening_low"])
        support = refs[code]["support"]
        lower = max(opening_low, support[0]) if support else opening_low
        weak += close < min(vwap, lower) and prices[code] < lower
        firm += close > vwap and prices[code] > vwap
    if weak == 2:
        return "同步走弱"
    return "双双站上 VWAP" if firm == 2 else "分化"


def relative_return(code, refs, prices):
    if code not in prices or not refs.get(code, {}).get("previous_close"):
        return None
    return prices[code] / refs[code]["previous_close"] - 1


def _level_groups(levels, buffer):
    groups = []
    for name, level in sorted(levels, key=lambda entry: entry[1], reverse=True):
        if not groups or groups[-1][0][1] - level > buffer:
            groups.append([])
        groups[-1].append((name, level))
    return groups


def break_state(row, ref, price, policy):
    baseline = ref["previous_close"]
    if not ref["atr"] or baseline is None:
        return None
    buffer = ref["atr"] * policy.buffer_daily_atr
    levels = [(name, level) for name, level in ref["levels"].items()
              if name in {"前低", "上升趋势线", "EMA20"}]
    support = ref["support"]
    if support:
        levels.insert(0, ("日线支撑下沿", support[0]))
    latest, previous = row.get("latest"), row.get("previous")
    groups = _level_groups([(name, level) for name, level in levels if baseline >= level - buffer], buffer)
    states = []
    for group in groups:
        crossed = [(name, level) for name, level in group if price < level - buffer]
        if not crossed:
            continue
        # 两根收线都必须跌破同一结构位。单独跌破均线或趋势线只预警。
        confirmed = bool(latest and previous and any(
            name in {"日线支撑下沿", "前低"} and max(_number(latest["c"]), _number(previous["c"])) < level - buffer
            for name, level in crossed))
        states.append((confirmed, crossed, group[0]))
    return max(states, key=lambda state: (state[0], state[2][1]), default=None)


def _breakout_retest(latest, previous, price, vwap, level, buffer):
    return (_number(previous["c"]) > level + buffer and abs(_number(latest["l"]) - level) <= buffer
            and _number(latest["c"]) > level and price >= level and price > _number(vwap))


def structure_state(row, ref, price, policy, profit_target=None):
    latest, previous = row.get("latest"), row.get("previous")
    baseline = ref["previous_close"]
    if not (latest and previous and ref["atr"] and baseline):
        return None
    buffer = ref["atr"] * policy.buffer_daily_atr
    close = _number(latest["c"])
    resistance = ref["resistance"]
    if resistance and profit_target is not None:
        lower = resistance[0]
        high = max(_number(latest["h"]), _number(previous["h"]))
        if high >= max(lower, profit_target) and max(close, price) < lower - buffer:
            return "profit", [("压力下沿", lower), ("登记止盈价", profit_target)], ("登记止盈价", profit_target)

    vwap = row.get("vwap")
    upper = ("压力上沿", resistance[1]) if resistance else ("前高", ref["levels"].get("前高"))
    level = upper[1]
    if vwap and level is not None and baseline <= level and _breakout_retest(latest, previous, price, vwap, level, buffer):
        return "breakout", [upper], upper

    support = ref["support"]
    support_levels = [("支撑上沿", support[1])] if support else [("前低", ref["levels"].get("前低"))]
    support_levels.append(("EMA20", ref["levels"].get("EMA20")))
    groups = _level_groups([(name, level) for name, level in support_levels
                            if level is not None and baseline >= level - buffer], buffer)
    prior_close = _number(previous["c"])
    low, prior_low = _number(latest["l"]), _number(previous["l"])
    for group in groups:
        recovered = []
        for name, level in group:
            lower = support[0] if name == "支撑上沿" else level
            if (lower - buffer <= min(low, prior_low) <= level and close > prior_close
                    and (low <= level or prior_close <= level + buffer) and min(close, price) >= level + buffer):
                recovered.append((name, level))
        if recovered:
            return "reclaim", recovered, group[0]
    return None


def add_stage(row, ref, price, stage, mode, policy):
    if mode in ("待确认", "同步走弱") or not ref["atr"]:
        return False
    latest, previous = row.get("latest"), row.get("previous")
    if not (latest and previous and row.get("vwap")):
        return False
    trigger, stop = D(stage["trigger"]), D(stage["stop"])
    close, low = _number(latest["c"]), _number(latest["l"])
    if not (D(stage["min_price"]) <= price <= D(stage["max_price"]) and price > stop and
            close >= trigger and price >= trigger):
        return False
    if stage["id"] == "left":
        touched = min(low, _number(previous["l"]))
        return stop < touched <= trigger and close > _number(latest["o"]) and close > _number(previous["c"])
    buffer = ref["atr"] * policy.buffer_daily_atr
    return _breakout_retest(latest, previous, price, row["vwap"], trigger, buffer)


def review_add(candidate, prices):
    with position_lock(), Session() as session:
        plan = session.get(PositionPlan, candidate["plan_id"])
        if plan is None or plan.version != candidate["plan_version"] or plan.status != "active":
            return None
        ctx = _context(session, plan.symbol, live_prices=prices)
        view = next((p for p in ctx["plans"] if p["id"] == plan.id and p["status"] == "active"), None)
        if view is None:
            return None
        stage = next((s for s in view["stages"] if s["id"] == candidate["stage"] and s["remaining"] > 0), None)
        if stage is None or not _check(session, ctx, view, stage, intraday=True)["ready"]:
            return None
        return stage

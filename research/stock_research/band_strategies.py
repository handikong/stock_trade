"""独立输出EMA回踩与MACD图标1买入提示，各策略条件分别计算。"""
import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ROOT
from .macd_arrow import MacdArrowPolicy, calculate as calculate_macd_arrow
from .price_features import wilder_atr
from .strategy_exits import ExitPolicy, attach_exits
from .store import Session, Snapshot, digest

ALGORITHM = "multi-strategy-ema-v5-macd-icon1-common-exits-v2"
POLICY_FILE = ROOT / "config" / "band-strategies.json"
STRATEGIES = {
    "EMA_PULLBACK": ("高点回踩买点", None, "pullback"),
    "GREEN_BREAKOUT": ("绿带突破买点", "GREEN", "breakout"),
    "RED_REBOUND": ("红带止跌反弹买点", "RED", "rebound"),
}
STATUS_LABELS = {
    "SIGNAL": "新买点", "HOLD": "该机会已触发，非新买点", "WAIT": "未命中",
    "UNKNOWN": "数据不足", "NOT_APPLICABLE": "当前色带不适用", "DISABLED": "已关闭",
}


class StrictPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PullbackPolicy(StrictPolicy):
    enabled: bool
    ema_period: int = Field(ge=2, le=250)
    peak_lookback: int = Field(ge=2, le=120)
    rise_lookback: int = Field(ge=2, le=120)
    min_rise_atr: float = Field(gt=0, le=30)
    min_peak_distance_atr: float = Field(gt=0, le=20)
    touch_tolerance_atr: float = Field(ge=0, le=5)
    max_consecutive_red_bars: int = Field(default=5, ge=0, le=120)


class BreakoutPolicy(StrictPolicy):
    enabled: bool
    lookback: int = Field(ge=2, le=250)
    volume_lookback: int = Field(ge=2, le=250)
    min_volume_ratio: float = Field(gt=0, le=10)
    max_distance_level_atr: float = Field(gt=0, le=10)
    max_distance_slow_atr: float = Field(gt=0, le=20)
    stop_below_level_atr: float = Field(gt=0, le=10)


class ReboundPolicy(StrictPolicy):
    enabled: bool
    support_lookback: int = Field(ge=2, le=250)
    stabilization_bars: int = Field(ge=2, le=30)
    momentum_bars: int = Field(ge=1, le=10)
    near_support_atr: float = Field(ge=0, le=10)
    max_distance_trigger_atr: float = Field(gt=0, le=10)
    min_reward_risk: float = Field(gt=0, le=20)
    max_holding_calendar_months: int = Field(ge=1, le=12)


class StrategyPolicy(StrictPolicy):
    version: str
    note: str
    stop_buffer_atr: float = Field(gt=0, le=10)
    entry_buffer_atr: float = Field(ge=0, le=5)
    max_risk_atr: float = Field(gt=0, le=20)
    pullback: PullbackPolicy
    breakout: BreakoutPolicy
    rebound: ReboundPolicy
    macd_arrow: MacdArrowPolicy = Field(default_factory=MacdArrowPolicy)
    exits: ExitPolicy = Field(default_factory=ExitPolicy)


def policy(policy_file=None):
    chosen = Path(policy_file or POLICY_FILE).expanduser()
    try:
        return StrategyPolicy.model_validate(json.loads(chosen.read_text()))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"买点策略配置无效，请检查{chosen.name}的字段、窗口和阈值") from exc


def _check(code, label, passed, actual=None, required=None):
    # None表示该项缺数据；不能用0或False伪装成已获得可靠判断。
    return {"code": code, "label": label, "passed": passed, "actual": actual, "required": required}


def _prices(bars, field):
    if field not in ("adjusted_close", "close"):
        raise ValueError("买点策略仅支持调整收盘价或原始收盘价口径")
    prefix = "adjusted_" if field == "adjusted_close" else ""
    result = []
    for bar in bars:
        item = {"date": bar["date"], "volume": bar.get("volume")}
        for name in ("open", "high", "low", "close"):
            value = float(bar[prefix + name])
            if not math.isfinite(value) or value <= 0:
                raise ValueError("买点策略需要有效的同口径OHLC价格")
            item["adjusted_" + name] = value
        if not (item["adjusted_low"] <= min(item["adjusted_open"], item["adjusted_close"])
                <= max(item["adjusted_open"], item["adjusted_close"]) <= item["adjusted_high"]):
            raise ValueError("买点策略的OHLC关系不一致，请检查价格口径")
        result.append(item)
    return result


def _levels(close, trigger, stop, cap, atr, cfg, *, target=None):
    maximum = min(close + cfg.entry_buffer_atr * atr, cap)
    risk = maximum - stop
    valid = 0 < stop < close <= maximum and 0 < risk <= cfg.max_risk_atr * atr
    levels = {"confirmation_price": trigger, "entry_reference": close,
              "entry_max": maximum, "invalidation_price": stop, "target_reference": target,
              "risk_at_max_entry_atr": risk / atr, "reward_risk_at_max_entry": None}
    if target is not None and risk > 0:
        levels["reward_risk_at_max_entry"] = (target - maximum) / risk
    return levels, _check("ENTRY_DISTANCE", "入场未超追价上限且失效距离合理", valid,
                          {"close": close, "entry_max": maximum, "risk_atr": risk / atr},
                          {"max_risk_atr": cfg.max_risk_atr})


def _red_limit_check(background, code):
    count = background["consecutive_red_bars"]
    limit = background["max_consecutive_red_bars"]
    return _check(code, f"连续红带不超过{limit}根（第{limit + 1}根起按下降处理）",
                  count <= limit if count is not None else None, count, limit)


def _wave_validity_check(evidence, background, code):
    # 截止日期只向前推进，转绿不会清除；既不复活旧高点，也不借用旧上涨起点。
    cutoff = background["invalidated_through_session"]
    valid = cutoff is None or (evidence["peak_session"] > cutoff
                               and evidence["wave_start_session"] >= cutoff)
    return _check(code, "上涨波段未失效，或已在失效后重新形成",
                  valid, {"wave_start": evidence["wave_start_session"], "peak": evidence["peak_session"]},
                  {"wave_start_on_or_after": cutoff, "peak_after": cutoff})


def _pullback_setup(t, prices, rows, atrs, cfg, required_bars, background):
    """仅看到回踩日T为止的数据；先找过去高点，再检查其之前的上涨和其后的回落。"""
    p = cfg.pullback
    result = {"as_of": rows[t]["as_of"], "ready": False, "background_ready": False,
              "checks": [_red_limit_check(background, "PULLBACK_RED_LIMIT")], "evidence": None}
    if t + 1 < required_bars or atrs[t] is None or atrs[t] <= 0:
        result["checks"].append(_check("WAVE_HISTORY", "上涨波段、回踩与ATR历史足够", None, t + 1, required_bars))
        return result
    # 先取窗口最高价，再检查该日色带；不能排除红带高点后改挑较低绿带高点。
    # 高点窗口不含T；同价取最近一天。不是使用未来确认的拐点。
    peak = max(range(t - p.peak_lookback, t), key=lambda j: (prices[j]["adjusted_high"], j))
    base = min(range(peak - p.rise_lookback, peak), key=lambda j: rows[j]["price"])
    peak_atr = atrs[peak]
    if peak_atr is None or peak_atr <= 0:
        result["checks"].append(_check("PEAK_ATR", "高点当日ATR有效", None))
        return result
    rise = (rows[peak]["price"] - rows[base]["price"]) / peak_atr
    distance = (rows[peak]["price"] - rows[peak]["pullback_ema"]) / peak_atr
    peak_price = prices[peak]["adjusted_high"]
    ema = rows[t]["pullback_ema"]; tolerance = p.touch_tolerance_atr * atrs[t]
    peak_color = rows[peak].get("band_color")
    background_checks = [
        _check("PEAK_GREEN", "上涨背景的高点当天为绿带",
               peak_color == "GREEN" if peak_color in ("GREEN", "RED") else None,
               peak_color, "GREEN"),
        _check("PRIOR_RISE", "高点之前有足够上涨幅度", rise >= p.min_rise_atr, rise, p.min_rise_atr),
        _check("RISING_BACKGROUND", "上涨段EMA抬高", rows[peak]["pullback_ema"] > rows[base]["pullback_ema"],
               rows[peak]["pullback_ema"], rows[base]["pullback_ema"]),
        _check("PEAK_AWAY_FROM_EMA", f"高点日收盘曾明显远离EMA{p.ema_period}",
               distance >= p.min_peak_distance_atr, distance, p.min_peak_distance_atr),
        _wave_validity_check({"wave_start_session": rows[base]["as_of"],
                              "peak_session": rows[peak]["as_of"]}, background, "PULLBACK_WAVE_VALID"),
    ]
    result["checks"].extend(background_checks)
    # 新上涨背景的恢复不要求当天已触及EMA，但仍须通过原有上涨幅度、距离和绿带高点条件。
    result["background_ready"] = all(c["passed"] is True for c in result["checks"])
    result["checks"].extend([
        _check("FALL_FROM_PEAK", "高点之后已回落，而非继续创新高",
               prices[t]["adjusted_high"] < peak_price and rows[t]["price"] < rows[peak]["price"],
               {"high": prices[t]["adjusted_high"], "close": rows[t]["price"]},
               {"peak_high": peak_price, "peak_close": rows[peak]["price"]}),
        _check("TOUCH_EMA", f"回踩日价格范围到达EMA{p.ema_period}附近",
               prices[t]["adjusted_low"] <= ema + tolerance and prices[t]["adjusted_high"] >= ema - tolerance,
               {"low": prices[t]["adjusted_low"], "high": prices[t]["adjusted_high"]},
               {"lower": ema - tolerance, "upper": ema + tolerance}),
    ])
    result["ready"] = all(c["passed"] is True for c in result["checks"])
    result["evidence"] = {"wave_start_session": rows[base]["as_of"], "wave_start_close": rows[base]["price"],
        "peak_session": rows[peak]["as_of"], "peak_price": peak_price, "peak_close": rows[peak]["price"],
        "peak_band_color": peak_color,
        "pullback_consecutive_red_bars": background["consecutive_red_bars"],
        "peak_ema": rows[peak]["pullback_ema"], "peak_atr": peak_atr, "rise_atr": rise,
        "peak_distance_atr": distance, "pullback_session": rows[t]["as_of"],
        "pullback_low": prices[t]["adjusted_low"], "pullback_high": prices[t]["adjusted_high"],
        "pullback_ema": ema, "pullback_band_color": rows[t]["band_color"],
        "correction_low": min(b["adjusted_low"] for b in prices[peak + 1:t + 1])}
    return result


def _pullback(i, prices, rows, atrs, cfg, setup, background):
    """仅确认紧邻的前一交易日候选；开盘比较昨日EMA，收盘比较今日EMA，不回填T。"""
    p = cfg.pullback; close = rows[i]["price"]; ema = rows[i]["pullback_ema"]
    open_price = prices[i]["adjusted_open"]; previous_ema = rows[i - 1]["pullback_ema"]
    checks = list(setup["checks"]) + [
        _red_limit_check(background, "CONFIRM_RED_LIMIT"),
        _check("NEXT_OPEN_HELD", f"次日开盘不低于回踩日EMA{p.ema_period}",
               open_price >= previous_ema, open_price, previous_ema),
        _check("NEXT_CLOSE_HELD", f"次日收盘不低于当日EMA{p.ema_period}", close >= ema, close, ema),
    ]
    if setup["evidence"] is not None:
        checks.append(_wave_validity_check(setup["evidence"], background, "CONFIRM_WAVE_VALID"))
    if not setup["ready"]:
        return checks, None
    evidence = setup["evidence"]
    stop = min(evidence["correction_low"], prices[i]["adjusted_low"]) - cfg.stop_buffer_atr * atrs[i]
    # 买点只按已确认的形态与开盘/收盘条件生成，旧追价/风险距离不再暗中阻塞它。
    # 价格参考仍保留，但不代表仓位或执行许可；风险距离单独展示。
    levels, reference_risk = _levels(close, ema, stop, close + cfg.entry_buffer_atr * atrs[i], atrs[i], cfg)
    levels.update(evidence)
    levels.update(confirmation_session=rows[i]["as_of"], confirmation_open=open_price,
                  open_ema_reference=previous_ema, confirmation_ema=ema,
                  confirmation_band_color=rows[i]["band_color"], reference_risk_check=reference_risk,
                  confirmation_consecutive_red_bars=background["consecutive_red_bars"],
                  entry_reference_only=True)
    if not 0 < stop < close:
        levels["invalidation_price"] = None  # 技术形态与可执行保护价是不同结论。
        levels["risk_at_max_entry_atr"] = None
    return checks, levels


def _breakout(i, prices, rows, atrs, cfg):
    p = cfg.breakout; row = rows[i]; atr = atrs[i]; close = row["price"]
    # 高点和均量都不含信号日，避免把当天大涨和放量放进比较基准。
    reference = max(prices[i - p.lookback:i], key=lambda b: b["adjusted_high"])
    trigger = reference["adjusted_high"]
    volumes = [b["volume"] for b in prices[i - p.volume_lookback:i + 1]]
    volume_ok = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v > 0 for v in volumes)
    ratio = volumes[-1] / (sum(volumes[:-1]) / p.volume_lookback) if volume_ok else None
    stop = trigger - p.stop_below_level_atr * atr
    cap = min(trigger + p.max_distance_level_atr * atr,
              row["ema_long_slow"] + p.max_distance_slow_atr * atr)
    levels, distance = _levels(close, trigger, stop, cap, atr, cfg)
    levels["breakout_reference_session"] = reference["date"]
    return [
        _check("CLOSE_BREAKOUT", "收盘突破此前窗口最高价", close > trigger, close, trigger),
        _check("BREAKOUT_VOLUME", "突破日放量", ratio >= p.min_volume_ratio if ratio is not None else None,
               ratio, p.min_volume_ratio), distance,
    ], levels


def _rebound(i, prices, rows, atrs, cfg):
    p = cfg.rebound; row = rows[i]; atr = atrs[i]; close = row["price"]
    split = i - p.stabilization_bars + 1
    base = min(prices[split - p.support_lookback:split], key=lambda b: b["adjusted_low"])
    support = base["adjusted_low"]
    recent_low = min(b["adjusted_low"] for b in prices[split:i + 1])
    momentum = all(rows[j]["macd"] > rows[j - 1]["macd"]
                   for j in range(i - p.momentum_bars + 1, i + 1))
    trigger = prices[i - 1]["adjusted_high"]
    stop = support - cfg.stop_buffer_atr * atr
    # 红带目标只用当日慢EMA作为反弹参考，并冻结到该条信号；不是预测目标价。
    target = row["ema_long_slow"]
    levels, distance = _levels(close, trigger, stop, trigger + p.max_distance_trigger_atr * atr,
                               atr, cfg, target=target)
    levels.update(support_reference=support, support_session=base["date"], target_method="SIGNAL_DAY_SLOW_EMA")
    rr = levels["reward_risk_at_max_entry"]
    return [
        _check("LOW_HELD", "企稳段未跌破此前窗口低点", recent_low >= support, recent_low, support),
        _check("NEAR_SUPPORT", "企稳位置靠近此前低点", recent_low <= support + p.near_support_atr * atr,
               (recent_low - support) / atr, p.near_support_atr),
        _check("MOMENTUM_RECOVERY", "MACD连续回升且短DIF未下降",
               momentum and row["dif_short"] >= rows[i - 1]["dif_short"],
               {"macd": row["macd"], "dif_short": row["dif_short"]}, {"momentum_bars": p.momentum_bars}),
        _check("CLOSE_RECOVERY", "收盘突破前日高点", close > trigger, close, trigger),
        distance,
        _check("REBOUND_SPACE", "到慢EMA的反弹空间满足毛风险收益比",
               rr is not None and rr >= p.min_reward_risk, rr, p.min_reward_risk),
    ], levels


def calculate(bars, rows, params, cfg):
    """公共纯计算入口；输入完整预热历史，逐日检查，最后由调用方裁剪展示区间。"""
    if len(bars) != len(rows) or any(b["date"] != r["as_of"] for b, r in zip(bars, rows)):
        raise ValueError("策略K线与色带日期未对齐")
    if any(b.get("complete") is not True or (i > 0 and b["date"] <= bars[i - 1]["date"])
           for i, b in enumerate(bars)):
        raise ValueError("买点策略只接受日期递增、无重复的完整K线")
    prices = _prices(bars, params["policy"]["price_field"])
    # 独立计算，不经过回踩历史长度、ATR、红带期限或主趋势过滤。
    arrow_evaluations = calculate_macd_arrow(bars, cfg.macd_arrow, params["policy"]["price_field"])
    from .futu_macd import _ema
    averages = _ema([b["adjusted_close"] for b in prices], cfg.pullback.ema_period)
    # 回调均线独立配置；修改色带周期不会悄悄改变EMA20回调策略，也不修改传入报告。
    rows = [{**row, "pullback_ema": average} for row, average in zip(rows, averages)]
    atr_window = params["shared_features"]["atr_window"]
    atrs = [wilder_atr(prices[:i + 1], atr_window) for i in range(len(prices))]
    setup_history = max(atr_window + 1, cfg.pullback.ema_period) + cfg.pullback.peak_lookback + cfg.pullback.rise_lookback
    minimum = {
        "EMA_PULLBACK": setup_history + 1,
        "GREEN_BREAKOUT": max(atr_window, cfg.breakout.lookback, cfg.breakout.volume_lookback) + 1,
        "RED_REBOUND": max(atr_window + 1, cfg.rebound.support_lookback + cfg.rebound.stabilization_bars,
                           cfg.rebound.momentum_bars + 1),
    }
    functions = {"GREEN_BREAKOUT": _breakout, "RED_REBOUND": _rebound}
    previous = {key: False for key in STRATEGIES}; last_signal = {key: None for key in STRATEGIES}
    previous_wave = None; last_signal_levels = {key: None for key in STRATEGIES}
    previous_setup = None
    red_count = 0; invalidated_through = None; direction = None
    result = []
    for i, row in enumerate(rows):
        if not math.isclose(row["price"], prices[i]["adjusted_close"], rel_tol=1e-10):
            raise ValueError("策略价格与色带价格口径不一致")
        # 从预热期开始逐根累计；使用原始色带，不使用涨跌K线颜色或旧主趋势标签。
        color_today = row.get("band_color")
        limit = cfg.pullback.max_consecutive_red_bars
        if color_today == "GREEN":
            red_count = 0
        elif color_today == "RED":
            red_count = red_count + 1 if red_count is not None else None
        else:
            red_count = None
        if red_count is None:
            invalidated_through = row["as_of"]
            direction = None  # 色带缺失不视为转绿或允许延续旧上涨背景。
        elif red_count > limit:
            invalidated_through = row["as_of"]
            direction = "DOWN"
        background = {"direction": direction, "consecutive_red_bars": red_count,
                      "max_consecutive_red_bars": limit,
                      "invalidated_through_session": invalidated_through,
                      "invalidated_today": red_count == limit + 1}
        today_setup = _pullback_setup(i, prices, rows, atrs, cfg, setup_history, background) if cfg.pullback.enabled else None
        if today_setup and today_setup["background_ready"]:
            direction = "UP"
        background["direction"] = direction
        background["label"] = ("下降，等待新上涨波段" if direction == "DOWN" else
                               "上涨回踩" if direction == "UP" and red_count else
                               "上涨背景" if direction == "UP" else "上涨背景待确认")
        evaluations = []
        for key, (name, color, section) in STRATEGIES.items():
            checks = []; levels = None
            if not getattr(cfg, section).enabled:
                status = "DISABLED"
            elif color is not None and row["band_color"] != color:
                status = "NOT_APPLICABLE"
            elif i + 1 < minimum[key] or atrs[i] is None or atrs[i] <= 0:
                status = "UNKNOWN"
                checks = [_check("HISTORY", "完整历史及ATR足够", None, i + 1, minimum[key])]
            else:
                if key == "EMA_PULLBACK":
                    checks, levels = _pullback(i, prices, rows, atrs, cfg, previous_setup, background)
                else:
                    checks, levels = functions[key](i, prices, rows, atrs, cfg)
                if any(c["passed"] is None for c in checks):
                    status = "UNKNOWN"
                elif all(c["passed"] for c in checks):
                    if key == "EMA_PULLBACK":
                        # 同一波段连续确认不重复报买点；中断后新的T仍须重新通过T+1确认。
                        status = "HOLD" if previous[key] and previous_wave == levels["peak_session"] else "SIGNAL"
                    else:
                        status = "HOLD" if previous[key] else "SIGNAL"
                else:
                    status = "WAIT"
            matched = status in ("SIGNAL", "HOLD")
            if status == "SIGNAL":
                last_signal[key] = row["as_of"]
                last_signal_levels[key] = dict(levels)
            if key == "EMA_PULLBACK":
                previous_wave = levels["peak_session"] if matched else None
            previous[key] = matched  # 连续满足仅首次；回踩策略不因换色中断，按波段及价格条件去重。
            evaluations.append({"strategy_id": key, "name": f"EMA{cfg.pullback.ema_period}高点回踩买点" if key == "EMA_PULLBACK" else name, "status": status,
                "status_label": STATUS_LABELS[status], "conditions_met": matched,
                "buy_signal": status == "SIGNAL", "last_signal_session": last_signal[key],
                "checks": checks, "unmet": [c["label"] for c in checks if c["passed"] is not True],
                "levels": levels, "book_scope": "TACTICAL" if color == "RED" else "TREND",
                "route_by": "PRIOR_UPSWING_AND_PRICE" if key == "EMA_PULLBACK" else "RAW_BAND_COLOR",
                "evaluated_pullback_session": previous_setup["as_of"] if key == "EMA_PULLBACK" and previous_setup else None,
                "next_session_candidate": today_setup if key == "EMA_PULLBACK" else None,
                "background": background if key == "EMA_PULLBACK" else None,
                "signal_levels": last_signal_levels[key] if matched else None,
                "signal_levels_as_of": last_signal[key] if matched else None,
                "exit_rules": {"invalidation": "低于信号记录的失效价即形态失效；缺口不保证按该价成交",
                    "profit": "到达冻结反弹目标复查退出" if color == "RED" else "不因固定小幅浮盈退出；趋势持有与跟踪退出另接持仓模块",
                    "max_holding_calendar_months": cfg.rebound.max_holding_calendar_months if color == "RED" else None},
                "account_risk_checked": False, "order_allowed": False})
        arrow = arrow_evaluations[i]
        arrow["band_context"] = {"color": row["band_color"],
            "red_to_green_today": (rows[i - 1]["band_color"] == "RED" and row["band_color"] == "GREEN") if i else None}
        evaluations.append(arrow)
        result.append({"as_of": row["as_of"], "band_color": row["band_color"], "close": row["price"],
                       "band_indicator": {"dif": row["dif_long"], "dea": row["dea_long"],
                           "periods": [params["policy"][k] for k in ("long_fast", "long_slow", "long_signal")]},
                       "atr": atrs[i], "pullback_ema": row["pullback_ema"],
                       "pullback_background": background, "strategies": evaluations})
        previous_setup = today_setup
    return attach_exits(result, prices, cfg.exits)


def evaluate_report(report, cfg):
    """复用趋势绑定的确切行情快照，不抓财报/行情，不调用AI，不修改旧趋势快照。"""
    from .futu_macd import calculate as calculate_macd
    from .trend import _bars

    with Session() as session:
        source = session.get(Snapshot, report["source_market_id"])
        if source is None or source.kind != "market" or source.fingerprint != report["source_market_version"]:
            raise ValueError("趋势对应的行情快照缺失或版本不一致，请重新计算趋势")
        bars = _bars({"bars": [b for b in source.payload.get("bars", [])
            if report["history_start"] <= b["date"] <= report["actual_end"]]})
    # 仅补齐预热期原始指标；关闭主趋势层，策略完全不依赖其确认/回踩状态。
    params = {**report["parameters"], "policy": {**report["parameters"]["policy"], "trend": None}}
    rows = calculate_macd(bars, params)
    if not rows or rows[-1]["as_of"] != report["actual_end"]:
        raise ValueError("买点计算窗口与趋势截止日期不一致")
    evaluated = calculate(bars, rows, params, cfg)
    visible = [r for r in evaluated if r["as_of"] >= report["requested_start"]]
    signals = [{"event_kind": "BUY", "as_of": day["as_of"], "band_color": day["band_color"], "close": day["close"], **s}
               for day in visible for s in day["strategies"] if s["buy_signal"]]
    exits = [s for day in visible for s in day["exit_signals"]]
    alerts = [s for day in visible for s in day["exit_alerts"]]
    from .strategy_flows import build_flows
    flows = build_flows(evaluated, report["requested_start"])
    configuration = cfg.model_dump()
    return {"algorithm": ALGORITHM, "configuration": configuration, "configuration_version": digest(configuration),
        "input_version": digest({"algorithm": ALGORITHM, "configuration": configuration,
                                 "trend_snapshot": report["version"]}),
        "source_trend_id": report["id"], "source_market_id": report["source_market_id"],
        "source_market_version": report["source_market_version"], "price_field": params["policy"]["price_field"],
        "as_of": report["actual_end"], "route_by": "STRATEGY_SPECIFIC", "daily_assessments": visible,
        "signals": signals, "exit_signals": exits, "exit_alerts": alerts,
        "flows": flows, "flow_format_version": "paired-signal-flow-v1",
        "latest": visible[-1], "historical_only": True,
        "warnings": report.get("command", {}).get("warnings", report.get("warnings", [])),
        "message": "买点收盘确认，从下一完整日线起独立跟踪技术退出；盘中保护线破位仅由日线事后识别，指标退出收盘确认。价格均为历史口径参考，未模拟持仓、成交、成本或收益，未核验今日行情新鲜度。"}

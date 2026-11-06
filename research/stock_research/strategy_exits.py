"""按买入信号独立跟踪技术退出；不假设成交、仓位、成交价或收益。"""
import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM = "frozen-protection-common-three-state-exits-v2"
COMMON_RULE = "红带、快EMA低于慢EMA、MACD的DIF低于DEA三项收盘状态同时成立；不要求交叉顺序"
STAGE_LABELS = {0: "共同预警已撤销/尚未形成", 1: "一级预警：色带转弱",
                2: "二级预警：色带与短均线转弱", 3: "三项共同确认退出", None: "退出指标数据不足"}


class ExitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    enabled: bool = True
    # 兼容旧配置，仅保留存档；v2不消费此值，旧实现已全部注释在strategy_exits_legacy.py。
    ema_below_closes: int = Field(default=2, ge=1, le=20, description="停用：仅兼容旧v1配置，不参与共同退出")
    fast_ema_period: int = Field(default=5, ge=1, le=250)
    slow_ema_period: int = Field(default=10, ge=2, le=500)
    macd_low_lookback: int = Field(default=10, ge=2, le=120)
    macd_stop_buffer_atr: float = Field(default=0.3, ge=0, le=10)

    @model_validator(mode="after")
    def ordered(self):
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("退出快EMA周期必须小于慢EMA周期")
        return self


def _common_state(day, fast, slow, cfg):
    """判断现态而非等待新的交叉；只要有一项恢复，就按当日条件退回对应层级。"""
    band = day.get("band_indicator") or {}
    arrow = next((s for s in day["strategies"] if s["strategy_id"] == "MACD_ICON1"), {})
    indicator = arrow.get("indicator") or {}

    def below(a, b):
        if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
            return None
        return a < b

    red = below(band.get("dif"), band.get("dea"))
    ema_below = below(fast, slow)
    macd_below = below(indicator.get("dif"), indicator.get("dea"))
    complete = all(value is not None for value in (red, ema_below, macd_below))
    level = (0 if not red else 1 if not ema_below else 2 if not macd_below else 3) if complete else None
    return {"level": level, "label": STAGE_LABELS[level], "complete": complete,
            "confirmed": level == 3, "band_red": red, "ema_fast_below_slow": ema_below,
            "macd_dif_below_dea": macd_below,
            "evidence": {"band": band, "ema_fast": fast, "ema_slow": slow,
                         "ema_periods": [cfg.fast_ema_period, cfg.slow_ema_period],
                         "macd": indicator, "comparison": "strict_less_than"}}


def _plan(i, signal, days, prices, cfg):
    """仅使用信号当天及此前资料，保护线从建立起冻结，缺失不事后补造。"""
    day = days[i]
    if signal["strategy_id"] == "EMA_PULLBACK":
        levels = signal.get("levels") or {}
        stop = levels.get("invalidation_price")
        evidence = {"source": "EMA_SIGNAL_INVALIDATION", "peak_session": levels.get("peak_session"),
                    "pullback_session": levels.get("pullback_session"), "signal_atr": day["atr"],
                    "signal_levels": dict(levels)}
    else:
        n = cfg.macd_low_lookback
        enough = i + 1 >= n and day["atr"] is not None and day["atr"] > 0
        low = min(p["adjusted_low"] for p in prices[i - n + 1:i + 1]) if enough else None
        stop = low - cfg.macd_stop_buffer_atr * day["atr"] if enough else None
        evidence = {"source": "SIGNAL_LOOKBACK_LOW", "lookback": n,
                    "window_start": prices[i - n + 1]["date"] if i + 1 >= n else None,
                    "window_end": day["as_of"], "low": low, "signal_atr": day["atr"],
                    "buffer_atr": cfg.macd_stop_buffer_atr}
    valid = stop is not None and math.isfinite(stop) and 0 < stop < day["close"]
    return {"algorithm": ALGORITHM, "strategy_id": signal["strategy_id"],
            "name": signal["name"], "entry_signal_date": day["as_of"],
            "entry_signal_reference": day["close"], "initial_protection_price": stop if valid else None,
            "protection_status": "READY" if valid else "UNKNOWN",
            "protection_evidence": evidence, "trend_exit_rule": COMMON_RULE,
            "warning": None if valid else "信号当时无法形成有效保护线；价格保护未知，仍跟踪指标退出，不放宽买点条件",
            "tracking_starts": "信号日之后的下一完整交易日", "technical_only": True,
            "order_allowed": False}


def attach_exits(days, prices, cfg):
    """先评估此前信号，再建立今日信号；预热期也参与，不在展示起点重置。"""
    if len(days) != len(prices) or any(d["as_of"] != p["date"] for d, p in zip(days, prices)):
        raise ValueError("退出计算的策略日线与价格日期未对齐")
    from .futu_macd import _ema
    closes = [p["adjusted_close"] for p in prices]
    fast = _ema(closes, cfg.fast_ema_period)
    slow = _ema(closes, cfg.slow_ema_period)
    active = []
    for i, day in enumerate(days):
        day["exit_signals"] = []
        day["exit_tracking"] = []
        day["exit_alerts"] = []
        day["exit_market_state"] = None
        if not cfg.enabled:
            continue
        state = _common_state(day, fast[i], slow[i], cfg)
        day["exit_market_state"] = state
        strategies = {s["strategy_id"]: s for s in day["strategies"]}
        for key in ("EMA_PULLBACK", "MACD_ICON1"):
            strategies[key]["exit_rules"] = {
                "invalidation": "从买点后下一完整交易日起，最低价严格低于冻结保护线则技术失效",
                "profit": "不设固定盈利止盈；" + COMMON_RULE,
                "technical_only": True, "configuration": cfg.model_dump()}
        survivors = []
        for track in active:
            stop = track["initial_protection_price"]
            broken = stop is not None and prices[i]["adjusted_low"] < stop
            evidence = {"open": prices[i]["adjusted_open"], "low": prices[i]["adjusted_low"],
                        "close": day["close"], "initial_protection_price": stop,
                        "protection_broken": broken if stop is not None else None,
                        "open_below_protection": prices[i]["adjusted_open"] < stop if stop is not None else None,
                        "common_state": state}
            reasons = []
            if broken:
                reasons.append("INITIAL_PROTECTION_BROKEN")
            if state["confirmed"]:
                reasons.append("COMMON_THREE_STATE_CONFIRMED")
            previous_level = track["warning_level"]
            previous_status = track["status"]
            track.update(warning_level=state["level"], common_state=state, status_label=state["label"],
                         status="UNKNOWN" if state["level"] is None else
                                "TRACKING" if state["level"] == 0 else f"WARNING_{state['level']}")
            if reasons:
                label = {"INITIAL_PROTECTION_BROKEN": "跌破冻结初始保护线",
                         "COMMON_THREE_STATE_CONFIRMED": "红带＋短均线偏空＋MACD偏空，共同确认退出"}[reasons[0]]
                day["exit_signals"].append({**track, "as_of": day["as_of"], "event_kind": "EXIT",
                    "name": track["name"] + "：退出提示", "exit_signal": True, "buy_signal": False,
                    "status": "EXIT", "status_label": label, "reason": reasons[0], "reasons": reasons,
                    "evidence": evidence, "close_reference": day["close"],
                    "detected_at": "完整日线收盘后",
                    "trigger_timing": "日内破位，日线仅能事后识别" if broken else "收盘条件确认",
                    "execution_note": "未生成成交价；跳空不保证按保护线成交，收盘条件最早下一交易时段参考执行"})
            else:
                changed = previous_level != state["level"] or (previous_status == "PENDING" and state["level"] is None)
                initial_quiet = previous_status == "PENDING" and state["level"] == 0
                if changed and not initial_quiet:
                    day["exit_alerts"].append({"as_of": day["as_of"], "strategy_id": track["strategy_id"],
                        "entry_signal_date": track["entry_signal_date"], "from_level": previous_level,
                        "to_level": state["level"], "status_label": state["label"], "common_state": state,
                        "technical_only": True, "order_allowed": False})
                survivors.append(track)
        # 同策略多条买入提示分别跟踪，不假设已经持仓、加仓或用新买点重置旧保护线。
        for signal in day["strategies"]:
            if signal["buy_signal"] and signal["strategy_id"] in ("EMA_PULLBACK", "MACD_ICON1"):
                plan = _plan(i, signal, days, prices, cfg)
                signal["exit_plan"] = plan
                survivors.append({**plan, "warning_level": None, "common_state": state,
                                  "status": "PENDING", "status_label": "买点收盘确认，下一交易日起评估退出"})
        active = survivors
        day["exit_tracking"] = [{**track, "as_of": day["as_of"]} for track in active]
    return days

"""富途 FACE 笑脸买点：按完整日线计算 CCI 和均线交叉。"""
from math import fsum

from pydantic import BaseModel, ConfigDict, Field

STRATEGY_ID = "FACE"
ALGORITHM = "futu-face-cci-buy-v1"


class FacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    enabled: bool = False
    cci_period: int = Field(default=14, ge=2, le=250)
    cci_threshold: float = -110.0
    min_bars_since_cross: int = Field(default=8, ge=0, le=250)
    count_window: int = Field(default=5, ge=1, le=250)


def calculate(prices, cfg):
    """复用策略入口校验后的同口径高、低、收盘价，逐根计算，不读未来日线。"""
    closes = [bar["adjusted_close"] for bar in prices]
    typical = [(bar["adjusted_high"] + bar["adjusted_low"] + close) / 3
               for bar, close in zip(prices, closes)]
    conditions = []
    result = []
    parameters = cfg.model_dump()
    last_cross = last_signal = None
    previous_spread = previous_cci = None
    for i, bar in enumerate(prices):
        day = bar["date"]
        # CROSS(MA(C,10),MA(C,5))：长均线今天严格在上，昨天在下或相等。
        if i >= 9:
            spread = fsum(closes[i - 9:i + 1]) / 10 - fsum(closes[i - 4:i + 1]) / 5
            if previous_spread is not None and previous_spread <= 0 < spread:
                last_cross = i
            previous_spread = spread
        bars_since_cross = i - last_cross if last_cross is not None else None
        elapsed = bars_since_cross >= cfg.min_bars_since_cross if bars_since_cross is not None else None

        cci = None
        if i + 1 >= cfg.cci_period:
            # 先减去当天典型价。均差不变，也能让完全平坦的窗口精确得到零均差。
            offsets = [value - typical[i] for value in typical[i + 1 - cfg.cci_period:i + 1]]
            average = fsum(offsets) / cfg.cci_period
            deviation = fsum(abs(value - average) for value in offsets) / cfg.cci_period
            if deviation > 0:
                cci = -average / (0.015 * deviation)
        crossed = (previous_cci <= cfg.cci_threshold < cci and cci > previous_cci
                   if cci is not None and previous_cci is not None else None)

        # COUNT 统计原始 COND03，包含今天及被 COUNT 抑制的旧候选。
        candidate = False if elapsed is False or crossed is False else elapsed and crossed
        conditions.append(candidate)
        recent = conditions[max(0, i + 1 - cfg.count_window):]
        count = recent.count(True)
        unique = (False if count > 1 else None if len(recent) < cfg.count_window or None in recent
                  else count == 1)
        signal = candidate is True and unique is True
        status = ("SIGNAL" if signal else "WAIT" if candidate is False or unique is False
                  else "UNKNOWN")
        if signal:
            last_signal = day
        levels = {"entry_reference": closes[i], "entry_max": None, "invalidation_price": None,
                  "target_reference": None, "entry_reference_only": True} if signal else None
        checks = [
            {"code": "FACE_MA_CROSS_AGE", "label": "距上次MA5下穿MA10的完整日线数达标",
             "passed": elapsed, "actual": bars_since_cross, "required": cfg.min_bars_since_cross},
            {"code": "FACE_CCI_CROSS", "label": "CCI上涨并上穿阈值", "passed": crossed,
             "actual": {"previous_cci": previous_cci, "cci": cci}, "required": cfg.cci_threshold},
            {"code": "FACE_UNIQUE_SETUP", "label": "最近窗口内原始抄底条件只命中一次",
             "passed": unique, "actual": {"known_count": count, "window": cfg.count_window}, "required": 1},
        ]
        result.append({
            "as_of": day, "strategy_id": STRATEGY_ID, "name": "FACE 笑脸抄底", "algorithm": ALGORITHM,
            "status": status, "status_label": {"SIGNAL": "买入提示（笑脸）", "WAIT": "未出现笑脸",
                                              "UNKNOWN": "数据不足"}[status],
            "conditions_met": signal, "buy_signal": signal, "last_signal_session": last_signal,
            "checks": checks, "unmet": [check["label"] for check in checks if check["passed"] is not True],
            "indicator": {"cci": cci, "previous_cci": previous_cci, "bars_since_cross": bars_since_cross,
                          "last_ma_cross_session": prices[last_cross]["date"] if last_cross is not None else None,
                          "icon_code": 5 if signal else None,
                          "icon_price": bar["adjusted_low"] * 0.998 if signal else None,
                          "parameters": parameters},
            "levels": levels, "signal_levels": levels, "signal_levels_as_of": day if signal else None,
            "route_by": "INDEPENDENT_FACE", "book_scope": "TACTICAL",
            "exit_rules": None, "account_risk_checked": False, "order_allowed": False,
        })
        previous_cci = cci
    return result

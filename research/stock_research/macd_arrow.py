"""独立MACD图标1策略：零轴之下金叉当日收盘后提示买入。"""
import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM = "macd-icon1-buy-v1"
STRATEGY_ID = "MACD_ICON1"


class MacdArrowPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    short: int = Field(default=12, ge=1, le=500)
    long: int = Field(default=26, ge=2, le=1000)
    m: int = Field(default=9, ge=1, le=500)

    @model_validator(mode="after")
    def ordered(self):
        if self.short >= self.long:
            raise ValueError("红色箭头策略的快EMA周期必须小于慢EMA周期")
        return self


def calculate(bars, cfg, price_field="adjusted_close"):
    """输入包含预热期的完整日线；只按原图标1条件判断，不消费其他策略状态。"""
    from .futu_macd import _ema

    if price_field not in ("adjusted_close", "close"):
        raise ValueError("红色箭头策略仅支持调整收盘价或原始收盘价")
    prices = []
    for i, bar in enumerate(bars):
        if bar.get("complete") is not True or (i and bar["date"] <= bars[i - 1]["date"]):
            raise ValueError("红色箭头策略需要日期递增、无重复的完整日线")
        value = float(bar[price_field])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("红色箭头策略的收盘价无效")
        prices.append(value)
    fast = _ema(prices, cfg.short)
    slow = _ema(prices, cfg.long)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = _ema(dif, cfg.m)
    result = []; last_signal = None
    for i, bar in enumerate(bars):
        # 前日相等、今日严格上穿才算CROSS；零轴条件保留原式的等于边界。
        cross = dif[i] > dea[i] and dif[i - 1] <= dea[i - 1] if i else None
        below = max(dif[i], dea[i]) <= 0
        signal = cfg.enabled and cross is True and below
        status = ("DISABLED" if not cfg.enabled else "UNKNOWN" if cross is None else
                  "SIGNAL" if signal else "WAIT")
        checks = [
            {"code": "ICON1_CROSS", "label": "DIF上穿DEA", "passed": cross,
             "actual": {"dif": dif[i], "dea": dea[i],
                        "previous_dif": dif[i - 1] if i else None,
                        "previous_dea": dea[i - 1] if i else None},
             "required": "当日DIF>DEA，前日DIF≤DEA"},
            {"code": "ICON1_BELOW_ZERO", "label": "DIF和DEA均不高于零轴", "passed": below,
             "actual": max(dif[i], dea[i]), "required": 0},
        ] if cfg.enabled else []
        evidence = {"dif": dif[i], "dea": dea[i], "macd": 2 * (dif[i] - dea[i]),
                    "previous_dif": dif[i - 1] if i else None,
                    "previous_dea": dea[i - 1] if i else None,
                    "icon_code": 1 if signal else None, "parameters": cfg.model_dump(),
                    "ema_seed": "first_input", "price_field": price_field}
        # 未讨论止损、追价或退出；只给信号日收盘参考，不能继承EMA回踩策略的价格规则。
        levels = {"entry_reference": prices[i], "entry_max": None, "invalidation_price": None,
                  "target_reference": None, "entry_reference_only": True} if signal else None
        if signal:
            last_signal = bar["date"]
        result.append({"as_of": bar["date"], "strategy_id": STRATEGY_ID,
            "name": "MACD零轴下金叉买入", "algorithm": ALGORITHM,
            "status": status, "status_label": {"SIGNAL": "买入提示（红色箭头）", "WAIT": "未出现红色箭头",
                "UNKNOWN": "数据不足", "DISABLED": "已关闭"}[status],
            "conditions_met": signal, "buy_signal": signal, "last_signal_session": last_signal,
            "checks": checks, "unmet": [c["label"] for c in checks if c["passed"] is not True],
            "indicator": evidence, "levels": levels, "signal_levels": levels,
            "signal_levels_as_of": bar["date"] if signal else None,
            "route_by": "INDEPENDENT_MACD_ICON1", "book_scope": "UNASSIGNED",
            "exit_rules": None, "account_risk_checked": False, "order_allowed": False})
    return result

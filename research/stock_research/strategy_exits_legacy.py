# 旧版退出v1的完整注释备份：仅留作将来回归比较，生产路径不导入、不执行。
# 恢复比较时需明确建立独立版本，不与共同退出同时放行。
# """按买入信号独立跟踪技术退出；不假设成交、仓位、成交价或收益。"""
# import math
# 
# from pydantic import BaseModel, ConfigDict, Field
# 
# ALGORITHM = "signal-linked-technical-exits-v1"
# 
# 
# class ExitPolicy(BaseModel):
#     model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
#     enabled: bool = True
#     ema_below_closes: int = Field(default=2, ge=1, le=20)
#     macd_low_lookback: int = Field(default=10, ge=2, le=120)
#     macd_stop_buffer_atr: float = Field(default=0.3, ge=0, le=10)
# 
# 
# def _plan(i, signal, days, prices, cfg):
#     """仅使用信号当天及此前资料，保护线从建立起冻结，缺失不事后补造。"""
#     day = days[i]
#     if signal["strategy_id"] == "EMA_PULLBACK":
#         levels = signal.get("levels") or {}
#         stop = levels.get("invalidation_price")
#         evidence = {"source": "EMA_SIGNAL_INVALIDATION", "peak_session": levels.get("peak_session"),
#                     "pullback_session": levels.get("pullback_session"), "signal_atr": day["atr"],
#                     "signal_levels": dict(levels)}
#         rule = f"连续{cfg.ema_below_closes}根完整日线收盘低于各自回踩EMA；收回或相等清零"
#     else:
#         n = cfg.macd_low_lookback
#         enough = i + 1 >= n and day["atr"] is not None and day["atr"] > 0
#         low = min(p["adjusted_low"] for p in prices[i - n + 1:i + 1]) if enough else None
#         stop = low - cfg.macd_stop_buffer_atr * day["atr"] if enough else None
#         evidence = {"source": "SIGNAL_LOOKBACK_LOW", "lookback": n,
#                     "window_start": prices[i - n + 1]["date"] if i + 1 >= n else None,
#                     "window_end": day["as_of"], "low": low, "signal_atr": day["atr"],
#                     "buffer_atr": cfg.macd_stop_buffer_atr}
#         rule = "下一次本策略DIF下穿DEA，不限定零轴上下"
#     valid = stop is not None and math.isfinite(stop) and 0 < stop < day["close"]
#     return {"algorithm": ALGORITHM, "strategy_id": signal["strategy_id"],
#             "name": signal["name"], "entry_signal_date": day["as_of"],
#             "entry_signal_reference": day["close"], "initial_protection_price": stop if valid else None,
#             "protection_status": "READY" if valid else "UNKNOWN",
#             "protection_evidence": evidence, "trend_exit_rule": rule,
#             "warning": None if valid else "信号当时无法形成有效保护线；价格保护未知，仍跟踪指标退出，不放宽买点条件",
#             "tracking_starts": "信号日之后的下一完整交易日", "technical_only": True,
#             "order_allowed": False}
# 
# 
# def attach_exits(days, prices, cfg):
#     """先评估此前信号，再建立今日信号；预热期也参与，不在展示起点重置。"""
#     if len(days) != len(prices) or any(d["as_of"] != p["date"] for d, p in zip(days, prices)):
#         raise ValueError("退出计算的策略日线与价格日期未对齐")
#     active = []
#     for i, day in enumerate(days):
#         day["exit_signals"] = []
#         day["exit_tracking"] = []
#         if not cfg.enabled:
#             continue
#         strategies = {s["strategy_id"]: s for s in day["strategies"]}
#         for key in ("EMA_PULLBACK", "MACD_ICON1"):
#             strategies[key]["exit_rules"] = {
#                 "invalidation": "从买点后下一完整交易日起，最低价严格低于冻结保护线则技术失效",
#                 "profit": "不设固定盈利止盈；连续收低回踩EMA退出" if key == "EMA_PULLBACK" else "不设固定盈利止盈；下一次本策略死叉退出",
#                 "technical_only": True, "configuration": cfg.model_dump()}
#         survivors = []
#         for track in active:
#             stop = track["initial_protection_price"]
#             broken = stop is not None and prices[i]["adjusted_low"] < stop
#             evidence = {"open": prices[i]["adjusted_open"], "low": prices[i]["adjusted_low"],
#                         "close": day["close"], "initial_protection_price": stop,
#                         "protection_broken": broken if stop is not None else None,
#                         "open_below_protection": prices[i]["adjusted_open"] < stop if stop is not None else None}
#             reasons = []
#             if broken:
#                 reasons.append("INITIAL_PROTECTION_BROKEN")
#             if track["strategy_id"] == "EMA_PULLBACK":
#                 below = day["close"] < day["pullback_ema"]
#                 track["below_ema_closes"] = track["below_ema_closes"] + 1 if below else 0
#                 evidence.update(ema=day["pullback_ema"], below_ema_closes=track["below_ema_closes"],
#                                 required_closes=cfg.ema_below_closes,
#                                 consecutive_close_evidence=[{"as_of": prior["as_of"], "close": prior["close"],
#                                     "ema": prior["pullback_ema"]}
#                                     for prior in days[i - track["below_ema_closes"] + 1:i + 1]]
#                                     if track["below_ema_closes"] else [])
#                 if track["below_ema_closes"] >= cfg.ema_below_closes:
#                     reasons.append("EMA_CONSECUTIVE_CLOSES_BELOW")
#             else:
#                 indicator = strategies["MACD_ICON1"]["indicator"]
#                 death = (indicator["previous_dif"] is not None and indicator["previous_dea"] is not None
#                          and indicator["dif"] < indicator["dea"]
#                          and indicator["previous_dif"] >= indicator["previous_dea"])
#                 evidence.update(indicator=indicator, death_cross=death)
#                 if death:
#                     reasons.append("MACD_DEATH_CROSS")
#             if reasons:
#                 label = {"INITIAL_PROTECTION_BROKEN": "跌破冻结初始保护线",
#                          "EMA_CONSECUTIVE_CLOSES_BELOW": f"连续{cfg.ema_below_closes}日收盘低于EMA",
#                          "MACD_DEATH_CROSS": "MACD死叉（不限定零轴）"}[reasons[0]]
#                 day["exit_signals"].append({**track, "as_of": day["as_of"], "event_kind": "EXIT",
#                     "name": track["name"] + "：退出提示", "exit_signal": True, "buy_signal": False,
#                     "status": "EXIT", "status_label": label, "reason": reasons[0], "reasons": reasons,
#                     "evidence": evidence, "close_reference": day["close"],
#                     "detected_at": "完整日线收盘后",
#                     "trigger_timing": "日内破位，日线仅能事后识别" if broken else "收盘条件确认",
#                     "execution_note": "未生成成交价；跳空不保证按保护线成交，收盘条件最早下一交易时段参考执行"})
#             else:
#                 survivors.append(track)
#         # 同策略多条买入提示分别跟踪，不假设已经持仓、加仓或用新买点重置旧保护线。
#         for signal in day["strategies"]:
#             if signal["buy_signal"] and signal["strategy_id"] in ("EMA_PULLBACK", "MACD_ICON1"):
#                 plan = _plan(i, signal, days, prices, cfg)
#                 signal["exit_plan"] = plan
#                 survivors.append({**plan, "below_ema_closes": 0})
#         active = survivors
#         day["exit_tracking"] = [{**track, "as_of": day["as_of"],
#             "status": "PROTECTION_UNKNOWN" if track["protection_status"] == "UNKNOWN" else
#                       "WATCH" if track["below_ema_closes"] else "TRACKING"}
#             for track in active]
#     return days

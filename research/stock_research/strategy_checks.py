"""逐步实现策略：本轮仅检查STR-01的趋势前置条件，不判断买点或完整适用性。"""

ALGORITHM="str01-trend-gate-v1"
STATUS_LABELS={"PASS":"趋势门槛通过","WAIT":"等待","FAIL":"趋势门槛不通过","UNKNOWN":"无法判断"}
REMAINING_CHECKS=["普通股类型与基础可交易性","基本面与研究逻辑","周线背景与市场环境",
    "有效支撑、回调企稳和入场价格","压力空间与事件限制","仓位与账户风险"]


def evaluate_str01_trend_gate(report):
    """只消费已有富途主趋势结果；不联网、重算趋势、读写数据库或调用AI。"""
    state=report.get("trend_latest") or {}
    current=state.get("current_trend")
    status=state.get("assessment_status")
    phase=state.get("phase")
    as_of=report.get("actual_end")
    last_day=(report.get("daily_latest") or {}).get("as_of")
    warnings=report.get("warnings") or (report.get("command") or {}).get("warnings")
    verdict="UNKNOWN";code="UNSUPPORTED_TREND_STATE";message="趋势状态或阶段不一致，请先核对趋势结果"
    if not report.get("trend_layer_enabled") or not state:
        code="TREND_LAYER_REQUIRED";message="尚无主趋势结果，请先启用富途主趋势回踩层"
    elif not as_of or last_day!=as_of:
        code="TREND_DATE_MISMATCH";message="趋势截止日期缺失或不一致，不能放行趋势条件"
    elif warnings:
        code="SOURCE_WARNINGS";message="来源或历史覆盖有警告，请先核对趋势报告中的警告"
    elif status in ("INSUFFICIENT_DATA","STALE"):
        code="TREND_UNAVAILABLE";message="趋势数据不足或不可确认，不能判断策略趋势门槛"
    elif status=="PENDING" and current is None:
        verdict="WAIT";code="TREND_PENDING";message="主趋势尚未确认，等待明确方向；历史上涨不视为当前仍然有效"
    elif current=="UP" and status=="CONFIRMED" and phase in ("UP_ACTIVE","UP_RECOVERED"):
        verdict="PASS";code="UP_TREND_CONFIRMED";message="上涨趋势有效，可以继续研究STR-01顺势回调条件"
    elif current=="UP" and status=="CHALLENGED" and phase in ("UP_PULLBACK","UP_WEAKENING","UP_NEUTRAL"):
        verdict="WAIT";code="UP_TREND_CHALLENGED";message="上涨背景处于回踩、转弱或动能不明确阶段，等待企稳及主趋势恢复"
    elif current=="DOWN" and ((status=="CONFIRMED" and phase in ("DOWN_ACTIVE","DOWN_RECOVERED"))
            or (status=="CHALLENGED" and phase in ("DOWN_PULLBACK","DOWN_WEAKENING","DOWN_NEUTRAL"))):
        verdict="FAIL";code="DOWN_TREND_NOT_ALLOWED";message="下降背景不通过核心顺势回调的趋势门槛；短暂反抽也不当成上涨"
    elif current=="RANGE" and status=="CONFIRMED":
        verdict="FAIL";code="RANGE_NOT_ALLOWED";message="震荡背景不通过核心顺势回调的趋势门槛，区间策略另行设计"
    return {"algorithm":ALGORITHM,"strategy_id":"STR-01","strategy_name":"核心普通股顺势回调",
        "scope":"TREND_GATE_ONLY","as_of":as_of,"historical_only":True,
        "source_trend":{"snapshot_id":report.get("id"),"version":report.get("version"),
            "algorithm":report.get("algorithm"),"current_trend":current,"assessment_status":status,
            "phase":phase,"phase_label":state.get("phase_label")},
        "trend_gate_status":verdict,"trend_gate_label":STATUS_LABELS[verdict],"trend_gate_passed":verdict=="PASS",
        "reason_code":code,"reason":message,"remaining_checks":list(REMAINING_CHECKS),
        "strategy_applicability":"NOT_EVALUATED","setup_status":"NOT_EVALUATED",
        "strategy_ready":False,"buy_signal":False,"new_risk_allowed":False,
        "message":"只验证历史截止日的趋势前置条件，未判断普通股类型、完整策略、当前数据有效性或买卖许可"}

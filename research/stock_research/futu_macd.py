"""保留用户富途公式，独立管理主趋势与回踩；震荡、策略和账户风险另行设计。"""
import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import exchange_calendars as xcals
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from .config import ROOT, settings
from .macd_trend import MacdTrendPolicy, assess_trend, trend_intervals
from .store import Session, Snapshot, digest, latest, save_snapshot, serialize_snapshot, stamp
from .trend import _bars, _calendar, _calculation_lock, completed_weeks

ALGORITHM="user-futu-macd-trend-with-pullback-v2"
POLICY_FILE=ROOT/"config"/"futu-macd-policy.json"
BIAS_LABELS={"UP_BIAS":"偏上","DOWN_BIAS":"偏下"}
SOURCES={"futu_script":"https://support.futunn.com/topic541",
    "reference_functions":"https://knowledge.thinktrader.net/docs/vba/system_function.html"}


class MacdPolicy(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    version: str
    review_status: str
    note: str
    long_fast: int=Field(ge=1,le=500)
    long_slow: int=Field(ge=2,le=1000)
    long_signal: int=Field(ge=1,le=500)
    short_fast: int=Field(ge=1,le=500)
    short_slow: int=Field(ge=2,le=1000)
    filter_bars: int=Field(ge=0,le=500)
    price_field: Literal["adjusted_close","close"]
    trend: MacdTrendPolicy | None=None

    @model_validator(mode="after")
    def ordered(self):
        if self.long_fast>=self.long_slow or self.short_fast>=self.short_slow:
            raise ValueError("快EMA周期必须小于对应慢EMA周期")
        return self


def policy(policy_file=None):
    chosen=Path(policy_file or POLICY_FILE).expanduser()
    try:
        cfg=MacdPolicy.model_validate(json.loads(chosen.read_text()))
    except (OSError,ValueError,ValidationError) as exc:
        raise ValueError(f"富途MACD配置无效，请检查{chosen.name}的字段和EMA周期") from exc
    if cfg.trend and cfg.trend.enabled and cfg.trend.anchor_lookback<settings.pivot_left+settings.pivot_right+1:
        raise ValueError("结构保护位回看范围不足以容纳已确认拐点")
    return {"policy":cfg.model_dump(),"ema_seed":"first_input",
        "ema_update":"alpha=2/(N+1); EMA[t]=alpha*X[t]+(1-alpha)*EMA[t-1]",
        "cross":"A[t]>B[t] and A[t-1]<=B[t-1]",
        "filter":"保留首次满足信号，抑制随后N根；被抑制信号不延长抑制期",
        "deashort_source":"DIFLONG（原公式如此，且DEASHORT未参与条件）",
        "shared_features":{"atr_window":settings.atr_window,"pivot_left":settings.pivot_left,"pivot_right":settings.pivot_right},
        "parity_status":"未与富途相同价格/复权/EMA预热长度逐点核对；函数递推及FILTER按公开同类公式语义实现"}


def _ema(values,period):
    result=[];previous=None;alpha=2/(period+1)
    for value in values:
        previous=value if previous is None else alpha*value+(1-alpha)*previous
        if not math.isfinite(previous):
            raise ValueError("EMA出现无效数值，停止计算富途指标")
        result.append(previous)
    return result


def calculate(bars,params):
    """从传入历史首根递推，过滤也包含预热期；不读取未来K线、不回填信号。"""
    cfg=params["policy"]
    prices=[];previous_date=None
    for bar in bars:
        day=date.fromisoformat(bar["date"])
        if bar.get("complete") is not True or (previous_date is not None and day<=previous_date):
            raise ValueError("富途指标需要按日期递增的完整K线，不能重复或使用未收盘数据")
        previous_date=day
        price=float(bar[cfg["price_field"]])
        if not math.isfinite(price) or price<=0:
            raise ValueError("富途指标的收盘价格缺失或无效")
        prices.append(price)
    periods={cfg[k] for k in ("long_fast","long_slow","short_fast","short_slow")}
    averages={n:_ema(prices,n) for n in periods}
    dif=[a-b for a,b in zip(averages[cfg["long_fast"]],averages[cfg["long_slow"]])]
    dea=_ema(dif,cfg["long_signal"])
    short=[a-b for a,b in zip(averages[cfg["short_fast"]],averages[cfg["short_slow"]])]
    macd=[2*(a-b) for a,b in zip(dif,dea)]
    rows=[];last_kept=None
    for i,bar in enumerate(bars):
        conditions={"COND1":i>0 and dif[i]>dea[i] and dif[i-1]<=dea[i-1],
            "COND2":i>0 and macd[i]>macd[i-1],"COND3":i>0 and short[i]>=short[i-1],
            "COND4":i>0 and dea[i]>=dea[i-1]}
        raw=all(conditions.values())
        suppressed=raw and last_kept is not None and i-last_kept<=cfg["filter_bars"]
        signal=raw and not suppressed
        if signal:
            last_kept=i
        green=dif[i]>=dea[i]
        rows.append({"as_of":bar["date"],"price":prices[i],"price_field":cfg["price_field"],
            "ema_long_fast":averages[cfg["long_fast"]][i],"ema_long_slow":averages[cfg["long_slow"]][i],
            "dif_long":dif[i],"dea_long":dea[i],"macd":macd[i],"dif_short":short[i],
            "dea_short":dea[i],"conditions":conditions,"raw_signal":raw,"signal":signal,
            "filter_suppressed":suppressed,"filter_last_kept_session":bars[last_kept]["date"] if last_kept is not None else None,
            "cross_down":i>0 and dif[i]<dea[i] and dif[i-1]>=dea[i-1],
            "band_color":"GREEN" if green else "RED","bias":"UP_BIAS" if green else "DOWN_BIAS",
            "bias_label":"偏上" if green else "偏下","equal_lines":dif[i]==dea[i],
            "bars_from_seed":i+1,"icon_code":4 if signal else None,"icon_position":dea[i] if signal else None})
    if cfg.get("trend") and cfg["trend"]["enabled"]:
        for row,state in zip(rows,assess_trend(bars,rows,params)):
            row["trend"]=state
    return rows


def _intervals(rows,start):
    groups=[]
    for row in rows:
        if groups and groups[-1]["bias"]==row["bias"]:
            groups[-1]["rows"].append(row)
        else:
            groups.append({"bias":row["bias"],"rows":[row]})
    result=[]
    for i,group in enumerate(groups):
        visible=[r for r in group["rows"] if r["as_of"]>=start]
        if not visible:
            continue
        first,last=visible[0],visible[-1]
        result.append({"start_session":first["as_of"],"end_session":last["as_of"],"bar_count":len(visible),
            "bias":first["bias"],"bias_label":first["bias_label"],"band_color":first["band_color"],
            "observed_state_started":group["rows"][0]["as_of"],"continues_from_warmup":group["rows"][0]["as_of"]<start,
            "signal_sessions":[r["as_of"] for r in visible if r["signal"]],
            "start_price":first["price"],"end_price":last["price"],"end_is_observation_cutoff":i==len(groups)-1})
    return result


def evaluate_timeline(security_id, *, history_start, start, end=None, cached_only=False, policy_file=None, region="US"):
    """独立指标快照；主趋势与原色带分开保存，震荡明确保持未评估。"""
    try:
        seed=date.fromisoformat(history_start);begin=date.fromisoformat(start)
        until=date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValueError("日期必须使用YYYY-MM-DD格式") from exc
    if seed>begin or (until and until<begin):
        raise ValueError("需要满足历史起点≤展示起点≤展示终点")
    history_start=seed.isoformat();start=begin.isoformat();end=until.isoformat() if until else None
    params=policy(policy_file)
    from . import market
    with _calculation_lock(security_id,cached_only), Session() as session:
        source=latest(session,security_id,market.snapshot_kind(region))
        if not source:
            raise ValueError("尚无行情档案，请先获取日线")
        chosen=[b for b in source.payload.get("bars",[]) if b.get("complete") is True
            and b["date"]>=history_start and (not end or b["date"]<=end)]
        bars=_bars({"bars":chosen})
        if not bars or bars[-1]["date"]<start:
            raise ValueError("展示窗口内没有完整日线，请核对起止日期")
        inputs={"algorithm":ALGORITHM,"parameters":params,"history_start":history_start,"start":start,"end":end,
            "bars":bars,"calendar_version":xcals.__version__,"adjustment":source.payload.get("adjustment")}
        if region == "CN":
            inputs["calendar"] = "XSHG"
        key=digest(inputs)
        saved=session.scalar(select(Snapshot).where(Snapshot.security_id==security_id,
            Snapshot.kind=="indicator_futu_macd",Snapshot.fingerprint==key).order_by(Snapshot.created_at.desc()).limit(1))
        if saved:
            return {**serialize_snapshot(saved),"cached":True}
        if cached_only:
            raise ValueError("该窗口/参数没有已保存的富途指标，请去掉--cached先计算；本次未联网或重算")
        if region == "CN":
            cal,_ = market.calendar_context(region)
            expected = [s.date().isoformat() for s in cal.sessions_in_range(bars[0]["date"],bars[-1]["date"])]
            if [bar["date"] for bar in bars] != expected:
                raise ValueError("沪深行情存在交易日缺口，不能确认指标")
        else:
            completed_weeks(bars)
            cal=_calendar(seed,until or date.fromisoformat(bars[-1]["date"]))
        rows=calculate(bars,params);visible=[r for r in rows if r["as_of"]>=start]
        warnings=[]
        if seed<begin:
            expected={s.date().isoformat() for s in cal.sessions_in_range(history_start,(begin-timedelta(days=1)).isoformat())}
            if expected-{b["date"] for b in bars if b["date"]<start}:
                warnings.append({"message":"预热期交易日不完整，EMA可能与完整历史计算不同"})
        warmup=sum(b["date"]<start for b in bars)
        if warmup<max(params["policy"]["long_slow"],params["policy"]["short_slow"]):
            warnings.append({"message":"展示前预热短于慢EMA周期；仍按首值递推展示，不声称EMA已经充分稳定"})
        if until:
            sessions=cal.sessions_in_range(start,end)
            if len(sessions) and bars[-1]["date"]<sessions[-1].date().isoformat():
                warnings.append({"message":"现有行情未覆盖请求终点，只展示实际取得的日线"})
        trend_enabled=bool(params["policy"].get("trend") and params["policy"]["trend"]["enabled"])
        payload={"algorithm":ALGORITHM,"mode":"futu_macd_timeline","input_key":key,"created_at":stamp(),
            "parameters":params,"policy_file":str(Path(policy_file or POLICY_FILE).expanduser()),
            "source_market_id":source.id,"source_market_version":source.fingerprint,"source_fetched_at":source.payload.get("fetched_at"),
            "history_start":history_start,"requested_start":start,"requested_end":end,"actual_history_start":bars[0]["date"],
            "actual_start":visible[0]["as_of"],"actual_end":bars[-1]["date"],"warmup_daily_count":warmup,
            "daily_intervals":_intervals(rows,start),"daily_assessments":visible,
            "trend_layer_enabled":trend_enabled,"trend_intervals":trend_intervals(rows,start) if trend_enabled else [],
            "trend_latest":visible[-1].get("trend"),"calendar_version":xcals.__version__,
            "signals":[r for r in visible if r["signal"]],"daily_latest":visible[-1],"warnings":warnings,
            "range_assessment_status":"NOT_EVALUATED","trend_dependency_ready":False,"strategy_evaluated":False,
            "review_status":"draft","research_only":True,"sources":SOURCES,
            "message":"保留绿带偏上、红带偏下及四条件信号；主趋势另行处理回踩/反抽和价格失效；震荡未评估，不生成策略或风险许可",
            "replay_note":"仅使用指定起点之后的现有调整价/原价档案；EMA初始化、复权及数据源差异尚未与富途逐点对齐，不声称无偏收益回测。"}
        item=save_snapshot(session,security_id,"indicator_futu_macd",payload);item.fingerprint=key
        session.commit()
        return {**serialize_snapshot(item),"cached":False}

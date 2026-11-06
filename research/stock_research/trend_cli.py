"""stock-trend：只获取/复用行情并判断趋势，不触发财报流程或DeepSeek。"""
import logging
import sys
from datetime import date
from pathlib import Path

from .cli import ChineseParser, configure_console_logging, decimal_text, dump

# 用-m启动时__name__为__main__；仍归入项目日志，保证控制台能看到进度与错误。
logger=logging.getLogger("stock_research.trend_cli")
CHECK_LABELS={
    "INDEPENDENT_PIVOTS":"独立高低点不足或重合","HIGHER_HIGH":"高点尚未充分抬高",
    "HIGHER_LOW":"低点尚未充分抬高","LOWER_HIGH":"高点尚未充分降低","LOWER_LOW":"低点尚未充分降低",
    "MA_SLOPE":"均线方向斜率不足","PRICE_POSITION":"价格位置未满足","MA_ALIGNMENT":"快慢均线尚未顺向排列",
    "EFFICIENCY":"ER持续性不足","DIRECTIONAL_DISPLACEMENT":"方向位移不足",
    "RANGE_BOUNDARY_TOUCHES":"上下沿独立触及不足","RANGE_WIDTH":"区间宽度未满足",
    "RANGE_CONTAINMENT":"区间包含率或现价边界未满足","FLAT_MA":"均线尚未走平",
    "MA_CROSSINGS":"均线穿越次数不足","LOW_EFFICIENCY":"ER尚未降至震荡门槛"}


def _progress_lines(progress, *, detailed=False):
    if not progress:
        return []
    lines=["阶段依据："+progress["explanation"]]
    failures=progress.get("blocking_checks",[])
    if failures:
        lines.append("未满足："+"；".join(CHECK_LABELS.get(c["code"],c["code"]) for c in failures))
        if detailed:
            for item in failures:
                lines.append(f"  {CHECK_LABELS.get(item['code'],item['code'])}：实际{item['actual']}；要求{item['requirement']}")
    return lines


def parser():
    p=ChineseParser(description="输入股票或ETF代码，输出日线趋势与完整周线背景。",add_help=False)
    p._positionals.title="股票代码";p._optionals.title="可选参数"
    p.add_argument("-h","--help",action="help",help="查看中文说明")
    p.add_argument("symbol",nargs="?",help="例如AAPL、MSFT、SPY；未填写时提示输入")
    p.add_argument("--method",choices=("three-family","futu-macd"),default="three-family",
        help="判断方式：three-family原三族确认（默认）；futu-macd主趋势/回踩及原色带信号，需--start，震荡另判")
    p.add_argument("--strategies","--strategy-check",dest="strategy_check",action="store_true",
        help="输出EMA回踩、MACD图标1买入及关联技术退出；需--method futu-macd和--start")
    p.add_argument("--strategy-policy",type=Path,help="买点参数配置，默认config/band-strategies.json；配合--strategies")
    p.add_argument("--strategy-details", action="store_true", help="配合--strategies显示原逐项条件；默认展示买入到卖出的配对过程")
    modes=p.add_mutually_exclusive_group()
    modes.add_argument("--cached",action="store_true",help="只读已保存趋势、不联网；买点仍按当前策略参数从档案计算，趋势缓存不匹配则提示")
    modes.add_argument("--force",action="store_true",help="仅手动同步行情；不获取财报、不调用DeepSeek")
    p.add_argument("--json",action="store_true",help="输出完整判断依据、参数和状态变化JSON")
    p.add_argument("--output",type=Path,help="另存结果，不覆盖已有文件；JSON请同时使用--json")
    p.add_argument("--history-limit",type=int,default=8,help="文本显示每周期最近多少次状态变化，默认8；0不显示，JSON始终完整")
    p.add_argument("--history-start",help="历史预热起点，例如2026-01-01；配合--start，未填时用展示起点当年的1月1日")
    p.add_argument("--start",help="历史区间展示起点，例如2026-03-01；启用逐根历史分段模式")
    p.add_argument("--end",help="历史展示终点，包含当日；未填时到本地实际取得的最后完整日线")
    p.add_argument("--policy",type=Path,help="指定所选方式的配置；富途方式默认futu-macd-policy.json，其余历史SMA20/30、当前SMA50/200")
    return p


def render_text(report,history_limit):
    from .trend import LABELS, STATUSES
    lines=[f"{report['symbol']} · 日线趋势 / 完整周线背景",
        f"行情状态：{report['market_cache']['state']}；最近完整交易日：{report.get('last_complete_session','未知')}",
        f"趋势结果：{'复用本地记录' if report.get('cached') else '已计算并保存'}；算法：{report.get('algorithm','尚无有效计算')}"]
    for key,title in (("daily","日线主趋势"),("weekly","周线背景")):
        view=report[key]
        lines.extend(["",f"【{title}】",
            f"当前：{view['trend_label']}；状态：{view['status_label']}；截止：{view.get('as_of') or '未知'}",
            f"候选：{view['candidate_label']}；上次确认：{view['last_confirmed_label']}（{view.get('last_confirmed_at') or '无'}）"])
        if view.get("candidate_trend"):
            lines.append(f"候选始于{view.get('candidate_since')}；连续满足{view.get('confirmation_count',0)}/{view.get('confirmation_required')}根，另需满足最短保持时段")
        if view.get("progress"):
            lines.append("当前阶段："+view["progress"]["label"])
            lines.extend(_progress_lines(view["progress"],detailed=True))
        evidence=view.get("evidence",{})
        features=evidence.get("features",{})
        if features:
            cfg=report["parameters"]["policy"][key]
            lines.append(f"SMA{cfg['ma_fast']}={features['ma_fast']:.4f}；SMA{cfg['ma_slow']}={features['ma_slow']:.4f}；ATR={features['atr']:.4f}")
            lines.append(f"均线归一化斜率={features['ma_slope_atr_per_bar']:.4f} ATR/根；ER={features['efficiency_ratio']:.4f}；带方向位移={features['signed_displacement_atr']:.4f} ATR")
            chosen=view.get("current_trend") or view.get("candidate_trend")
            targets=[chosen] if chosen else ["UP","RANGE","DOWN"]
            source=evidence.get("hold_checks",{}) if view.get("decision_mode")=="hold" else evidence.get("checks",{})
            for direction in targets:
                families=source.get(direction,{})
                text="；".join(f"{label}{'满足' if all(c['passed'] for c in families.get(family,[])) and families.get(family) else '未满足'}"
                    for family,label in (("structure","结构"),("moving_average","均线"),("persistence","持续性")))
                lines.append(f"{LABELS[direction]}条件（{'保持' if view.get('decision_mode')=='hold' else '进入'}）：{text}")
            for kind,label in (("high","高点"),("low","低点")):
                points=features["pivots"][kind]
                lines.append(f"最近已确认{label}："+("；".join(f"{p['date']} {p['price']:.4f}，确认于{p['confirmed_at']}" for p in points) or "不足"))
            bounds=features["range"]
            if bounds["lower"] is not None and bounds["upper"] is not None:
                lines.append(f"震荡候选边界：{bounds['lower']:.4f}～{bounds['upper']:.4f}；上下沿独立触及{len(bounds['upper_touches'])}/{len(bounds['lower_touches'])}次；包含率{bounds['containment']:.1%}（仍须其余条件通过）")
        elif evidence.get("required_bars"):
            lines.append(f"完整K线{evidence.get('available_bars',0)}根，指标预热至少需要{evidence['required_bars']}根")
        lines.append("原因编号："+"、".join(view.get("reason_codes",[])))
        if view.get("assessment_status")=="STALE":
            lines.append("以上指标/历史标签只供回看，当前没有有效方向；需要更新行情或按新参数重新计算。")
        if history_limit:
            for event in view.get("transitions",[])[-history_limit:]:
                phase=("；阶段"+event["progress"]["label"]) if event.get("progress") else ""
                lines.append(f"变化 {event['session']}：{STATUSES[event['assessment_status']]}；趋势{LABELS.get(event['current_trend'],'无')}；候选{LABELS.get(event['candidate_trend'],'无')}{phase}")
    lines.extend(["",report.get("message","行情不可用，仅保留历史结果"),
        "形成中、修复及短期动能只解释部分证据，未确认趋势仍不可放行依赖趋势的策略。",
        "均线、ATR和拐点采用供应商调整价；研究模板未验证，不是概率、买卖信号或账户风控许可。",
        "配置文件："+str(report.get("policy_file","未知"))])
    for warning in report.get("command",{}).get("warnings",[]):
        lines.append("警告："+warning["message"])
    return "\n".join(lines)+"\n"


def render_timeline(report):
    daily_cfg=report["parameters"]["policy"]["daily"]
    lines=[f"{report['symbol']} · 历史趋势分段（逐根判断）",
        f"使用历史：{report['history_start']}起，实际最早{report['actual_history_start']}；展示{report['actual_start']}至{report['actual_end']}",
        f"展示前预热：{report['warmup_daily_count']}根日线、{report['warmup_weekly_count']}根完整周线",
        f"日线SMA{daily_cfg['ma_fast']}/{daily_cfg['ma_slow']}；连续{daily_cfg['confirmation_bars']}根满足后确认；配置：{report['policy_file']}"]
    for key,title in (("daily_intervals","日线主趋势区间"),("weekly_intervals","完整周线背景区间")):
        lines.extend(["",f"【{title}】","开始日期     结束日期     趋势 / 判断状态                 根数"])
        for interval in report[key]:
            description=interval["trend_label"] if interval["assessment_status"]=="CONFIRMED" else interval["status_label"]
            progress=interval.get("progress")
            if interval["assessment_status"]=="PENDING" and progress:
                description=progress["label"]+"（待确认）"
            elif interval["candidate_trend"]:
                description+=f"（候选{interval['candidate_label']}）"
            if interval["continues_from_warmup"]:
                description+="，承接预热期"
            mark=" *" if interval["end_is_observation_cutoff"] else ""
            lines.append(f"{interval['start_session']}   {interval['end_session']}   {description}   {interval['bar_count']}{mark}")
            if interval["assessment_status"]=="PENDING":
                lines.extend("  "+line for line in _progress_lines(interval.get("end_progress") or progress))
        if not report[key]:
            lines.append("展示窗口没有该周期的完整K线，不补造趋势。")
    lines.extend(["","* 末段只表示观察截止，不表示趋势已经结束。根数为实际完整交易日/周数。",
        "候选前两根不会事后改成已确认；确认从达到条件的当天生效。未知、待确认与数据不足均保留。",
        "待确认按已有方向证据和未通过条件细分；形成中、修复、短期动能均不是已确认趋势或买卖信号。",
        "日线只挂当日已经完成的最近周线；40周背景不足时如实显示，不用两个月冒充长期背景。",
        report["message"],report["replay_note"],
        "这是未验证的短样本研究模板，未计算策略收益、买卖信号或账户风险许可。"])
    for warning in report.get("command",{}).get("warnings",[]):
        lines.append("警告："+warning["message"])
    return "\n".join(lines)+"\n"


def render_futu_macd(report):
    cfg=report["parameters"]["policy"]
    lines=[f"{report['symbol']} · 富途MACD主趋势 / 回踩 / 原色带",
        f"使用历史：{report['history_start']}起；展示{report['actual_start']}至{report['actual_end']}；预热{report['warmup_daily_count']}根日线",
        f"DIF=EMA({cfg['long_fast']})-EMA({cfg['long_slow']})；DEA=EMA(DIF,{cfg['long_signal']})；短DIF=EMA({cfg['short_fast']})-EMA({cfg['short_slow']})",
        f"价格字段：{cfg['price_field']}；信号抑制其后{cfg['filter_bars']}根；结果：{'复用本地记录' if report.get('cached') else '已计算并保存'}",
        "算法："+report["algorithm"]]
    if report.get("trend_layer_enabled"):
        from .macd_trend import REASON_LABELS
        t=cfg["trend"]
        statuses={"CONFIRMED":"已确认","CHALLENGED":"受挑战","PENDING":"待确认","INSUFFICIENT_DATA":"数据不足"}
        lines.extend([f"主趋势：连续{t['confirmation_bars']}根进入；保护位缓冲{t['break_buffer_atr']} ATR；反向至少{t['reversal_bars']}根、深度≥{t['reversal_depth_atr']} ATR并满足价格确认可切换",
            "","【主趋势与回踩/反抽区间】","开始日期     结束日期     主趋势 / 阶段 / 判断状态                 根数"])
        for item in report["trend_intervals"]:
            continued="，承接预热期" if item["continues_from_warmup"] else ""
            mark=" *" if item["end_is_observation_cutoff"] else ""
            lines.append(f"{item['start_session']}   {item['end_session']}   {item['trend_label']} / {item['phase_label']} / {statuses[item['assessment_status']]}{continued}   {item['bar_count']}{mark}")
            if "PROTECTION_BROKEN" in item["start_reason_codes"]:
                lines.append("  原结构保护位已失效，不等待色带确认。")
            if "SUSTAINED_OPPOSITE_WITH_PRICE" in item["start_reason_codes"]:
                lines.append("  反向色带持续加深且价格连续确认，原方向失效。")
        view=report["trend_latest"]
        lines.extend(["","【最近主趋势依据】",f"{view['trend_label']} / {view['phase_label']} / {statuses[view['assessment_status']]}；主趋势确认日起：{view.get('confirmed_since') or '无'}"])
        if view.get("candidate_trend"):
            direction="上涨" if view["candidate_trend"]=="UP" else "下降"
            lines.append(f"候选{direction}；色带进入条件连续{view['candidate_count']}/{view['confirmation_required']}根，另须有效保护位")
        if view.get("atr") is not None and view.get("gap_depth_atr") is not None:
            lines.append(f"ATR={view['atr']:.4f}；两线间距={view['gap_depth_atr']:.4f} ATR；同方向色带连续{view['band_run']}根；当前逆向色带{view['counter_band_bars']}根")
        anchor=view.get("anchor")
        if anchor:
            source="已确认拐点" if anchor["method"]=="CONFIRMED_PIVOT" else "前序窗口极值（备用，非确认拐点）"
            relation="低于" if view["current_trend"]=="UP" else "高于"
            lines.append(f"结构锚点：{anchor['date']} {anchor['price']:.4f}；来源：{source}；选定于{anchor['selected_at']}")
            lines.append(f"冻结失效线：{anchor['break_level']:.4f}；收盘{relation}此线则原趋势失效；缓冲按选定时ATR固定")
        for direction,barrier in view.get("reclaim_barriers",{}).items():
            relation="不低于" if direction=="UP" else "不高于"
            lines.append(f"原{'上涨' if direction=='UP' else '下降'}恢复价格门槛：收盘{relation}{barrier['price']:.4f}，并重新满足色带确认")
        invalidation=view.get("pending_invalidation")
        if invalidation:
            lines.append(f"待处理的原趋势失效：{invalidation['session']}；收盘{invalidation['price']:.4f}；{REASON_LABELS[invalidation['reason']]}")
        lines.append("主趋势原因："+"、".join(REASON_LABELS.get(code,code) for code in view["reason_codes"]))
        lines.append("回踩/反抽是当日观察状态，后续可升级失效；原始色带与信号保留，不因主趋势保持而改色。")
    else:
        lines.extend(["","配置未启用主趋势回踩层，本次只输出原始色带与信号。"])
    lines.extend(["","【原始色带区间】","开始日期     结束日期     色带 / 偏向        根数   四条件信号"])
    for item in report["daily_intervals"]:
        color="绿带" if item["band_color"]=="GREEN" else "红带"
        continued="，承接预热期" if item["continues_from_warmup"] else ""
        mark=" *" if item["end_is_observation_cutoff"] else ""
        lines.append(f"{item['start_session']}   {item['end_session']}   {color} / {item['bias_label']}{continued}   {item['bar_count']}   {','.join(item['signal_sessions']) or '无'}{mark}")
    lines.extend(["","【过滤后四条件信号】"])
    for row in report["signals"]:
        lines.append(f"{row['as_of']}  收盘{row['price']:.4f}  DIF{row['dif_long']:.4f}  DEA{row['dea_long']:.4f}  MACD{row['macd']:.4f}  短DIF{row['dif_short']:.4f}")
    if not report["signals"]:
        lines.append("该窗口没有通过原公式四条件及FILTER的信号；没有信号不等于没有偏向。")
    lines.extend(["","* 最后一段仅表示观察截止；相等时按原公式归绿带。绿带/红带并不对应每日K线的涨跌颜色。",
        "震荡：未评估；主趋势为未验证研究结果，回踩受挑战时也不产生策略或账户风险许可。",
        "原公式DEASHORT继续引用DIFLONG，且未参与条件；未自行改成短DIF的DEA。",
        report["parameters"]["parity_status"],report["replay_note"],"配置文件："+report["policy_file"]])
    for warning in report.get("command",{}).get("warnings",[]):
        lines.append("警告："+warning["message"])
    return "\n".join(lines)+"\n"


def render_strategies(report, details=False):
    if details:
        return render_strategies_details(report)
    from .strategy_flows import render_flows
    result = report["strategy_results"]
    lines = [f"{report['symbol']} · 买入—预警—卖出完整信号过程",
             f"展示{report['actual_start']}至{result['as_of']}；按策略和原买点配对。",
             "日期是信号确认日，价格是收盘参考，不是实际成交；同股多条过程可以重叠，不代表加仓。"]
    lines.extend(render_flows(result["flows"], symbol=report["symbol"]))
    lines.extend(["", "买点及指标卖出信号收盘确认，最早下一交易时段参考执行；日内保护线破位只由日线事后识别。",
                  "使用--strategy-details查看原始逐项条件，--json保留全部证据；未计算实际收益。"])
    for warning in result["warnings"]:
        lines.append("数据警告：" + warning["message"])
    return "\n".join(lines) + "\n"


def render_strategies_details(report):
    result=report["strategy_results"];latest=result["latest"]
    cfg=report["parameters"]["policy"]
    strategy_cfg=result["configuration"]
    enabled=[item["name"] for item in latest["strategies"] if item["status"]!="DISABLED"]
    color="绿带" if latest["band_color"]=="GREEN" else "红带"
    lines=[f"{report['symbol']} · 多策略买入与技术退出提示",
        f"展示{report['actual_start']}至{result['as_of']}；最近收盘{latest['close']:.4f}；{color}",
        "启用策略："+"、".join(enabled or ["无"]),
        f"回调参考EMA{strategy_cfg['pullback']['ema_period']}；色带仍为{cfg['long_fast']}/{cfg['long_slow']}/{cfg['long_signal']}。",
        "回踩策略要求此前上涨波段的高点当天为绿带，再看高点回落与次日开盘/收盘守住；回踩日、确认日允许红带。",
        f"最多容忍{strategy_cfg['pullback']['max_consecutive_red_bars']}根连续红带，下一根起按下降处理并取消旧上涨资格；确认日也计数。",
        f"独立红色箭头策略：MACD {strategy_cfg['macd_arrow']['short']}/{strategy_cfg['macd_arrow']['long']}/{strategy_cfg['macd_arrow']['m']}，图标1出现即提示买入；不套用EMA回踩或红带期限。",
        "各策略分别记录信号；图标25、二次金叉、底背/顶背尚未接入。",
        (f"共同退出：红带＋EMA{strategy_cfg['exits']['fast_ema_period']}低于EMA{strategy_cfg['exits']['slow_ema_period']}＋MACD的DIF低于DEA；三项同时确认，不要求发生顺序；冻结保护线优先。"
         if strategy_cfg['exits']['enabled'] else "技术退出跟踪已关闭。"),
        "","【历史新买点】","确认日期     策略              确认收盘价    入场参考上沿  失效参考    反弹目标"]
    for signal in result["signals"]:
        levels=signal["levels"]
        if signal["strategy_id"]=="MACD_ICON1":
            indicator=signal["indicator"]
            lines.append(f"{signal['as_of']}  {signal['name']}  {levels['entry_reference']:.4f}  —  —  —")
            lines.append(f"  买入提示：出现红色箭头（图标1）；DIF={indicator['dif']:.6f}，DEA={indicator['dea']:.6f}，零轴下金叉。")
            plan=signal.get("exit_plan")
            if plan:
                stop=plan["initial_protection_price"]
                lines.append(f"  冻结保护线：{f'{stop:.4f}' if stop is not None else '未知'}；{plan['trend_exit_rule']}。")
                if plan.get("warning"):
                    lines.append("  "+plan["warning"])
            lines.append("  当日收盘后确认；从下一完整交易日跟踪退出，不代表已经成交。")
            continue
        target=f"{levels['target_reference']:.4f}" if levels["target_reference"] is not None else "趋势持有"
        stop=f"{levels['invalidation_price']:.4f}" if levels["invalidation_price"] is not None else "暂无有效价"
        lines.append(f"{signal['as_of']}  {signal['name']}  {levels['entry_reference']:.4f}  {levels['entry_max']:.4f}  {stop}  {target}")
        if signal["strategy_id"]=="EMA_PULLBACK":
            lines.append(f"  绿带高点{levels['peak_session']} {levels['peak_price']:.4f} → 回踩{levels['pullback_session']}，最低{levels['pullback_low']:.4f} / EMA{levels['pullback_ema']:.4f}")
            lines.append(f"  次日开盘{levels['confirmation_open']:.4f} ≥ 昨日EMA{levels['open_ema_reference']:.4f}；收盘{levels['entry_reference']:.4f} ≥ 当日EMA{levels['confirmation_ema']:.4f}")
            lines.append(f"  连续红带：回踩日{levels['pullback_consecutive_red_bars']}根，确认日{levels['confirmation_consecutive_red_bars']}根。")
            if not levels["reference_risk_check"]["passed"]:
                lines.append("  价格参考暂不满足配置的风险条件；技术形态信号保留，尚无执行许可。")
    if not result["signals"]:
        lines.append("该窗口没有命中买点；下方显示最近一日未满足的具体条件。")
    lines.extend(["", "【共同退出预警变化（升级、退回与撤销）】"])
    for alert in result.get("exit_alerts", []):
        state=alert["common_state"]
        lines.append(f"{alert['as_of']} {alert['strategy_id']} 买点{alert['entry_signal_date']}：{alert['status_label']}；"
                     f"红带={state['band_red']}，快EMA低于慢EMA={state['ema_fast_below_slow']}，MACD偏空={state['macd_dif_below_dea']}。")
    if not result.get("exit_alerts"):
        lines.append("展示区间内没有预警层级变化；已终止跟踪的买点不重复预警。")
    lines.extend(["", "【历史技术退出（关联买点，不代表真实卖出）】"])
    for event in result.get("exit_signals", []):
        lines.append(f"{event['as_of']}  {event['name']}；对应买点{event['entry_signal_date']}；{event['status_label']}")
        stop=event["initial_protection_price"]
        lines.append(f"  冻结保护线{f'{stop:.4f}' if stop is not None else '未知'}；当日收盘参考{event['close_reference']:.4f}（非成交价）；{event['trigger_timing']}。")
        evidence=event["evidence"]
        if event["reason"]=="INITIAL_PROTECTION_BROKEN":
            lines.append(f"  当日开盘{evidence['open']:.4f}，最低{evidence['low']:.4f}；开盘是否已低于保护线：{evidence['open_below_protection']}。")
        if evidence.get("common_state"):
            common=evidence["common_state"]["evidence"]
            lines.append(f"  色带DIF/DEA {decimal_text(common['band'].get('dif'))}/{decimal_text(common['band'].get('dea'))}；"
                         f"快/慢EMA {decimal_text(common['ema_fast'])}/{decimal_text(common['ema_slow'])}；"
                         f"MACD DIF/DEA {decimal_text(common['macd'].get('dif'))}/{decimal_text(common['macd'].get('dea'))}。")
    if not result.get("exit_signals"):
        lines.append("展示区间内没有技术退出事件。")
    lines.extend(["", "【最近一日仍在技术跟踪的买点】"])
    for track in latest.get("exit_tracking", []):
        stop=track["initial_protection_price"]
        progress=track["status_label"]
        lines.append(f"{track['strategy_id']} 买点{track['entry_signal_date']}；冻结保护线{decimal_text(stop)}；{progress}。")
        common=track["common_state"]
        lines.append(f"  当日状态：红带={common['band_red']}，快EMA低于慢EMA={common['ema_fast_below_slow']}，MACD偏空={common['macd_dif_below_dea']}。")
        if track.get("warning"):
            lines.append("  "+track["warning"])
    if not latest.get("exit_tracking"):
        lines.append("没有未结束的技术跟踪项；这不是实际空仓判断。")
    lines.extend(["", "【回踩策略转为下降的日期】"])
    invalidations = [day for day in result["daily_assessments"] if day["pullback_background"]["invalidated_today"]]
    for day in invalidations:
        background = day["pullback_background"]
        lines.append(f"{day['as_of']}：连续红带第{background['consecutive_red_bars']}根，超过容忍期限；旧上涨波段失效，停止回踩买点。")
    if not invalidations:
        lines.append("展示区间内没有新发生的连续红带超限；预热期失效记录仍参与判断。")
    lines.extend(["","【最近一日逐策略判断】"])
    for item in latest["strategies"]:
        if item["status"]=="DISABLED":
            continue
        lines.append(f"{item['name']}：{item['status_label']}")
        background = item.get("background")
        if background:
            count = background["consecutive_red_bars"]
            lines.append(f"  回踩策略趋势：{background['label']}；连续红带{count if count is not None else '未知'}根，上限{background['max_consecutive_red_bars']}根。")
            if background["invalidated_through_session"]:
                lines.append(f"  上涨起点不得早于{background['invalidated_through_session']}，且高点须晚于该日；转绿不恢复旧高点资格。")
        if item["status"]=="HOLD":
            lines.append(f"  该机会已于{item['last_signal_session']}触发；不重复生成新买点，也不自动加仓。")
        for check in item["checks"]:
            mark="满足" if check["passed"] is True else "缺数据" if check["passed"] is None else "未满足"
            lines.append(f"  {mark}：{check['label']}；实际={check['actual']}；参考={check['required']}")
        candidate=item.get("next_session_candidate")
        if candidate and candidate["ready"]:
            evidence=candidate["evidence"]
            lines.append(f"  今日{candidate['as_of']}也形成回踩候选（高点{evidence['peak_session']}），须等下一完整交易日开盘/收盘确认，不能提前记成买点。")
        if item["buy_signal"]:
            levels=item["levels"]
            if item["strategy_id"]=="MACD_ICON1":
                lines.append(f"  买入提示：当日出现图标1红色箭头，收盘参考{levels['entry_reference']:.4f}；最早下一交易时段参考执行。")
                continue
            lines.append(f"  信号在本日收盘后成立；最早下一交易时段参考范围{levels['entry_reference']:.4f}～{levels['entry_max']:.4f}，并非已成交或执行许可。")
            stop=f"{levels['invalidation_price']:.4f}" if levels["invalidation_price"] is not None else "暂无有效价"
            lines.append(f"  冻结失效参考{stop}；{item['exit_rules']['profit']}。")
    lines.extend(["",f"窗口内共{len(result['signals'])}个策略买点事件（同日多个策略分别记录），不是成交笔数或胜率。",
        "上涨幅度、离均线距离、回看窗口与附近容差均可配置；同一波段连续确认只列首次。",
        "EMA回踩的次日确认不要求突破前日高点或涨幅；红色箭头策略在箭头当日收盘确认。",
        "两策略均不设置小幅盈利自动止盈；技术退出按买点分别记录，真实持仓与成交尚未接入。",
        "以上为技术买点，未接基本面/证券质量、账户仓位或真实持仓管理。",
        result["message"],"买点配置："+result["policy_file"]])
    for warning in result["warnings"]:
        lines.append("数据警告："+warning["message"])
    return "\n".join(lines)+"\n"


def main():
    p=parser();args=p.parse_args();configure_console_logging()
    if args.history_limit<0:
        p.error("--history-limit不能为负数")
    if (args.history_start or args.end) and not args.start:
        p.error("--history-start和--end需要配合--start")
    if args.method=="futu-macd" and not args.start:
        p.error("富途色带方式请指定--start展示起点；例如--method futu-macd --start 2026-03-01")
    if args.strategy_check and args.method!="futu-macd":
        p.error("色带买点策略需要--method futu-macd及--start")
    if args.strategy_policy and not args.strategy_check:
        p.error("--strategy-policy需要配合--strategies")
    if args.strategy_details and not args.strategy_check:
        p.error("--strategy-details需要配合--strategies")
    if args.start:
        try:
            args.start=date.fromisoformat(args.start).isoformat()
            args.history_start=date.fromisoformat(args.history_start).isoformat() if args.history_start else f"{date.fromisoformat(args.start).year}-01-01"
            args.end=date.fromisoformat(args.end).isoformat() if args.end else None
            if args.history_start>args.start or (args.end and args.end<args.start):
                p.error("需要满足历史起点≤展示起点≤展示终点")
        except ValueError:
            p.error("起止日期请使用YYYY-MM-DD格式")
    try:
        symbol=args.symbol
        if not symbol:
            print("请输入美股或ETF代码：",end="",file=sys.stderr,flush=True)
            symbol=input().strip()
        output=args.output.expanduser().resolve() if args.output else None
        if output and output.exists():
            raise FileExistsError
        from .config import settings
        from .market import refresh_market
        from .providers import failure_detail
        from .service import security_for
        from .store import Session, initialize, latest
        from .trend import evaluate_timeline, evaluate_trend, policy
        if args.method=="futu-macd":
            from . import futu_macd
            chosen_policy=(args.policy or futu_macd.POLICY_FILE).expanduser().resolve()
            futu_macd.policy(chosen_policy)
        else:
            chosen_policy=(args.policy or (settings.trend_history_policy_file if args.start else settings.trend_policy_file)).expanduser().resolve()
            policy(chosen_policy)  # 配置错误先提示，避免不必要的行情请求。
        if args.strategy_check:
            from . import band_strategies
            strategy_policy=(args.strategy_policy or band_strategies.POLICY_FILE).expanduser().resolve()
            strategy_cfg=band_strategies.policy(strategy_policy)
        initialize()
        security=security_for(symbol,create=not args.cached)
        command={"mode":"cached" if args.cached else "force" if args.force else "refresh","warnings":[]}
        logger.info("开始趋势判断：%s；方式%s；只处理行情与指标，不调用DeepSeek",security.symbol,args.method)
        if not args.cached:
            with Session() as session:
                local=latest(session,security.id,"market")
                reuse_historical=bool(args.start and not args.force and local and any(
                    b.get("complete") is True and b["date"]>=args.start and (not args.end or b["date"]<=args.end)
                    for b in local.payload.get("bars",[])))
            if reuse_historical:
                command["market"]={"cached":True,"mode":"historical_archive"}
                logger.info("历史模式复用本地行情档案；仅使用%s及之后的数据，不因实时TTL过期重新下载",args.history_start)
            else:
                try:
                    command["market"]=refresh_market(security.id,args.force)
                    logger.info("行情%s", "命中缓存" if command["market"].get("cached") else "已更新")
                except Exception as exc:
                    warning=failure_detail(exc);command["warnings"].append(warning)
                    logger.warning("行情更新未完成：%s；尝试读取历史记录",warning["message"])
        if args.method=="futu-macd":
            logger.info("正在读取色带缓存或按完整日线顺序递推EMA与信号")
        else:
            logger.info("正在读取趋势缓存或按完整K线顺序计算日线/周线判断")
        if args.method=="futu-macd":
            report=futu_macd.evaluate_timeline(security.id,history_start=args.history_start,start=args.start,end=args.end,
                cached_only=args.cached,policy_file=chosen_policy)
            command["warnings"].extend(report.get("warnings",[]))
        elif args.start:
            report=evaluate_timeline(security.id,history_start=args.history_start,start=args.start,end=args.end,
                cached_only=args.cached,policy_file=chosen_policy)
            command["warnings"].extend(report.get("warnings",[]))
        else:
            report=evaluate_trend(security.id,cached_only=args.cached,policy_file=chosen_policy)
        report.update(symbol=security.symbol,policy_file=str(chosen_policy),command=command)
        if args.strategy_check:
            logger.info("开始逐日检查独立买入策略：EMA回踩按波段和红带期限判断；MACD图标1出现即提示买入。只复用行情，不调用DeepSeek")
            report["strategy_results"]=band_strategies.evaluate_report(report,strategy_cfg)
            report["strategy_results"]["policy_file"]=str(strategy_policy)
            logger.info("策略计算完成：%s个买点，%s个技术退出，最近判断日%s",len(report["strategy_results"]["signals"]),len(report["strategy_results"]["exit_signals"]),report["actual_end"])
        text=dump(report)+"\n" if args.json else render_strategies(report, details=args.strategy_details) if args.strategy_check else render_futu_macd(report) if args.method=="futu-macd" else render_timeline(report) if args.start else render_text(report,args.history_limit)
        sys.stdout.write(text);sys.stdout.flush()
        if output:
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open("x",encoding="utf-8") as target:
                target.write(text)
            logger.info("趋势报告已保存：%s",output)
        if args.start:
            if args.method=="futu-macd":
                logger.info("富途判断完成：主趋势%s段、原色带%s段、%s个过滤后信号；震荡未评估",len(report.get("trend_intervals",[])),len(report["daily_intervals"]),len(report["signals"]))
            else:
                logger.info("历史分段完成：日线%s段、周线%s段；仅覆盖实际取得的数据",len(report["daily_intervals"]),len(report["weekly_intervals"]))
            return 2 if command["warnings"] else 0
        logger.info("趋势判断完成：日线%s/%s；周线%s/%s",report['daily']['trend_label'],report['daily']['status_label'],report['weekly']['trend_label'],report['weekly']['status_label'])
        return 2 if command["warnings"] or any(report[k]["assessment_status"] in ("STALE","INSUFFICIENT_DATA") for k in ("daily","weekly")) else 0
    except FileExistsError:
        logger.error("输出文件已存在，请更换--output文件名，不覆盖旧报告")
    except BrokenPipeError:
        return 0
    except (ValueError,LookupError,OSError) as exc:
        logger.error("无法完成趋势判断：%s",exc)
    except (KeyboardInterrupt,EOFError):
        logger.warning("已取消趋势判断，已有数据保留")
        return 130
    except Exception:
        logger.error("趋势处理未完成，请核对行情档案与配置；已保存数据保留")
    return 1


if __name__=="__main__":
    raise SystemExit(main())

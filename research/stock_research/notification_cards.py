"""飞书简洁研究卡：只展示决策摘要，完整口径及版本留本地档案。"""
from decimal import Decimal, InvalidOperation, localcontext
import re

FORMAT = "stock-brief-card-v1"


def _decimal(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,ValueError):
        return None


def _price(value):
    value = _decimal(value)
    return f"{value:,.2f}" if value is not None else "待补"


def _text(value, limit=100):
    # 外部名称不能变成Markdown格式、链接、@提及或飞书标签。
    value = re.sub(r"\s+"," ",str(value or "")).strip()[:limit]
    return value.translate(str.maketrans({"<":"＜",">":"＞","@":"＠","[":"［",
        "]":"］","*":"＊","_":"＿","`":"｀","#":"＃","~":"～","\\":"＼","|":"｜"}))


def _zone(zone):
    low, high = (_decimal(zone.get(k)) for k in ("quote_low","quote_high"))
    if low is None or high is None or not 0 < low <= high:
        return None
    strength = ({"strong":"强","medium":"一般","weak":"弱"}.get(zone.get("strength"),"待确认")
                if zone.get("role_status")=="confirmed" else "待确认")
    return f"**{_price(low)}—{_price(high)}**（{strength}）"


def summarize(report):
    """仅派生展示内容；不新增估值、策略、止损或基本面通过条件。"""
    current_valuation = report.get("automatic_valuation_reference") or {}; market = report.get("market") or {}
    technical = market.get("technical") or {}; cache = report.get("cache") or {}
    profile = report.get("profile") or {}
    current = cache.get("state")=="fresh" and not technical.get("stale") and technical.get("status")=="ready"
    zones = technical.get("zones",[]) if current else []
    levels = {}
    for kind in ("support","resistance"):
        # 原技术结果按距现价排序；各取最近一档，不用远处强区间替代近处候选。
        candidates = [z for z in zones if z.get("kind")==kind]
        levels[kind] = next((text for z in candidates if (text := _zone(z))),
            "待更新" if not current else "暂无有效区间")

    scenarios = current_valuation.get("scenarios") or []
    close = _decimal(market.get("last_price"))
    if scenarios:
        valuation = " · ".join(f"{row['name']} **{_price(row['price'])}**" for row in scenarios) + " USD"
        if current_valuation.get("corrected_fields"):
            valuation += " · 含网页更正"
        if current_valuation.get("stale"):
            valuation += " · 含旧数据"
        reason = "财年EPS与Forward PE口径待核对，当前区间仅作估值参考。"
    else:
        valuation = "估值资料待补" if current_valuation else "基金不适用公司EPS估值"
        reason = current_valuation.get("message") or valuation
    quote = f"{market.get('last_complete_session') or '日期待补'} 收盘 {_price(close)} {_text(profile.get('currency') or '币种待核对',10)}"
    if cache.get("state")!="fresh":
        quote += " · 历史参考/待更新"
    return {"format":FORMAT,"symbol":report["symbol"],"quote":quote,"valuation":valuation,
            "levels":levels,"fundamental_reason":reason,"quote_conversion_factor":technical.get("quote_conversion_factor")}


def card_text(card):
    """CLI展开新版卡片，同时兼容既有纯文本退出/测试卡及旧档案。"""
    lines = []
    for element in (card.get("body") or {}).get("elements",card.get("elements",[])):
        if element.get("tag")=="hr":
            continue
        content = element.get("content") if element.get("tag")=="markdown" else (element.get("text") or {}).get("content")
        if content:
            lines.append(content)
    return lines


def _stop(item, brief):
    signal = item["signal"]; plan = signal.get("exit_plan") or {}; levels = signal.get("levels") or {}
    stop = _decimal(plan.get("initial_protection_price"))
    entry = _decimal(plan.get("entry_signal_reference")) or _decimal(levels.get("entry_reference"))
    if stop is None or entry is None or not 0 < stop < entry:
        return "待确认（信号未形成有效保护线）"
    with localcontext() as ctx:
        ctx.prec = 48
        distance = (stop/entry-1)*100
        basis = (item.get("evaluation") or {}).get("price_field")
        factor = _decimal(brief.get("quote_conversion_factor")) if basis=="adjusted_close" else Decimal(1) if basis=="close" else None
        quoted = factor is not None and factor>0
        price = stop/factor if quoted else stop
    return f"**{_price(price)}（{distance:.2f}%）**" + ("" if quoted else " · 技术调整价")


def buy_card(symbol, research, *, item=None):
    """真实买点和无信号研究共用五项模板；手动快照不补造策略及止损。"""
    brief = (research or {}).get("brief") or {}; snapshot = item is None
    if snapshot:
        strategy = "研究快照（未评估买点）"
        stop = "未生成（本次未评估买点）"
        technical_reason = "本次未评估买点，仅展示当前支撑与压力。"
    else:
        signal = item["signal"]; cfg = item.get("evaluation",{}).get("strategy_configuration",{})
        strategy = _text(signal.get("name") or item["strategy_id"],60)
        stop = _stop(item,brief)
        if item["strategy_id"]=="EMA_PULLBACK":
            period = (cfg.get("pullback") or {}).get("ema_period",20)
            technical_reason = f"上涨高点回踩EMA{period}，次日开盘及收盘满足确认条件。"
        elif item["strategy_id"]=="MACD_ICON1":
            technical_reason = "MACD在零轴下金叉，触发红色箭头买点。"
        else:
            technical_reason = "策略买点条件已触发，按对应保护线跟踪。"
    levels = brief.get("levels") or {}
    sections = [f"**股票：{_text(symbol,15)}**\n{strategy}",
        f"**止损位**\n{stop}",
        "**基本面估值**\n"+brief.get("valuation","研究未完成，待补齐"),
        "**技术面**\n支撑 "+levels.get("support","待补")+"\n压力 "+levels.get("resistance","待补"),
        ("**研究结论**" if snapshot else "**买入依据**")+"\n技术面："+technical_reason+"\n基本面："+brief.get("fundamental_reason","研究未完成，暂无基本面确认。")]
    elements = []
    for index,content in enumerate(sections):
        if index in (2,4):
            elements.append({"tag":"hr"})
        elements.append({"tag":"markdown","content":content})
    elements.append({"tag":"markdown","content":_text(brief.get("quote") or (f"信号日 {item['as_of']}" if item else "行情待补"))+" · 人工决策"})
    return {"schema":"2.0","header":{"template":"blue" if snapshot else "green",
        "title":{"tag":"plain_text","content":f"股票研究 · {symbol}" if snapshot else f"股票买点 · {symbol}"}},
        "body":{"elements":elements}}

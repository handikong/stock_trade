"""技术区间与趋势共用的价格特征；只接收当时已经完成的K线。"""


def wilder_atr(bars, window):
    if len(bars)<window+1:
        return None
    closes=[float(b["adjusted_close"]) for b in bars]
    tr=[max(float(bars[i]["adjusted_high"])-float(bars[i]["adjusted_low"]),
            abs(float(bars[i]["adjusted_high"])-closes[i-1]),
            abs(float(bars[i]["adjusted_low"])-closes[i-1])) for i in range(1,len(bars))]
    value=sum(tr[:window])/window
    for item in tr[window:]:
        value=(value*(window-1)+item)/window
    return value


def confirmed_pivots(bars, left, right, lookback):
    highs=[float(b["adjusted_high"]) for b in bars]
    lows=[float(b["adjusted_low"]) for b in bars]
    points=[]
    for i in range(max(left,len(bars)-lookback),len(bars)-right):
        for values,kind,extreme in ((highs,"high",max),(lows,"low",min)):
            window=values[i-left:i+right+1]
            # 平台仅取最早极值；右侧确认线属于传入的完整历史，不能提前引用。
            if values[i]==extreme(window) and window.index(values[i])==left:
                points.append({"price":values[i],"kind":kind,"index":i,"date":bars[i]["date"],
                    "confirmed_at":bars[i+right]["date"],"volume":bars[i].get("volume",0)})
    return points

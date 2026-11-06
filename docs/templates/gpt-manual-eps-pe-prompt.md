# 手工EPS＋PE提问模板

复制下方提问及JSON给联网大模型，替换股票即可；仅返回七个字段，行业PE可null。系统不自动获取PE，不因缺失回退网站或旧缓存。输入数值为用户研究假设，来源真实性仍由本人核对。

---

请联网整理Alphabet（GOOG）的预测EPS和估值PE，供我手工录入程序计算EPS×PE。请只返回下方JSON，不加Markdown、长篇解释或额外字段。

- EPS：给出从本次查询日起未来12个月（NTM）的保守、基准、乐观三种研究情景，单位美元/股，三值采用同一盈利口径和期间，保守≤基准≤乐观。不把下一财年EPS直接冒充NTM。
- 历史PE：查询近5年的历史Forward PE，优先与上述EPS相同盈利口径和NTM定义，pe_low/pe_median/pe_high分别填写25%、50%、75%分位数。不要将当前PE、平均数、历史最小/最大或走势图目测值冒充这些分位数。无法核实就填null，不编造。
- 行业PE：填写与谷歌业务可比行业、尽可能同一盈利口径的Forward PE参照。不能用大盘PE代替，不能把整个科技板块直接当作谷歌的同行组；来源/分类/日期不能核实时填null。该项是可选比较依据。
- 所有数值用十进制字符串，无美元符号、逗号或“倍”；缺值用JSON null，不用0、空字符串或N/A。EPS和PE口径无法保持一致时，对应PE留null。不要反推PE来凑预设股价，不返回DCF、股价目标或买卖指令。

返回结构：

```json
{
  "eps_conservative": null,
  "eps_base": null,
  "eps_optimistic": null,
  "pe_low": null,
  "pe_median": null,
  "pe_high": null,
  "industry_pe": null
}
```

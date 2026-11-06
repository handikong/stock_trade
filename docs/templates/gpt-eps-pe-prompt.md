# 精简提问模板：预测EPS与历史PE

2026-11-05最新默认为[手工EPS＋PE七字段提问](gpt-manual-eps-pe-prompt.md)，自动PE已停用；下方v2保留兼容。

2026-11-05：默认改用[仅三个EPS的提问格式](gpt-eps-only-prompt.md)。下方v2仅保留兼容，不再要求每次询问PE。

以下分隔线后的内容已包含完整返回JSON，可一次复制给支持联网的大模型，修改股票即可。使用实际查询日期。本文件没有真实GOOG预测数据；旧完整v1继续兼容，新格式不要求DCF资料。

---

请联网查询 Alphabet（GOOG）的预测EPS和历史PE，供我核对后导入估值程序。查询截止日使用本次实际日期，填入as_of。

只返回下面结构的一个合法JSON对象，不加Markdown或额外解释。程序负责计算EPS×PE，不需要输出股价目标、技术支撑压力、DCF或买卖建议。

要求：

1. **预测EPS**：优先查询下一完整财年的分析师最低、平均、最高EPS预测，period_type填fy1，注明实际财年起止日期。三值须来自同一来源、同一期间、同一盈利口径。只有平均值时，填mean，low/high都填null，不用增长率或百分比编造范围。不能把中位数冒充平均值。也可使用明确的未来12个月滚动预测ntm，起止日为as_of次日至一年后的对应日期；不能把财年预测改名NTM。
2. **历史PE**：优先查询近5年、与预测EPS盈利口径相同的历史Forward PE，eps_reference如实填fy1或ntm。优先返回25%、50%、75%分位数，statistic填quartiles，分别放入low/median/high。没有分位数时，可提供有依据的历史最小值、中位数、最大值，statistic填min_median_max。给出真实统计起止日，note简短注明采样频率、异常/非正盈利样本处理及限制。只有当前PE、历史均值或走势图印象，不足以填出这三个统计值。
3. **可获取性**：找不到历史Forward PE但能核实历史TTM PE范围时，可以返回ttm，并在note说明差异；程序仅作为跨口径倍数假设，不作为同口径估值确认。只有3年历史就注明真实3年，不伪装5年。无法核实的值填null，不估造历史统计。
4. **盈利口径**：basis和earnings_basis只能为gaap_diluted、adjusted_diluted、normalized_diluted，EPS与PE必须相同。正常化盈利的调整依据在notes简述。找不到兼容口径时保留null，不混用GAAP和调整后数据。
5. **来源**：EPS与PE分别引用source_ids。sources只列实际读到且支持数值的HTTPS页面，填写id、title、url、updated_at（数据更新时间，未知null）。不能把链接存在当成已核实数字；遇付费墙/无法读取就保留null，不凭记忆或搜索标题补数。
6. **格式**：所有数值使用十进制字符串，缺值为JSON null，不用0、空串或N/A。日期YYYY-MM-DD，币种USD，notes最多5条简短备注。不增加字段，不替我设置确认状态。

按下面结构替换已核实数据。S1/S2是结构占位，必须改成实际来源及对应引用；没有来源就用空数组：

```json
{
  "schema_version": "manual-valuation-v2",
  "symbol": "GOOG",
  "as_of": null,
  "currency": "USD",
  "security_type": "EQUITY",
  "eps": {
    "period_type": "fy1",
    "period_start": null,
    "period_end": null,
    "basis": null,
    "low": null,
    "mean": null,
    "high": null,
    "source_ids": ["S1"]
  },
  "historical_pe": {
    "period_start": null,
    "period_end": null,
    "eps_reference": null,
    "earnings_basis": null,
    "statistic": null,
    "low": null,
    "median": null,
    "high": null,
    "note": null,
    "source_ids": ["S2"]
  },
  "sources": [
    {"id": "S1", "title": "实际预测EPS来源标题", "url": "实际HTTPS链接", "updated_at": null},
    {"id": "S2", "title": "实际历史PE来源标题", "url": "实际HTTPS链接", "updated_at": null}
  ],
  "notes": []
}
```

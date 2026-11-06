# 复制给GPT的个股估值资料询问模板

2026-11-05最新默认：[手工EPS＋PE七字段提问及JSON](gpt-manual-eps-pe-prompt.md)。本完整v1继续兼容，自动PE已停用。

2026-11-05默认只需[三个EPS的JSON](gpt-eps-only-prompt.md)，PE由程序独立获取。以下完整旧格式保留兼容。

2026-11-03：当前优先使用[精简EPS与历史PE提问模板（内含JSON）](gpt-eps-pe-prompt.md)。下方完整v1保留兼容，不再作为默认提问模板；新v2不要求DCF和折算率。

使用时先填写下面的股票、查询日、目标估值日，并把同目录`manual-valuation.template.json`全文附在提问后。后台已支持该JSON的校验、草稿保存和显式确认，网页粘贴入口后续增加；不可粘贴到旧EPS逐项表单中。

---

请协助我整理一份供本人核对、随后手工导入股票研究系统的财务估值输入。系统不再读取财报、电话会议或网站摘要；你提供有依据的输入，EPS×PE、DCF以及K线支撑压力仍由程序计算。

- 股票代码：【填写，例如GOOG】
- 查询截止日期：【填写YYYY-MM-DD】
- 目标估值日期：【填写YYYY-MM-DD，通常约一年后；必须明确日期】
- 币种：【填写，例如USD】
- 目标EPS采用的口径：【填写，例如GAAP diluted；如需正常化口径，请明确写normalized diluted并说明调整】

请联网核对公司投资者关系页面、SEC披露及可公开访问的有署名/有日期的研究资料。若本次不能联网、资料不可访问或无法核实，就返回null并说明，不能依靠记忆或编造链接补齐。

请遵循这些要求：

1. **实际值、预期、研究假设分开。** current_ttm只放截至查询日已披露的真实TTM稀释EPS。observed_pe只放有时点和口径的已观察市场PE；目标情景中的pe是估值假设，不能把两者混成同一字段。
2. **明确EPS期间。** 所有TTM/预测EPS必须给出开始日、结束日。target.eps_period_type只能使用target_ttm、ntm、fy1：target_ttm指目标估值日之前12个月；ntm指查询日之后12个月；fy1指供应商所定义的下一财年、必须给出财年起止。不知道准确期间就保持null，不得把FY1直接改名为NTM或目标TTM。
3. **口径保持一致。** 区分GAAP/non-GAAP/normalized、basic/diluted。三个目标情景必须采用同一期间、同一EPS口径。优先股股息、可转优先股、股份稀释、拆股需要核对，不要简单用公司总净利润除一个期末股数代替稀释EPS。净亏损或EPS非正时如实报告，不强造正PE。
4. **核对盈利质量。** 单列证券未实现收益、资产处置、税项、重组等重大非经常因素及其来源。没有有依据的税后调整，不要自行扣除税前收益来伪造正常化EPS。无法给出可信正常化EPS时，normalization_note说明限制，对应值保持null。
5. **目标情景是有依据的假设。** 分别整理保守、基准、乐观EPS及目标PE。eps_origin只能为analyst_consensus、company_guidance或research_assumption；如果某个情景由你推演，必须填research_assumption，写出计算/调整依据，不要伪装成机构一致预期或公司指引。不必为填满三情景而造数。
6. **PE依据要可核对。** 解释使用的历史区间/同行及其时期、对应盈利口径、异常盈利的影响；不要把过去PE分位当成未来合理PE的证明。来源不足以支持目标PE时返回null。若observed_pe缺少可对应的股价、EPS或时间，也可留空。
7. **只给输入，不计算目标股价。** 不返回买卖指令、成功概率或保证收益。EPS×PE由我的系统用Decimal计算，系统不会把情景当成一年内实际最高/最低价格。
8. **来源与缺失透明。** 每条来源包含id、title、publisher、url、published_at、accessed_at和supports；发布时间未知就null，访问时间用本次真实访问时间。supports写该页面实际支持的字段或结论。事实、预期和情景引用source_ids，且引用必须存在于sources中。链接存在不代表数字已核实，不能只看搜索标题便认定已取得资料。
9. **严格JSON。** 按我附的manual-valuation-v1模板输出一个JSON对象，不加Markdown围栏，不在前后附解释。日期YYYY-MM-DD；时间使用含时区ISO8601；数值用十进制字符串，不加逗号、货币符号、倍或百分号；EPS单位为每股报价币种、PE为倍数。缺值用JSON null，不用0、空串或“N/A”。缺少字段列入missing_fields，限制列入risks。不得替我设置“已人工确认”。
10. **DCF给输入路径，不给成品价格。** 仅适用已确认非金融经营公司FCFF时提供dcf，否则dcf=null。提供2至15年完整的EBIT、税率、折旧摊销da、资本开支capex、经营营运资本增加delta_wc，以及WACC、长期增长、非经营现金、债务、其他优先索取权、稀释股数、依据与稳定期/股票薪酬处理。路径从as_of资料日开始，第一年是此后的12个月。金额与股数均用原币单位/原股数，或一致百万单位/百万股，不能混尺度；税率、WACC、增长率用小数字符串。不能把缺失债务/摊销填0。若提供一年后价值，额外给完整year_one_state现金、债务、优先索取权、股数；没有就null。示例years中两行只是空白占位，请按实际有依据的路径填写，不必只提供两年，不得为满足行数编造。预测路径、增长及折现率属于假设，必须说明来源或推演过程。若只能取得部分DCF资料，就保留部分字段与null供补齐，不能擅自补假设。
11. **不输出支撑压力成品。** 系统继续从K线计算技术区间，你只整理财务和估值输入，不用财报推断技术支撑压力。
12. **声明DCF数量级。** amount_scale和shares_scale都填units（原币单位/原股数）或都填millions（百万币种/百万股），不能一边亿、一边百万。无法核对时填null；系统不会自动猜测换算。
13. **技术与基本面结合的折算输入。** technical_alignment可留空。只有能给出有来源或明确推演依据、供本人确认的股权年度要求收益率时，才填写annual_required_return（0至1之间小数字符串、不含1）、source_note、source_ids。它用于将未来EPS×PE终点价格折算为当前价格参照，不是企业FCFF的WACC，不能直接抄用WACC。如果明确采用不含期间分红的终点股价口径，cash_flow_treatment填写terminal_price_only_excluding_dividends；这并不表示公司不分红，也不是包含所有分红的总回报现值。无法支持这一口径或缺少依据时各字段填null，系统会提示模型尚不能参与当前支撑压力判断，不能为了凑共振制造折现率。

sources条目结构：

```json
{
  "id": "S1",
  "title": "实际页面标题",
  "publisher": "实际发布者",
  "url": "实际公开来源链接",
  "published_at": null,
  "accessed_at": null,
  "supports": ["current_ttm.eps"]
}
```

请保留缺失和不确定性，即使最后无法提供完整估值情景，也不要为了返回完整数据而猜测。

下面是输出结构，请替换symbol、日期及已核实内容，保留无法取得项的null：

【在此粘贴manual-valuation.template.json全文】

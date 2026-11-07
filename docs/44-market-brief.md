# 大盘日报、周一开盘计划和周报

按北京时间周一至周六07:00入队，12:00前可补跑；周日休息。复用现有单进程后台及交易所交易日历。串行Worker正执行其他任务时，实际开始时间会顺延。周一早上用上个美股交易日的完整日线并纳入周末消息；周二至周五用最近完整日线，遇到休市明确标注沿用。周六用美股本周最后一个完整交易日，汇总实际交易日数、周区间、周收益以及已确认或失效的结构变化。每份报告同时标明北京时间报告日、美股数据日与下一交易日。

本模块仅处理大盘，使用独立任务和报告版本，不依赖股票池，既有个股扫描和推送规则照常运行。

## 证据和执行条件

- **方向**：SPY、QQQ 同在 EMA20/EMA50 上方且 EMA20 较五个交易日前上行才判整体偏强；两者都在均线下且 EMA20 下行判偏弱，其余判分化或过渡。另展示已完成周线的 EMA20 方向。基准行情复用已有完整日线及技术区间，缺目标日才补。用调整价计算，展示价位换算回对应交易日的报价尺度。
- **结构**：前高前低、两条均线、已确认高低点连出的趋势线以及历史结构区间，候选线以 0.15 ATR 为默认缓冲。展示最靠近现价的支撑和压力及近期关键变化。每天用前一交易日已知的水平判本日越线，随后只用已经完成的日线判断回踩、反抽、继续收盘确认或失效。两点趋势线只是结构候选。
- **量价**：相对过去 20 个常规完整交易日均量，默认大于等于 1.25 倍是放量、小于等于 0.80 倍是缩量。结合收盘在日内的位置、突破后偏离区间的 ATR 倍数，区分等待确认、等待回踩、观察试仓或加仓条件。成交量是 ETF 自身成交量；缩短交易日单独标记，不参与普通量比。
- **参与度**：RSP/SPY 五个交易日相对变化，高于 +0.30% 提示等权领先，低于 −0.30% 提示落后。日期须对齐，这是等权表现代理指标。 [RSP 官方资料](https://www.invesco.com/us/financial-products/etfs/product-detail?productId=RSP)
- **波动风险**：ATR14 描述价格波动；VIX 收盘日涨幅达 10% 或收盘达此前 20 日中位数的 1.2 倍，提示波动预期升高，并建议等待价格确认。VIX 只调整观察节奏。要求目标日及前 20 个交易日都有对应值。 [Cboe 历史日线与定义](https://www.cboe.com/tradable-products/vix/vix-historical-data)

`MARKET_BRIEF_SLOPE_SESSIONS`（5）、`MARKET_BRIEF_VOLUME_HIGH`（1.25）、`MARKET_BRIEF_VOLUME_LOW`（0.80）、`MARKET_BRIEF_LEVEL_BUFFER_ATR`（0.15）为可调初值。其他阈值是报告 v1 规则。缺核心 SPY 或 QQQ 时标资料待补；其他来源失败继续发布已知证据。

## 新闻与 DeepSeek

首版自动读美联储货币政策、CPI 与美国就业报告的官方发布源，并标出下一交易日或下周的 CPI、就业报告和 FOMC 日程。正文使用标题与有限节选。BLS 的明确发布时间转北京时间，Fed 日历仅提供日期的会议只列美东日期。每条有来源链接；来源失败明确标注。 [Fed RSS](https://www.federalreserve.gov/feeds/feeds.htm) · [FOMC 日历](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) · [BLS RSS](https://www.bls.gov/feed/) · [BLS 日程](https://www.bls.gov/schedule/news_release/cpi.htm)

每份报告单独请求一次 DeepSeek（配置 `DEEPSEEK_API_KEY`，沿用 `DEEPSEEK_MODEL`），只发送压缩后的证据和已核实来源，结构化输出不超过 1100 token。程序校验 JSON、来源编号和模型新增的数字或链接；价格和风险条件来自程序本身。报告保存模型名称、消耗与分析状态。缺密钥、请求失败、结果不合格时仍能看程序报告。任务中断后只恢复未执行步骤，结果未知的模型请求和飞书投递不自动重复。 [DeepSeek JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)

## 查看和部署

```bash
bash update.sh
.venv/bin/python -m stock_research.schedule_cli market-brief
.venv/bin/python -m stock_research.schedule_cli market-brief --report weekly
.venv/bin/python -m stock_research.schedule_cli market-brief --date 2026-11-07 --json
# 即时预览，可联网补基准行情、读取官方来源并调用模型；不发送或写正式简报档案
.venv/bin/python -m stock_research.schedule_cli market-brief --preview
```

报告开关与时间位于`config/post-close-schedule.json`（`market_brief_enabled`、`market_brief_time`、`market_brief_until`）。使用自定义`POST_CLOSE_SCHEDULE_FILE`的服务器需加入对应字段。简报飞书有单独的`market_brief_feishu_enabled`，默认关闭；启用后沿用现有飞书机器人地址。服务器的密钥仅放在 `.env`。

## 用户验收

- MB01 周一沿用上个收盘、正常周六纳入周五、短周只汇总实际交易日、周日休息。
- MB02 两个基准按同一目标日判断，缺核心行情降级，RSP/VIX 缺项不生成虚假读数。
- MB03 收盘突破、次日回踩、失效分别展示；周报包含历史状态变化和可复核价位。
- MB04 已发布新闻和未来事件带来源与时间；数据源不可用时有提示。
- MB05 模型只用已有证据，超时或格式异常仍显示程序结论；保存报告只读查询。
- MB06 同一天重启不创建第二份报告，结果未知的请求不自动重发；简报飞书单独控制。

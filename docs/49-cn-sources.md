# A 股关注池与估值来源核对

## 人气榜与入池

按东方财富 A 股人气榜的前 100 条取样。榜单代表站内访问、关注和讨论热度。代码核对排名完整且唯一，再从同一来源读取沪深候选的股票简称及人民币总市值。新增时排除北交所、基金、B 股、ST 和退市整理股。同股按带交易所后缀的代码去重，昨日入池的股票落榜后继续保留。若请求、榜单校验或简称核对失败，保留原关注池。 [东方财富榜单及规则](https://guba.eastmoney.com/rank/)

默认每日北京时间 16:30 起运行，19:00 结束当天补跑窗口，包含休市日。同日成功只入池一次。开关为 `config/post-close-schedule.json` 的 `cn_heat_enabled`，时间可调。工作台顶部「A 股」展示该名单、最近上榜时间及人民币总市值排序。市值取该股票最近一次上榜时的报价快照，缺值放最后。美股扫描仍从原美股名单读取。

```bash
# 查看上游当下榜单
.venv/bin/python -m stock_research.cn_cli preview
# 同步今天的榜单并更新独立关注池，同日成功后复用档案
.venv/bin/python -m stock_research.cn_cli sync
# 查看已保存的独立关注池
.venv/bin/python -m stock_research.cn_cli pool
```

## EPS 和 PE

- **已披露 EPS**可以从公司定期报告或 Tushare 的利润表、财务指标获得，保留报告期和披露日。 [Tushare 财务指标](https://tushare.pro/document/2?doc_id=79)
- **财年预测 EPS**先使用 Stock Analysis 的公开表，读取人民币每股的低、均、高预测及财年截止日。茅台和宁德时代可见三档。 [贵州茅台预测](https://stockanalysis.com/quote/sha/600519/forecast/) · [宁德时代预测](https://stockanalysis.com/quote/she/300750/forecast/)
- **历史 PE**分开读取历史 PE Ratio 和历史 Forward PE。年度表用于核对历史记录；估值仍沿用美股的季度 Forward PE 正值样本至少八个、近五年 P25/P50/P75 口径。只取得年度表、预测期不明或季度样本不足时保留缺项。 [贵州茅台估值倍数](https://stockanalysis.com/quote/sha/600519/financials/ratios/) · [宁德时代估值倍数](https://stockanalysis.com/quote/she/300750/financials/ratios/)

查询命令只读取公开资料，输出来源、样本数、缺项及可算时的人民币三档估值。估值是财年预测情景，Forward PE 分母口径待核对。

```bash
.venv/bin/python -m stock_research.cn_cli research 600519.SH
.venv/bin/python -m stock_research.cn_cli research 300750.SZ
```

这一阶段交付关注池、每日同步和估值核对入口。后续接 A 股收盘日线缓存、研究页、策略扫描、飞书和人民币仓位。日历、复权、停牌、价格限制及成交模拟需要各自按 A 股口径处理。 [上交所交易规则](https://big5.sse.com.cn/site/cht/www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)

## 用户验收

- CN01 预览完整 100 个榜位，核对沪深代码及过滤结果。
- CN02 同步两次，第二次应复用当天档案。核对网页与 CLI 名单、排序和美股名单隔离。
- CN03 核对两只样本的人民币 EPS、历史 PE 区分、季度样本数与缺项提示。
- CN04 核对北京时间 16:30 的后台任务及失败保留旧池。真实抓取与网页验收由用户执行。

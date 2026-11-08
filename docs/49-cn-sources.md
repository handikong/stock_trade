# A 股收息关注池与估值来源

## 两类关注名单

- **高息股候选**从中证红利指数（000922）成分中筛选沪深普通 A 股，排除成分表中简称标为 ST 或退市整理的股票。页面展示成分表日期和入池依据。用户也可手动添加自己核对过分红的沪深股票。 [中证红利指数](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000922factsheet.pdf)
- **大盘宽基 ETF**按已核对的跟踪指数分类。默认目录含沪深300 ETF（510300.SH）、上证50 ETF（510050.SH）和中证A500 ETF（512050.SH）。页面可移除及重新添加目录内的基金。目录可在 `config/cn-income-etfs.json` 维护。 [上交所沪深300 ETF](https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=510300) · [华夏上证50 ETF](https://en.chinaamc.com/etf-connect/50etf/) · [华夏中证A500 ETF](https://fund.chinaamc.com/fund/512050/)

工作台顶部「A 股」可手动更新中证红利。程序读取 [中证指数成分文件](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000922cons.xls)，核对完整 100 条、指数代码、唯一证券代码、成分表日期及沪深交易所。下载失败、文件异常或日期倒退时保留旧名单。每次成功更新替换指数候选，手工添加或移除优先。已离开指数的股票可以手动保留。

东方财富人气榜、原 16:30 定时任务及旧 `preview`、`sync` 命令均已停用。历史热榜档案独立保留。新的收息关注池使用独立记录，美股扫描读取美股名单。

```bash
# 手动更新中证红利成分
.venv/bin/python -m stock_research.cn_cli refresh-dividends
# 查看两类关注名单
.venv/bin/python -m stock_research.cn_cli pool
```

首次部署收息池或从热榜版本升级时，使用 `bash deploy.sh` 安装 XLS 读取依赖。

## EPS 和 PE

- **已披露 EPS**可以从公司定期报告或 Tushare 的利润表、财务指标获得，保留报告期和披露日。 [Tushare 财务指标](https://tushare.pro/document/2?doc_id=79)
- **财年预测 EPS**先使用 Stock Analysis 的公开表，读取人民币每股的低、均、高预测及财年截止日。茅台和宁德时代可见三档。 [贵州茅台预测](https://stockanalysis.com/quote/sha/600519/forecast/) · [宁德时代预测](https://stockanalysis.com/quote/she/300750/forecast/)
- **历史 PE**分开读取历史 PE Ratio 和历史 Forward PE。年度表用于核对历史记录；估值仍沿用美股的季度 Forward PE 正值样本至少八个、近五年 P25/P50/P75 口径。只取得年度表、预测期不明或季度样本不足时保留缺项。 [贵州茅台估值倍数](https://stockanalysis.com/quote/sha/600519/financials/ratios/) · [宁德时代估值倍数](https://stockanalysis.com/quote/she/300750/financials/ratios/)

查询命令只读取公开资料，输出来源、样本数、缺项及可算时的人民币三档估值。估值是财年预测情景，Forward PE 分母口径待核对。

```bash
.venv/bin/python -m stock_research.cn_cli research 600519.SH
.venv/bin/python -m stock_research.cn_cli research 300750.SZ
```

估值查询适用于沪深普通股票。买点判断留待后续规则确认，关注池当前展示候选和来源。后续评估高息股时，可以用已实施的近 12 个月普通现金分红和未复权收盘价计算历史股息率，再结合分红持续性、估值和技术位置。ETF 的实际分红按基金公告独立核对。

## 用户验收

- CN01 首次打开 A 股，核对默认三只 ETF 的跟踪指数。手动更新中证红利后，核对成分表日期和沪深候选。
- CN02 移除一只成分股，手动添加一只已核对的股票，再次更新。核对移除继续生效、手工股仍在、网页和 CLI 名单一致。
- CN03 核对两只样本的人民币 EPS、历史 PE 区分、季度样本数与缺项提示。
- CN04 核对下载失败时保留原候选，旧热榜命令提示停用，A 股名单和美股名单互相独立。
- CN05 从目录移除并重新添加宽基 ETF。核对缺少目录条目的基金无法通过宽基入口加入。

真实抓取与网页验收由用户执行。

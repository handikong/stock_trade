# A 股持仓、收息关注池与估值来源

## 人民币持仓

顶部切换到「A 股」后进入「持仓总览」，同一市场的侧栏可打开「收息关注池」。持仓支持手动添加、编辑和移除沪深普通 A 股及支持代码段内的 ETF。可以录入券商显示的裸代码、沪深前缀或交易所后缀。系统统一存为六位代码加 `.SH` 或 `.SZ`。名称与备注可选，数量按实际股数或份数填正整数，成本均价使用人民币。ETF 的持仓登记独立于收息池的宽基目录。 [上交所代码段分配指南](https://www.sse.com.cn/lawandrules/guide2025/stock/jyglywznylc/zn/c/c_20260831_10830433.shtml) · [深交所证券代码区间表](https://www.szse.cn/marketServices/technicalservice/doc/P020240510623040090566.pdf)

页面按数量 × 成本均价计算单只和全部持仓成本。行情、市值与浮动盈亏尚未接入。A 股持仓与美股持仓、美元现金、风险预算、策略扫描和盘中订阅分别处理。添加、编辑、移除会保留变更快照，编辑和移除须使用最新版本。

服务启动时自动创建 `cn_manual_positions` 和 `cn_position_changes` 两张新表。已部署且装有收息关注池依赖的服务器可以直接运行 `bash update.sh`。A 股持仓通过 `/api/v1/cn/holdings` 独立读写，编辑或移除时在路径末尾附持仓 ID。

## 两类关注名单

- **高息股候选**从中证红利指数（000922）成分中筛选沪深普通 A 股，排除成分表中简称标为 ST 或退市整理的股票。页面展示成分表日期和入池依据。用户也可手动添加自己核对过分红的沪深股票。 [中证红利指数](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000922factsheet.pdf)
- **大盘宽基 ETF**按已核对的跟踪指数分类。默认目录含沪深300 ETF（510300.SH）、上证50 ETF（510050.SH）和中证A500 ETF（512050.SH）。页面可移除及重新添加目录内的基金。目录可在 `config/cn-income-etfs.json` 维护。 [上交所沪深300 ETF](https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=510300) · [华夏上证50 ETF](https://en.chinaamc.com/etf-connect/50etf/) · [华夏中证A500 ETF](https://fund.chinaamc.com/fund/512050/)

在 A 股侧栏打开「收息关注池」可手动更新中证红利。程序先核对完整 100 条、指数代码、唯一证券代码和成分表日期，再按中英文交易所名称筛选。北交所成分计入完整性核对，导入时跳过并显示数量。下载失败、交易所无法识别或中英文名称冲突、沪深代码与交易所不一致、文件异常或日期倒退时保留旧名单。每次成功更新替换指数候选，手工添加或移除优先。已离开指数的股票可以手动保留。 [中证指数成分文件](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000922cons.xls)

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

- CN01 打开 A 股「收息关注池」，核对默认三只 ETF 的跟踪指数。手动更新中证红利后，核对成分表日期和沪深候选。
- CN02 移除一只成分股，手动添加一只已核对的股票，再次更新。核对移除继续生效、手工股仍在、网页和 CLI 名单一致。
- CN03 核对两只样本的人民币 EPS、历史 PE 区分、季度样本数与缺项提示。
- CN04 核对下载失败时保留原候选，旧热榜命令提示停用，A 股名单和美股名单互相独立。
- CN05 从目录移除并重新添加宽基 ETF。核对缺少目录条目的基金无法通过宽基入口加入。
- CN06 切换到 A 股，分别登记一个沪深股票和一个 ETF，核对人民币成本和两市场的持仓数量。切回美股核对原账户、持仓与关注池。
- CN07 编辑、移除并重新添加一只 A 股持仓，刷新页面核对保存状态。用两个窗口编辑同一条记录，核对旧版本被拒绝。
- CN08 核对重复代码、数量为零或小数、错误交易所后缀和美元股票代码被拒绝。核对 A 股持仓与收息关注池可分别增删。
- CN09 导入含北交所成分的完整中证红利成分表，核对只导入符合范围的沪深股票，完成提示显示北交所跳过数。2026-11-06 官方表含沪市 77 只、深市 22 只、北交所 1 只（同力股份 920599）。

真实抓取与网页验收由用户执行。

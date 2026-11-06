# 研究数据获取修复与剩余限制

## 最新实际抓取：7897代理下刷新成功

2026-10-24用户明确提供本机HTTP代理并授权重新获取AAPL。已执行以下命令，完成于14:49:06（Asia/Shanghai），退出码0，`command.warnings=[]`，本次未调用DeepSeek、未获取电话会议。

```bash
http_proxy="http://127.0.0.1:7897" https_proxy="http://127.0.0.1:7897" .venv/bin/stock-research AAPL --force --json --output data/AAPL-proxy-review-20261024.json
```

报告：`data/AAPL-proxy-review-20261024.json`。命令进程设置上述代理环境变量，Yahoo SDK可读取；SEC原文客户端仍按现有`trust_env=False`实现直连，本轮也成功，不能将此记录解释为所有来源均通过代理。

| 区块 | 本次取得的结果 |
| --- | --- |
| 日线 | 754条，2023-10-23至2026-10-23，完整交易日缺口0，缓存fresh |
| 行情附带元数据 | EQUITY、USD、NasdaqGS，未执行独立get_info查询 |
| 技术结构 | ready；MA20/50/200、ATR及9个区间已计算；8个support、1个inside，本窗口未识别独立resistance，不补造压力位 |
| 财务与披露 | 2026-06-27和2026-03-28两期；25类66条事实、两份10-Q原文 |
| 业绩附件 | 两份EX-99原文重新下载成功，内容去重后沿用档案ID；不是新增两份不同附件 |
| EPS一致预期 | 0q、+1q、0y、+1y四个期间，含平均/高/低、覆盖分析师数量 |
| 日程 | Yahoo返回2026-11-03，confirmed=false；原样记录供应商值，尚未核实公司日期与时区 |
| EPS修订与趋势 | 各4个期间；趋势含当前与7/30/60/90天前数值，是本次供应商回溯字段，不是系统当时冻结的历史快照 |
| 管理层指引 | guidance=null，候选items=[]；已取得附件中未找到可核对的经营指引，不代表公司没有在其他渠道披露 |
| 电话会 | disabled，calls_required=false，不列缺失项，本次无转录请求 |

仍需补充或核对：

1. 管理层经营指引原文。当前自动来源仅SEC附件，独立公司IR材料仍需导入；不重新获取电话会。
2. 明确财报日期/时间及目标财报期，并在公布前冻结当前预期；`earnings`和`preview`均为空。不补造过往历史冻结记录。
3. EPS预期仍只有供应商0q/0y等相对标签，未保存明确目标期末/GAAP或non-GAAP口径；日程中的Earnings Average=1.98187与分析师表0q avg=1.97817略有差异，保留各自来源，不视作同一已确认数字。尚不能不经核对就直接代入估值或计算历史beat/miss。
4. 营收一致预期目前只取得日程中的当前财报事件平均/高/低，未抓取独立多期间营收预期表。未结构化保存全部业务分部、公司KPI及non-GAAP调节表；财报原文保留，不等于这些字段已经提取完成。
5. 两期CFO、capex、折旧摊销等仍为财年累计值，未形成完整年度/TTM及经营营运资本桥接；未来现金流路径、PE、WACC、长期增长率和情景依据尚需定义，`valuations=[]`。不将两季直接年化或自动猜测假设。
6. DeepSeek分析尚未显式请求，`analysis=null`属于未执行，不是接口失败。FMP备用来源未启用不再阻碍本轮Yahoo抓取。

本节为用户授权的真实数据刷新与只读结果核对，不代表所有分支、AI分析或整体验收通过。下面保留历次变更及失败记录，不能继续将此前Yahoo限流当作当前抓取结果。

## 最新变更：取消电话会议获取，保留管理层指引研究

用户于2026-10-24随后取消电话会议获取。当前普通刷新只获取SEC业绩EX-99附件与指引候选片段，不扫描转录链接、不请求FMP电话会接口。电话会状态为`disabled`，不作为缺失或阻塞项；已有和主动导入的材料保留。下文电话会自动获取与真实执行结果属于此前版本的历史记录。

管理层指引可以来自公司官方发布稿、演示稿及财务补充。当前自动来源仅SEC附件，独立IR材料可手工导入，尚无通用IR抓取器。关键词定位和显式DeepSeek分析均为待核对草稿，不能把一致预期、历史业绩或通用前瞻免责声明当作经营指引。已存AAPL两期发布稿未找到下季营收/EPS等明确经营指引，保持“已获取材料中未取得”；不宣称公司没有在其他渠道披露。

本次代码同步调整材料缓存策略版本、研究状态、命令行/页面提示及AI提示版本，仅阅读代码与核对已有原文，未运行应用或请求模型；由用户运行`stock-research AAPL --force`验收。

更新日期：2026-10-24。用户要求移除独立身份查询、修复剩余问题，并说明不能完成的原因。延续用户此前授权的AAPL真实强制抓取诊断；没有新建测试文件、测试脚本或运行测试套件，没有调用DeepSeek。

## 本次修复

1. 移除`get_info()`及独立证券简介查询。直接获取日线，使用响应附带的币种/交易时区/证券类型；不为补简介请求quoteSummary。0.2.x适配层提供已知美股时区以阻止SDK探测失败时回退info，并读取日线已载入的元数据，避免公开metadata方法额外获取分时数据。yfinance约束为`>=0.2.66,<0.3`；未知币种不默认为USD。
2. Yahoo限流单独分类，记录全局冷却到期时间，默认120秒，可配置`YAHOO_COOLDOWN_SECONDS`。冷却期不反复请求同一提供方，其他来源继续执行；`--force`跳过业务缓存有效期，但不跳过供应商冷却，也不通过换IP/清cookie规避限流。
3. 一致预期、日程、修订、趋势分别执行和保存。主EPS失败不会因流程异常阻止其他资料；提供方统一限流时分别记录暂停原因。失败不以空快照覆盖已有有效数据。Yahoo预期/日程不可用时，可选FMP备用来源；原始期间与未确认口径保留。
4. SEC财报仍只取最近两期；指标由10类扩展为25类，增加税务、折旧摊销、股份支付、资产负债、流动项目、应收存货应付、期末股数等。字段保持来源概念、单位、单季/累计/时点；不把流动资产减流动负债直接当成FCFF营运资本。
5. 将业绩材料获取接入普通刷新：按最近两期定期报告公布窗口定位业绩8-K，下载索引中的EX-99附件；8-K本身不占财报名额，也不以封面充当发布稿。公布窗口关联保留待核对标记，不宣称所有公司/所有外籍发行人都能自动覆盖。
6. 扫描业绩附件中的公开转录链接，取得可读正文才归档为电话会；不足以确认正文则保留候选与失败原因。缺失期可使用FMP电话会目录及正文接口，财年/季度来自提供方目录，不按自然月猜测财季。仍缺正文时明确列出期末日，音频不会变成模型编造的转录。
7. 从发布稿/电话会原文中定位指引关键词段落，存为`guidance_draft`并保留证据ID；这是待核对片段，不自动确认数字、期间或上调/下调。
8. 命令行新增资料、事件、已核对指引、估值假设与前瞻冻结入口，复用网页业务规则。无需为这些操作启动网页；普通查询仍不调用模型。

## 实际执行结果

已执行：

```bash
.venv/bin/stock-research AAPL --force --json --output data/AAPL-repair-20261024.json
```

完成时间：2026-10-24 13:17:04（Asia/Shanghai）。报告：`data/AAPL-repair-20261024.json`。退出码1，原因是仍没有可用行情；其他步骤的已取得数据已保存。

| 区块 | 本次真实结果 |
| --- | --- |
| 独立身份查询 | 已移除，本轮直接进入`history(日线)` |
| SEC财报 | 两期：2026-06-27、2026-03-28，25类指标共66条事实，两份10-Q |
| 业绩发布稿 | 两份EX-99附件，分别约10456、10787字符，已存原文及段落 |
| K线与技术区间 | `history()`仍触发Yahoo限流，无法取得新K线；移除info没有解除提供方限流 |
| 一致预期、日程等 | Yahoo进入冷却，分别记录暂停；未配置FMP_API_KEY，备用来源未请求 |
| 电话会 | 两份发布稿没有发现直接转录链接；未配置备用转录来源，仍缺两期正文/Q&A |
| 指引 | 取得发布稿后关键词定位没有找到片段，草稿items为空；不能推断公司没有指引，也不能编造内容 |
| 估值 | 本次没有传入估值假设，未生成；不以两季EPS年化或随意选PE/WACC生成价格 |

后续只修正了边界、说明与提示，没有在已知限流期间反复强制刷新。FMP密钥分支、资料/事件/指引/估值文件导入尚未运行验收；这些不能声称已经验证通过。日线成功后的技术计算本轮仍被限流阻挡，未获运行验证。

## 配置与使用

根目录`.env`可补充：

```dotenv
FINANCIAL_REPORT_COUNT=2
YAHOO_COOLDOWN_SECONDS=120
MATERIAL_LINK_LIMIT=3
FMP_API_KEY=
```

FMP密钥留空时不请求该提供方。填写密钥后使用其现有账号的接口额度，分析师预期、财报日程和电话会接口可能具有不同访问权限；程序不购买订阅、不创建账号。401/402/403会明确报告权限问题，不绕过付费限制。备用来源当前覆盖预期/日程/转录，不覆盖行情；Yahoo日线持续不可用时，需要后续选定有权限的替代行情源，不能把此配置写成已经解决行情限流。

```bash
# 普通查询：含最近两期财报、业绩附件、电话会尝试；不调用AI
stock-research AAPL

# 强制刷新：供应商冷却仍生效，旧数据不会冒充本次取得
stock-research AAPL --force --json --output data/AAPL-next.json

# 导入公开原文清单（先导入，再刷新，模型仍默认不调用）
stock-research AAPL --materials materials.json

# 明确执行DeepSeek分析，会使用你配置的API额度
stock-research AAPL --analyze

# 分别记录已核对事件/指引、冻结前瞻、用假设计算估值
stock-research AAPL --event event.json
stock-research AAPL --guidance guidance.json
stock-research AAPL --freeze-preview
stock-research AAPL --valuation valuation.json
```

`--cached`是只读模式，不能和上述导入、确认、冻结或估值参数合用。文件导入错误会输出中文警告，仍保留其他研究结果；CLI退出码沿用交付说明。

资料清单可为一个对象或对象数组，每项需要`kind`、`title`，正文来源为`url`、`file`、`text`之一。`kind`为`call/filing/release/presentation`，推荐填写`period_end`与`published_at`；文件路径相对于清单所在目录。公开URL仍受HTTPS/内网防护约束，PDF需可提取文字。

事件文件需要`period_end`、`scheduled_at`（含时区）、`source_url`、`confirmed`（布尔值）。日期经公司确认后才填写true；冻结需当前预期已经取得且仍在公布前。历史财报前预期缺失不能事后回填。

指引文件需要`metric`（EPS/revenue/operating_margin/capex）、`period_end`（目标期）、`basis`、`unit`、`low`、`high`、`evidence_id`、`source_note`；证据必须属于本股票原文段落，才能写入已核对记录。

估值文件沿用研究页JSON输入，可为单个对象或多个模型对象数组。公共字段`method`、`currency`、`source_note`；EPS需要`convention`、`eps_basis`、`period_note`、三个`scenarios`（name/eps/pe），目标估值另需未来`target_date`；DCF需要完整经营现金流路径、折现/终值、股数与权益桥接，详见`valuation.py`与研究页。输入按Decimal计算，仍保留适用性、币种和边界校验。

无需查询身份也可明确声明估值上下文：可选`security_context`对象含`confirmed:true`、`source_note`、`type:"EQUITY"`、`currency`及可选`sector`。这是你确认的假设资料，只用于当前计算，不反写证券身份；不能覆盖已知ETF或与行情冲突的币种。没有事实或有依据的假设时继续保留缺失。

## 2026-10-24 15:56：DeepSeek实际分析完成

用户明确授权再次执行AAPL查询并开启DeepSeek。最终导出`data/AAPL-deepseek-verified-20261024.json`，退出码0、步骤警告0；来源命中有效/永久缓存，没有重复下载旧财报。deepseek-flash实际处理4份原文、7513段、16个分析分段，并结合66条财务事实生成带引用草稿。

本次修复重复元数据导致的89段预算溢出、加入最多一次引用/JSON纠正及失败响应存档、将汇总输出上限与分段独立。全部分段有缓存，续跑没有重新分析已通过分段。报告的引用存在性通过，不代表语义、财务数字或资料缺失判断已核实：已发现电话会误列、完整材料误称节选、引用不能支持“无电话会”等问题。

量化管理层经营指引、公司确认事件、历史冻结预期仍缺。模型提取的定性经营前瞻没有回写为已确认指引。完整覆盖、模型问题、执行与token记录限制见[本次核对](../data/AAPL-deepseek-summary-20261024.md)。未新增/执行测试套件，未扩大到其他股票或分支验收。

## 接口依据

- [yfinance history](https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html)及[metadata额外请求问题](https://github.com/ranaroussi/yfinance/issues/2745)：无身份查询适配与版本限制。
- [FMP分析师预期](https://site.financialmodelingprep.com/developer/docs/stable/analyst-estimates)、[电话会正文](https://site.financialmodelingprep.com/developer/docs/stable/search-transcripts)、[目录](https://site.financialmodelingprep.com/developer/docs/stable/transcripts-dates-by-symbol)：采用Stable接口，保留原始期间，不使用旧版接口或模型记忆补数据。

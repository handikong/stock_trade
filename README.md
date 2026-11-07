# 个人美股交易决策系统

北京时间每周六08:00，后台重新抓取全部活动关注股的财年EPS与历史Forward PE，保存[每周自动估值参考](docs/43-weekly-valuation.md)。在个股研究页或`stock-research SYMBOL --cached`查看，和手工确认估值分开。

当前自动股票池来源是[富途美股热榜](docs/42-futu-heat-watchlist.md)。北京时间每日06:00取前100条，只保留股票；首次成功替换旧指数池，随后只追加新上榜股票，有新增就发飞书。行情更新和策略扫描仍只在有新美股收盘时执行。

Linux 服务器首次运行 `bash deploy.sh`，之后运行 `bash update.sh`。`deploy.sh` 安装依赖并初始化数据库；默认关注池由首次富途同步建立。`update.sh` 只拉取当前分支的上游代码并重启单进程后台。已有关注记录保留。见[一键部署说明](docs/41-linux-deploy.md)。

盘后任务可自动累计[美股成交股数前50](docs/40-volume-leaders.md)：北京时间06:00先取榜、去重加入关注池，新增股票参加同一轮行情更新和后续策略扫描。用 `.venv/bin/python -m stock_research.schedule_cli leaders` 查看最新保存的榜单。

2026-11-05新增[指数候选与流动性股票池](docs/39-index-universe.md)：纳指100官网公开表＋SPY发行商股票持仓代理标普500，按官方调整公告更正后去重518只候选；默认最近20个完整交易日日均成交金额至少1亿美元、末日价格至少5美元，均可配。`.venv/bin/stock-universe --apply`将合格股写入现有关注列表，后续06:00/07:00任务共用；不立即扫描或推送。默认仅评估，`--offline`复用价格缓存，`--refresh-members`显式更新公开来源；人工移除优先，缺数据另列，历史/持仓保留。真实导入结果、来源限制与用户核对清单见专题，未新增/运行测试。

2026-11-05新增[系统后台盘后任务](docs/38-post-close-schedule.md)：北京时间06:00更新关注列表完整日K入缓存；07:00等更新阶段结束后只读缓存算买点，有当前BUY才研究并发飞书，不考虑账户金额。配置`config/post-close-schedule.json`从2026-11-06启用；需用户重启并保持单进程后台常驻才生效。交易日/缺失数据/重启进度/正式消息去重及10:00补跑截止见说明；未运行服务/CLI/测试/抓取或实际发送。

2026-11-05按用户模板精简[飞书研究卡](docs/37-feishu-brief-card.md)：股票策略、止损百分比、估值、支撑压力及买入依据，使用飞书原生富文本卡片，长说明移入档案。无信号仍可用`stock-notify GOOG --research-only --refresh --send`查看，手动研究不补造策略或止损。仅逻辑审阅及官方格式查阅，未运行命令/测试/实际发送。

2026-11-05单股无信号也可推送研究：`.venv/bin/stock-notify GOOG --research-only --refresh --send`，去掉`--send`仅预览。标题明确“手动快照（非信号）”，读取本人确认EPS/PE与程序技术/估值，缺项保留；不扫描、不调用模型、不占正式信号去重。独立编号档案与语义见[推送专题](docs/27-signal-research-feishu.md)。本轮未实际运行/测试/抓取/发送，由用户验证。

2026-11-05新增[美股网页工作台](docs/36-web-workspace.md)：首页手工维护持仓，扫描名单增删/后台扫描，个股估值/技术查询，以及EPS＋PE JSON预览/草稿/确认保存。A股按用户最新决定暂不接入，入口禁用；不新增注册/多用户或规则编辑。已有虚拟环境可在根目录运行`.venv/bin/python -m uvicorn stock_research.api:app --host 127.0.0.1 --port 8000 --workers 1`，打开[本地工作台](http://127.0.0.1:8000)。仅代码逻辑审阅，未启动/构建/自测/抓取/实际写用户持仓，WEB01—WEB12均待用户核对；下方“网页后续做”为旧进度。

2026-11-05最新改为EPS与PE全部手工录入：[提问及七字段JSON](docs/templates/gpt-manual-eps-pe-prompt.md)。新增三档EPS＋pe_low/pe_median/pe_high，industry_pe可选；程序计算历史及行业两组估值。普通研究/levels只更新行情，不自动抓PE，不回退旧自动PE缓存；旧v1/v2手工格式兼容，v3只EPS记录提示补PE。见[规则、命令和用户核对清单](docs/35-manual-eps-pe.md)。仅逻辑审阅，未运行应用/抓取/测试。

2026-11-05最新默认输入仅为三个EPS字段：eps_conservative、eps_base、eps_optimistic，见[提问与JSON](docs/templates/gpt-eps-only-prompt.md)。stock-input按股票及录入日保存固定NTM研究情景，stock-research/非cached的stock-levels独立获取Yahoo历史PE和NYU行业PE并列表格计算；录入/通知研究/cached不请求来源。GOOG/GOOGL行业先用明确标注的广告行业代理，窗口/TTL/定义/行业映射在config/automatic-pe-policy.json。旧v1/v2兼容，不恢复财报/身份/AI分析，口径未知只显示倍数假设。见[流程、限制与用户核对](docs/34-eps-only-automatic-pe.md)。未运行应用/测试或实际PE抓取，由用户验证。

2026-11-03最新：默认改用[精简EPS＋历史PE提问模板（已内嵌返回JSON）](docs/templates/gpt-eps-pe-prompt.md)。`stock-input GOOG --file data/GOOG-input-v2.json`直接计算三档前瞻估值参考；只有EPS平均值也支持，确认后供研究/技术结合共用。v2不再要求DCF、实际TTM和折算率；旧v1兼容，历史TTM PE跨口径假设明确标识。见[规则与用户核对清单](docs/33-compact-eps-pe.md)。只逻辑审阅，未运行应用或测试，由用户验证。

2026-11-03新增独立命令：[完整CLI验证流程与待办](docs/32-cli-verification-workflow.md)。`stock-input GOOG`直接粘贴GPT JSON（默认预览，--save草稿、--confirm确认）；`stock-levels GOOG --cached`单独比对技术支撑压力与基本面配合；`stock-notify test`预览无信号测试卡，加`--send`向已配置群发送一条。正式`stock-notify GOOG --send --refresh`继续只处理有效信号。两新命令本地入口已补齐，网页后续开发；未运行应用/测试或实际发送，验收清单全部待用户核对。

2026-11-03新增[技术与基本面综合判断](docs/31-fundamental-technical-confluence.md)：每个技术区间保留原强弱，再显示估值支持/冲突/情景分歧/资料不足、交集与研究共振。CLI/JSON/通知同一结果；未来EPS目标需要手工折算依据，DCF默认仅同日比较，缺失明确提示。参数在config/fundamental-technical-policy.json，手工JSON与GPT模板已更新；原财报/电话会议获取保持停用，网页后续做。仅逻辑审阅，未运行命令或测试。

2026-11-03最终研究逻辑已调整：财务输入由本人问GPT、核对后录入，停止财报/电话会议/预期/网站简析自动研究；**程序仍计算EPS×PE、DCF和K线技术支撑压力**。用`stock-research GOOG --valuation 你的JSON路径`保存草稿，核对后加`--confirm-valuation`确认；没有输入只提示，不回退旧自动结果。行情与第二模块保持。后台预览/保存接口已提供，网页录入后续做；见[规则与命令](docs/30-manual-valuation-input.md)、[GPT模板](docs/templates/gpt-manual-valuation-prompt.md)、[JSON结构](docs/templates/manual-valuation.template.json)。仅逻辑审阅，未运行新版命令或测试。

早期模板阶段记录（已由上条实现覆盖）：[操作与格式约定](docs/30-manual-valuation-input.md)、[给GPT的提问模板](docs/templates/gpt-manual-valuation-prompt.md)、[JSON空白结构](docs/templates/manual-valuation.template.json)。本轮仅交付模板；网页整段粘贴、确认保存及停止自动估值尚待实现，不能把新JSON直接提交旧计算器。

2026-11-03用户授权实际运行GOOG新版研究命令：退出2、默认111行，全部命中缓存，无模型请求。PE/EPS已有机械情景，仍发现营收提取遗漏、DCF数据/假设缺口及盈利质量问题；公开简析只有标题，尚未取得正文摘要。见[实际输出与原因分析](data/GOOG-research-review-20261103.md)。本次未修改业务代码或运行测试，不能视为第一模块封版。

2026-11-03最新：stock-research默认紧凑中文表格，--details展开、--json完整导出；PE/EPS/DCF独立记录结果和缺口。用户已取消DeepSeek原文分析，改读公开RSS简析，默认Seeking Alpha股票订阅，逐条附出处；标题/失败/旧材料明确标识。旧原文和分析保留。见[报表与估值](docs/28-research-console-report.md)、[公开简析与停用范围](docs/29-external-briefs.md)。只逻辑审阅，未运行新版命令/测试/程序抓取，由用户验证；下方首次/新报自动AI记录已停用。

2026-11-01支持单股信号研究和群推送：`.venv/bin/stock-notify NVDA --refresh`更新单股行情并预览；加`--send`处理当前有效信号并发群。股票无需先加入池，也不自动加入；不带股票仍扫描全池。单股/批量共享投递去重，详见[命令与NF12—NF14核对](docs/27-signal-research-feishu.md)。仅逻辑审阅，未运行测试或实际投递，由用户验证。

2026-10-31新增信号研究与飞书群机器人：`stock-notify`默认只预览当前有效信号；`.venv/bin/stock-notify --send --refresh`更新行情、为新买点复用/补齐第一模块研究后发群，退出/预警变化优先推送。Webhook与可选签名密钥写本地`.env`，模板见`config/feishu.env.example`。有投递去重、失败/未知状态与历史；未设定时任务，不生成真实成交。详见[配置、命令与NF验收](docs/27-signal-research-feishu.md)。本轮仅逻辑审阅，未运行测试或实际发送；等待用户配置地址后验收。

2026-10-31命令行报表更新：用户明确暂不做页面，`stock-backtest`现在默认输出账户绩效、各股票累计盈亏、逐笔买卖三张表，包含夏普率、回撤区间、年化波动率及未平仓盈亏。已有结果用`.venv/bin/stock-backtest --report data/MG7-backtest-10000-20261031.json`直接出表，不重跑交易；--details展开长说明。已实际生成[三表文本](data/MG7-backtest-report-10000-20261031.txt)，夏普2.23（无风险假设0%、252日年化），见[统计口径](docs/26-console-backtest-report.md)。没有运行测试套件。

2026-10-31新增可配置资金的历史组合回测：用户确认总初始资金10,000 USD，`.venv/bin/stock-backtest --cash 10000`运行MG7共享现金模拟，`--cash 20000`可改金额；配置在`config/backtest-policy.json`。本轮已按明确授权实际跑一次，2026-03-02至10-23净值10,000→10,616.49 USD（+6.16%），最大日终回撤1.34%；平均资金投入15.96%，不能忽略现金影响。见[完整中文买卖结果](data/MG7-backtest-summary-20261031.md)与[模型/参数/限制](docs/25-portfolio-backtest.md)。未跑测试套件，未写真实持仓/成交；实盘仍由用户手动决定，完整账户管理未实现。

2026-10-31展示更新：`stock-trend ... --strategies`及股票池scan默认按股票展示“买入信号 → 预警变化 → 对应卖出信号”的完整配对过程，含日期、参考价、退出原因；未退出显示跟踪中。跨展示起点补回原买点，`--strategy-details`保留逐项条件视图。规则不变，不生成真实成交或收益；未运行测试，由用户验证。见[完整过程展示](docs/24-signal-flows.md)。

2026-10-30最新退出规则：两套买点共用“冻结保护线＋三项状态共同确认”。红带一级预警，红带且EMA5＜EMA10二级预警，再加MACD12/26/9的DIF＜DEA则确认退出；反包按当日状态退级/撤销，不要求交叉依次发生。冻结保护线跌破优先。旧版退出已完整注释保留，当前不执行。原`stock-trend ... --strategies`和`stock-watchlist scan`输出预警与退出，`history --kind EXIT`查看曾经发现的退出。仅技术信号，不生成持仓/成交/收益；未运行测试，由用户验证。详见[当前退出规则与验收](docs/23-common-exit.md)。

2026-10-30最新增量：新增MG7初始自选池、双策略批量扫描和信号历史去重。首次使用初始化AAPL、MSFT、GOOGL、AMZN、META、NVDA、TSLA，随后可增删。用`.venv/bin/stock-watchlist list`查看，`.venv/bin/stock-watchlist scan --offline --history-start 2026-01-01 --start 2026-03-01`只用缓存扫描；去掉`--offline`可补齐缺行情股票，`--refresh`手动更新日线，`history`查看信号，`last`查看扫描结果。详见[股票池批量扫描](docs/21-watchlist-scan.md)。未运行测试，由用户测试；完整机会生命周期与账户模块未实现。

2026-10-29最新增量：`stock-trend ... --strategies`默认分别输出EMA20回踩和独立MACD12/26/9图标1买入提示。图标1出现即提示，不叠加回踩或5根红带限制；见[箭头策略与核对清单](docs/20-macd-reversal-discussion.md)。本轮仅逻辑审阅，未运行正式集成验证。

当前状态：**第一模块AAPL已有行情、两期财报、预期、估值及DeepSeek草稿，缺口与模型问题见[实际核对](data/AAPL-deepseek-summary-20261024.md)，整体验收未完成。第二模块已实现趋势命令、EMA20回踩、独立MACD图标1及MG7自选池批量扫描；支持2026年1—2月预热、3月起展示历史，见[趋势命令](docs/16-trend-cli.md)与[股票池说明](docs/21-watchlist-scan.md)。回踩高点须绿带，严格次日确认，最多容忍5根连续红带。最新集成未运行，由用户验证；完整持仓/账户和机会生命周期尚未实现。**

本项目帮助使用者在盘后制定计划，在盘中接收提醒，并通过资金分配、风险预算、持仓管理和复盘，减少下跌抄底、盈利过早退出、临时换股等行为。支持美股与 ETF、现有持仓导入、长期基本面持仓和独立的短期机动策略。

## 已确认的需求

- 参考本金和币种由使用者配置，不默认推定币种。
- 所有本金、资金比例、风险阈值、持股上限、策略参数和提醒方式均由用户配置，不能写死。
- 用户当前偏好：约 10% 资金可用于抄底和波段；长期持仓可持续很久，机动持仓约一个月。
- 用户当前风险口径：相对参考本金最多亏损约 5%，允许利润回撤；不是默认从净值最高点回撤 5%。
- 希望年收益约 15%；系统将其作为可配置的研究/评估目标，不承诺收益，不用收益缺口驱动交易。
- 避免妖股、仙股；需要将其转化为透明、可配置的流动性、证券类型、财务与异常波动筛选。
- 第一版以盘后决策、盘中提醒、人工在券商处执行和导入成交为工作方式。
- 第一模块输入股票代码研究：后台K线、yf补齐与自建缓存、多因素支撑压力、财报与业绩材料存档、财报前瞻、管理层指引、EPS倍数与DCF情景估值。按用户最新要求，前端不展示K线图，不自动获取电话会议。
- 第二模块识别上涨/震荡/下降，按趋势适配策略；一个主观察池按趋势、策略和准备状态分类，趋势变化后重评机会与原计划。

以上偏好可预填为**未启用的草稿**，首次设置后才能激活；不构成生产参数或买卖指令。

## 文档与实现依据

| 文档 | 用途 |
| --- | --- |
| [产品需求](docs/01-product-requirements.md) | 痛点、用户流程、范围和结果标准 |
| [交易与风险规则](docs/02-trading-rules.md) | 规范性规则、计算口径、状态转换和策略条件 |
| [配置与决策记录](docs/03-configuration-decisions.md) | 可调参数、默认行为、待确认事项和变更政策 |
| [系统设计](docs/04-system-design.md) | 模块、数据、任务、接口和实现边界 |
| [验收与实施路线](docs/05-acceptance-roadmap.md) | 场景化验收、回测要求、分阶段交付 |
| [依据与限制](docs/06-sources-and-limitations.md) | 官方资料、设计假设、能力限制 |
| [首个个股研究模块](docs/07-stock-research-module.md) | 行情缓存、技术区间、财报前瞻、电话会议、估值与模块验收 |
| [第二模块：趋势、策略与观察池](docs/08-trend-strategy-module.md) | 多周期趋势、策略路由、独立机会、动态分类、变化重评与验收 |
| [开发交接与待设计清单](docs/09-development-handoff.md) | 当前进度、下次编码起点及5个待细化模块 |
| [第一模块启动与交付说明](docs/10-research-implementation.md) | 安装配置、运行命令、当前能力、数据限制与用户测试要点 |
| [AAPL导出数据核对](docs/11-AAPL-data-review.md) | 用户报告的实际覆盖、缺失原因及最近两期财报调整 |
| [研究数据获取修复](docs/12-research-repairs.md) | 移除身份查询、附件/电话会接入、备用来源、命令行输入及不能自动完成的限制 |
| [自动估值草稿](docs/13-valuation-drafts.md) | 两期财报与补充TTM口径、PE参考、EPS/DCF情景、可修改假设及实际结果 |
| [DeepSeek费用与质量对比](docs/14-deepseek-cost-quality-comparison.md) | 用户授权的直接询问、官方搜索和完整10-Q抓取实验，真实token、费用估算与字段缺失 |
| [财报公共方法与永久AI分析](docs/15-financial-report-lifecycle.md) | 命令行/网页/未来日历共用入口、按财报版本分析、同版本复用与失败续跑 |
| [趋势判断与历史分段命令](docs/16-trend-cli.md) | stock-trend、三族证据、确认/保持、2026短样本历史分段、逐日依据与局限 |
| [用户富途MACD色带与信号](docs/17-futu-macd.md) | 用户公式、绿偏上/红偏下、四条件与FILTER、配置、实际AAPL对照图及富途一致性边界 |
| [策略增量第一步](docs/18-strategy-trend-gate.md) | STR-01趋势前置检查、四种结果、运行命令和用户核对清单；不判断完整策略或买点 |
| [EMA20回调买点](docs/19-ema20-pullback.md) | 当前启用的具体买点规则、命令、初值和核对清单，替代旧前置门槛 |
| [MACD12/26/9独立箭头策略](docs/20-macd-reversal-discussion.md) | 图标1买入提示已接命令；原式及其他事件讨论、NVDA独立复算记录 |
| [当前信号、研究与飞书群推送](docs/27-signal-research-feishu.md) | stock-notify、群机器人配置、研究复用、有效期、去重与失败处理 |
| [后续开发约束](AGENTS.md) | 给后续实现者的项目级指引 |

需求与规则使用稳定编号。后续代码、逻辑审阅和变更说明应引用相关编号；规则调整时同步更新配置定义和验收场景。用户后续明确指示优先，文档不得用于阻止用户调整自己的设置。

## 开发约定

- 页面、操作反馈、错误等提示使用中文，关键业务逻辑添加必要中文注释。
- Codex只阅读代码、推演逻辑和边界，不新增测试文件，不自行执行测试或启动应用自测。
- 实际运行测试与验收由用户负责；现有验收场景作为用户测试清单保留。
- 交付说明实现内容、启动/使用方式和待测试点，不将未运行的代码声称为测试通过。

## 首次启用时填写，不阻塞当前开发

账户计价币种、券商及 CSV 格式、实际现金与持仓、参考本金与生效日、资金使用期限、风险参数、数据源与数据延迟、通知渠道。缺少关键字段时可浏览和研究，不能产生声称已通过风控的开仓计划。

初期允许使用模拟数据开发，但必须显著标记为模拟；不能将模拟行情、回测收益或模拟成交作为真实结果。

## 模块顺序

1. 第一模块：个股研究，见下文及专题文档。
2. 第二模块：[趋势识别、策略适配与动态观察池](docs/08-trend-strategy-module.md)，复用第一模块的数据与证据；当前仅趋势和命令行已实现待验收。
3. 后续暂列5个模块：账户与持仓、资金仓位与风险、交易计划与执行记录、任务与提醒、绩效与复盘，详见 [待设计清单](docs/09-development-handoff.md)。实施路线中的M1/M2是工程阶段编号，不是第一/第二产品模块。

先按 [个股研究模块](docs/07-stock-research-module.md) 实现单股研究闭环，研究页使用证券自身报价币种，不要求先填账户本金。财报前瞻必须区分市场一致预期和管理层指引，管理层指引需公司官方原文依据；电话会不再必需或自动获取。原始资料持久保存，AI负责有依据的分析，估值由确定性程序计算。

当前DCF内在价值与12个月目标时点估值分别显示；支撑压力区间不等于估值区间；研究结果本身不代表已经通过账户风控。完整账户、计划、执行核对与复盘仍按实施路线后续接入。

第二模块区分趋势、策略机会、交易计划和真实持仓状态。趋势未知不强行记为震荡；匹配策略不等于立即买入；同一股票多个策略保留独立机会；趋势变化不自动改写旧交易或机械清仓。

## 当前交付

第二模块趋势命令只处理行情，不触发财报或DeepSeek。已有editable安装可直接使用Python模块，严格从2026年起预热、3月展示历史区间：

```bash
.venv/bin/python -m stock_research.trend_cli AAPL --history-start 2026-01-01 --start 2026-03-01
```

更新安装入口后可用`stock-trend AAPL --start 2026-03-01`；不带`--start`为当前日/周趋势判断。历史默认短样本SMA20/30，当前默认SMA50/200，不混为同一种长期趋势。输出保留上涨/震荡/下降、待确认和数据不足区间，不回填确认、不提前使用周线；具体参数与使用方式见[趋势说明](docs/16-trend-cli.md)。

待确认区间现增加“短期动能、修复、形成中、动能减弱”等阶段和具体未满足条件，避免将数周不同证据合成一段。阶段只解释已有证据，不改变严格确认日期。先前已只读核对用户旧版AAPL滞后原因，随后实际运行情况见下文；严格分类偏慢及保持条件仍待讨论，见[记录与限制](docs/16-trend-cli.md#8-用户反馈的aapl记录核对)。

随后按用户要求实际运行并对照AAPL K线检查：命令成功，165天中131天待确认，确认及时性和保持规则仍需调整，目前不能封版。修复了一项结构失败原因误报，v2.1固定终点48条记录与完整窗口前缀一致；详见[实际运行与K线图](data/AAPL-trend-review-20261024.md)。这不代表已验证策略收益。

用户随后提供富途公式并确认绿带偏上、红带偏下、震荡另判。新增可选方式，复用本地行情、保留原四条件和FILTER：

```bash
stock-trend AAPL --method futu-macd --start 2026-03-01
```

已实际运行：03-31至05-29连续绿带，07-02转绿；03-26也出现短暂转绿后转红，不能据此宣称准确率或收益通过。震荡尚未设计，默认原三族方式仍保留；参数和结果见[富途指标说明](docs/17-futu-macd.md)。尚未与富途相同复权、EMA历史长度及函数边界逐点对齐。

2026-10-25按用户要求增加主趋势和回踩/反抽层，同一富途命令现在先展示主趋势阶段，再列原始色带和箭头。短暂反向先观察，结构保护位冻结；价格破坏或持续反向与价格确认触发失效/切换。所有参数在`config/futu-macd-policy.json`的`trend`段，默认值未验证。本轮未运行，由用户重新核对；首次不用`--cached`，算法更新会复用本地行情重新计算，无需`--force`。说明与验收点见[富途专题第7节](docs/17-futu-macd.md)。

2026-10-27确认的EMA20高点回踩规则保持：先有上涨与明显远离均线，所选高点当天必须为绿带，再从高点回落到EMA附近；严格下一交易日开盘≥前日EMA、收盘≥当日EMA确认，不要求突破前高。最多容忍5根连续红带，含确认日，第6根起回踩策略按下降处理，取消旧上涨资格；短暂转绿不能复活旧高点，须在失效后重新形成上涨波段。输出高点/回踩/确认证据、红带根数与失效日期，参数可配置。2026-10-29另接入独立MACD图标1策略，突破和红带反弹草稿保持关闭。未运行，由用户验证；回踩详情见[规则与核对清单](docs/19-ema20-pullback.md)。

```bash
.venv/bin/stock-trend AAPL --method futu-macd --start 2026-03-01 --strategies
```

已加入Go入口、Python计算服务、中文研究界面、PostgreSQL部署配置和SQLite本地开发模式。启动方式见 [交付说明](docs/10-research-implementation.md)。API密钥仅在服务端配置，尚未连接券商、部署或执行真实交易。

支持直接在终端输入股票，无需先启动网页服务。首次安装后查询：

```bash
cd /path/to/stock_trade
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./research
stock-research AAPL
```

已有虚拟环境时直接激活使用，无需重建。可将 `AAPL` 换成自己的股票或ETF代码；只输入 `stock-research` 会提示输入代码。默认获取/复用行情、财务和当前预期缓存，输出技术区间、原文覆盖和已有估值/研究结果。已配置DeepSeek时，首次取得完整财报或实际新增财报版本才分析；其他查询永久复用本地结果，预期和模型参数变化不重跑。

```bash
stock-research AAPL --json --output data/AAPL-research.json
stock-research AAPL --cached
stock-research AAPL --force
stock-research AAPL --analyze
```

`--json`包含全部结构化财务明细，`--cached`只读本地数据；`--analyze`用于首次/新财报分析或手动继续失败任务；同版本已有完整结果仍复用，不强制重跑。官方财报访问需配置根目录`.env`中的 `SEC_USER_AGENT`，AI分析需配置 `DEEPSEEK_API_KEY`。管理层指引、估值假设未取得时会显示缺失，电话会已取消自动获取；普通查询不会编造目标价。更多参数、退出码和核对方式见交付说明。

命令日志默认实时输出到控制台，带时间、级别、进度、缓存命中、耗时与失败提示。日志走stderr，研究结果走stdout；使用`--output`导出报告时，日志仍显示在控制台。

普通研究现在还会从已归档数据生成未确认估值草稿，输出当前PE、EPS×PE一年目标情景、DCF今日及一年后区间；参数在`config/valuation-policy.json`，不调用AI。技术区间新增强/一般/弱及待确认标签，参数在`config/technical-strength.json`，保留每项依据与限制。修改技术参数只重算有效缓存，不重复请求行情；`--cached`仍只读。两个季度本身不够形成完整TTM与可靠预测，补充基线和模型限制见[估值说明](docs/13-valuation-drafts.md)。

财报默认只取最近2期，配置为`FINANCIAL_REPORT_COUNT=2`；8-K不计作定期财报。JSON新增`data_status`与`research_scope`说明缺失项和本次范围。旧档案保留，不因SEC取得部分财务数据就判定研究资料齐全。

财报、财务事实、披露索引和业绩附件永久保存，不设24小时过期；已有完整且范围/版本匹配的档案直接复用。首次获取、补缺或显式`--force`人工同步时可请求来源，同版本原件复用。动态预期/日程仍有独立有效期。财报处理共用`financial_reports.process_financial_reports`，后续日历在实际发布后调用同一入口同步并分析未覆盖的新版本；同版本通知不重复分析，`--force`也不强制AI。事件接口尚未接入，详情见[财报公共方法](docs/15-financial-report-lifecycle.md)。

已取消独立`get_info`身份查询，日线直接获取。普通刷新会尝试两期业绩EX-99附件与指引片段，不再扫描/获取电话会；FMP仅用于可选备用预期/日程，需要自行配置密钥及权限。当前无通用公司IR抓取器，独立官方发布稿/演示稿可用`--materials`导入。支持`--event`、`--guidance`、`--freeze-preview`、`--valuation`，详细格式与实际修复结果见[修复说明](docs/12-research-repairs.md)。

2026-10-24用户随后明确启动第一模块开发，并补充“K线不用展示在前端”。本轮只做代码阅读与逻辑推演，未安装依赖、构建、启动应用或执行测试；实际运行由用户进行。其余模块仍按原计划后续推进。

随后用户明确授权执行AAPL无缓存抓取与原因诊断，已使用`--force`实际运行并记录结果，见[执行诊断](docs/11-AAPL-data-review.md)。本次未调用DeepSeek、未新建测试文件，未扩大到其他运行测试。

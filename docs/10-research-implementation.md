# 第一模块 v0.1：启动与交付说明

2026-11-05后台Worker新增[06:00行情/07:00买点飞书计划](38-post-close-schedule.md)，从11月6日起配置启用，用户重启单进程服务常驻才生效；POST_CLOSE_SCHEDULE_FILE可覆盖，日志控制台，status只读可查。启动前请停旧服务，本机新增Worker文件锁；未启动/构建/实际执行或测试。

2026-11-05已开发[美股网页工作台](36-web-workspace.md)，原页面改为持仓、扫描股票池、个股研究及七字段EPS＋PE预览/确认；A股暂不接入。已有虚拟环境用`.venv/bin/python -m uvicorn stock_research.api:app --host 127.0.0.1 --port 8000 --workers 1`启动后打开8000端口即可，无前端构建步骤。旧财报/AI表单已移出，当前页面规则及WEB清单优先于本文旧功能说明。未启动或自测，由用户验证。

2026-11-03新增stock-input手工JSON录入、stock-levels独立支撑压力比对、stock-notify test无信号连通性测试，详细命令/副作用/本地入口安装情况见[CLI验证流程](32-cli-verification-workflow.md)。本轮没有执行这些应用命令或发送消息；先由用户核对再做网页。

2026-11-03当前入口：stock-research仅处理行情/技术与已确认手工财务输入，不读旧财报/电话会或自动取得预期/简析。--valuation导入新版JSON（默认草稿），--confirm-valuation显式确认；缺值提示，程序计算EPS×PE/DCF。旧--analyze/--materials/--event/--guidance等流程停用，force仅行情。新后台preview/save接口已就绪，网页后续增加；[模板、命令与限制](30-manual-valuation-input.md)优先于下文旧启动说明。本轮未运行程序或测试。

2026-11-03交付覆盖旧分析说明：默认[紧凑报表](28-research-console-report.md)，--details展开、--json完整。DeepSeek原文分析取消，旧--analyze/--document-id拒绝；普通研究改读[公开RSS简析](29-external-briefs.md)，旧结果留档。新版程序抓取和排版未实际验证，本次只逻辑审阅，由用户运行。

记录日期：2026-10-24。初次代码交付未运行测试。后续按用户明确授权执行了AAPL强制抓取诊断：SEC两期财报与30条事实成功，Yahoo身份/预期调用限流；未调用模型，也未完成整体验收。详细经过见[执行诊断](11-AAPL-data-review.md)。以下为用户使用命令。

## 1. 当前实现

- Go入口：嵌入中文静态界面，将API转发给Python计算服务。
- Python服务：FastAPI、SQLAlchemy；默认SQLite本地开发，Docker Compose使用PostgreSQL。
- 股票身份、日线自建持久缓存、交易所日历、完整K线确认、缺口补齐、企业行动窗口复取与手动完整更新。
- 后台用已确认拐点、ATR、均线共振及量能说明形成支撑压力区间。前端不展示K线，也不接收日K数组。第二模块现已按用户后续指令独立加入趋势及命令行，详见[趋势交付](16-trend-cli.md)，本研究页未加入策略/观察池。
- SEC标准财务事实、最近披露索引及按配置归档的原始文件；无法获取SEC时可用明确标注未核实口径的供应商财务辅助。
- 公开材料链接下载、文本导入、PDF/TXT/HTML上传、原件保留、内容哈希、版本和原文段落定位。电话会正文/问答启发式分区，识别结果需核对。
- yf当前一致预期快照；人工核对财报事件与管理层指引；公布前冻结预期；同期间/单位/口径的旧新指引对比。
- EPS三情景、FCFF DCF、WACC/g敏感性、终值占比；一年后DCF需要独立的未来现金、债务、索取权和股数假设。
- 最新普通查询已增加未确认估值草稿生成与技术强弱分级；历史手工入口保留。AAPL实际结果及补充TTM/假设口径见[估值草稿](13-valuation-drafts.md)，不把两个当季数据直接年化或当作可靠预测。
- DeepSeek公开材料搜索、分段阅读与合并研究草稿、引用存在性校验、分段/整份结果缓存、usage展示；结果不回写为已核实财报或自动买卖信号。
- 可选择本次分析的完整资料；超过分段预算会明确拒绝，不静默截掉电话会问答。
- AI原文按文档分组传输，文档ID/标题不再对每个表格单元重复；段落以数字编号加完整正文表示，引用还原为`文档ID:p编号`并校验。保留全部原文及原始段落定位，不提高分段预算来掩盖重复元数据膨胀；格式变更以提示版本区分缓存。
- 已返回的模型响应（包括引用校验失败、JSON无效或截断）保存在`analysis_response`，记录用量与失败原因，不发布为可用研究。`AI_VALIDATION_REPAIRS`默认1、可设0，限制每次分析调用至多一次纠正请求；再次失败停止，已通过的分段继续复用。失败分段不会凭空补引用。
- 分段输出默认5000 token，汇总输出独立默认10000 token；调整汇总上限仅用于尚未完成的汇总；已有完整分析继续复用，不因参数变化重跑。截断仍拒绝发布，不自动无限加预算。
- 研究快照保存与JSON导出；后台任务去重、状态/进度/警告持久化。中断的付费任务不会重启后自动重复调用。
- 命令行 `stock-research 股票代码`：无需启动Go/FastAPI，直接复用相同数据、缓存、技术算法与报告视图；仅首次/新财报版本可自动请求AI，同版本永久复用。

## 命令行直接查询与导出

首次使用，在项目根目录安装（已有虚拟环境可直接激活）：

```bash
cd /path/to/stock_trade
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./research
```

如需官方财报或AI分析，首次可用 `cp -n .env.example .env` 创建配置文件，再自行填写 `SEC_USER_AGENT`（真实应用名与联系邮箱）和 `DEEPSEEK_API_KEY`。不要覆盖已有`.env`。未填写SEC时仍会尝试Yahoo财务辅助数据，并提示其单位/会计口径待核对；未填写DeepSeek不影响普通行情与财务查询。

```bash
# 输入股票，输出中文研究结果
stock-research AAPL
stock-research SPY

# 不带股票代码时，终端提示你输入
stock-research

# 输出完整JSON并另存文件；文件已存在时拒绝覆盖
stock-research AAPL --json --output data/AAPL-research.json

# 仅读取缓存，无外部数据/模型请求；尚无缓存时提示先普通查询
stock-research AAPL --cached

# 忽略缓存有效期重新获取；普通查询则优先复用有效缓存
stock-research AAPL --force

# 文本默认显示40条财务明细，0表示显示全部；JSON始终含全部已保存明细
stock-research AAPL --facts-limit 0

# 首次/新财报分析或手动继续未完成任务；同版本已有完整结果仍复用
stock-research AAPL --analyze

# 只分析指定的原文，可重复传入ID；ID来自档案输出
stock-research AAPL --analyze --document-id 你的原文ID

# 等价的Python模块入口
python -m stock_research AAPL
```

命令同步执行完即退出，不启动网页、任务worker或端口监听。使用与网页相同的数据库和原文目录，进度/提示写入stderr，报告写入stdout，便于 `stock-research AAPL --json > data/AAPL.json` 重定向（此时目录须已存在，shell重定向本身会覆盖文件；需要防覆盖可用`--output`）。

命令默认将日志实时输出到控制台（stderr），包含本地时间及UTC偏移、INFO/WARNING/ERROR级别、研究模式、步骤进度、缓存命中/跳过、耗时、日线/财务/归档数量、失败原因和报告保存结果。显式使用DeepSeek时还显示分段进度、分段缓存命中和汇总阶段。日志不会主动创建日志文件，不输出API密钥、完整请求、模型提示正文或数据库连接口令。

```bash
# 日志和研究结果均在当前控制台可见
stock-research AAPL

# 报告另存JSON，执行日志仍实时显示在控制台
stock-research AAPL --json --output data/AAPL-research.json
```

报告写stdout，项目日志写stderr；请勿用`2>&1`将日志混入需要程序解析的JSON文件。这里只配置本项目的INFO日志，不开启第三方HTTP客户端的调试日志。

输出包括股票身份/币种、最近完整日线收盘与获取时间、行情覆盖/新鲜度、技术支撑压力与确认依据、均线/ATR、财务数值/单位/会计期间/证据ID/来源链接、财报披露索引、当前预期/修订/日程、已确认财报事件/冻结预期/管理层指引、电话会档案覆盖、已有EPS/DCF情景和DeepSeek草稿。报告生成时间不代表所有数据均在该时刻更新，各块保留各自时间与版本。

财报默认最近两期，可用`FINANCIAL_REPORT_COUNT=2`调整。按定期财报期末日筛选，8-K不占财报名额；原报告和最新修订可能是多份文件，但同属一期。结构化事实仅保留对应期末日，辅助财务源使用季度三表。旧期原文/快照不删除；`documents`仍为完整历史档案，当前默认研究材料ID见`research_scope.selected_document_ids`。改变范围后会重新评估缓存；旧导出文件保持原样，`--cached`也不会把旧快照改写成已刷新。

`data_status`区分哪些块有数据、缺失、需要确认、尚未生成或不适用，不代表有数据就已核实或足够估值。`research_scope`说明当前财报范围以及旧财务缓存是否匹配。`command.warnings`对一般来源异常新增`error_type`和可取得的`http_status`；不记录可能含密钥的底层异常全文。此前导出的通用失败提示无法事后恢复具体异常原因。

2026-10-24用户明确授权一次真实强制刷新诊断，SEC两期30条事实获取成功，Yahoo身份与预期调用遭遇`YFRateLimitError`；结果与调用位置见[实际执行诊断](11-AAPL-data-review.md)。本次授权不代表自动放开其他测试。后续错误记录会将此类限流标为`category=rate_limited`，保留安全的`error_location`（项目文件/函数/行号），没有有效HTTP响应时不填造状态码。

技术区间和均线同时说明调整价/最近收盘报价换算口径，不输出完整K线数组。文本财务明细被数量参数截取时会写明已保存/已显示数量；完整JSON用于逐字段核对。`--output`按当前输出格式保存，并非按文件扩展名切换格式；JSON文件请同时使用`--json`。

普通查询获取SEC业绩附件并提取指引候选片段。用户已取消电话会获取：不扫描转录链接、不调用FMP电话会接口，报告中`data_status.calls=disabled`，不列为资料缺失。已有或主动导入的档案保留。独立IR发布稿/演示稿可用`--materials`导入，目前无通用IR抓取器。`--event`、`--guidance`、`--freeze-preview`、`--valuation`支持命令行事件/指引确认、前瞻冻结及估值计算，字段和规则见[修复说明](12-research-repairs.md)。`--analyze`用于手动继续首次/新版本分析，同版本已有结果仍复用；不自动确认指引或填写估值假设；缺失指引保留未取得。ETF不做公司EPS/DCF。

退出码：`0`为有可用行情且本次没有步骤警告；`2`为参数用法错误，或报告已输出但存在部分来源/分析警告（依据stderr区分）；`1`为无可用行情、缓存不存在、配置/存储/导出失败等；`130`为用户取消。退出码0不代表资料齐全、AI内容已核实或允许买卖，仍需检查报告中的缺失项、时间与来源。全部来源失败时如有旧行情仍输出旧报告及警告，不宣称已经更新。

只读缓存与AI分析不能同时使用。命令不自动处理后台队列，不改变既有运行任务的状态。当前仍按单计算进程设计；用户核对时避免同时对同一股票启动网页刷新与命令强制刷新。

## 2. 本地启动：无需Go也可先使用

在项目根目录执行：

```bash
cp -n .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./research
uvicorn stock_research.api:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [本地研究页](http://127.0.0.1:8000)。Python开发入口也提供同一份静态界面，默认数据库在 `data/research.db`。命令需在项目根目录运行，以使相对数据目录一致。

`.env`可先仅设置数据与模型访问项；未配置SEC/DeepSeek时仍允许展示已取得行情与技术结果，相关区块会提示配置/资料缺失。

如需Go入口，安装Go后另开终端执行：

```bash
cd gateway
go run .
```

打开 [Go入口](http://127.0.0.1:8080)。Go默认转发到 `127.0.0.1:8000`；自定义入口参数通过环境变量 `PUBLIC_ADDR`、`WORKER_URL`传入，Go本身不读取`.env`。

## 3. PostgreSQL与Docker Compose

先复制并编辑`.env`，填写非空 `POSTGRES_PASSWORD`；如使用Compose，保持 `DATABASE_URL`为空，由分离字段构建数据库URL，支持密码特殊字符。

```bash
docker compose up --build -d
```

浏览器打开 `http://127.0.0.1:8080`。Go入口仅映射本机端口；PostgreSQL不映射宿主机端口。数据库保存于Docker卷，原文保存于项目 `data/documents/`，备份时两者都要保留。当前没有自动备份调度，不要仅复制数据库而遗漏原文。

首版仅允许一个Python计算进程（`--workers 1`、一个worker实例）。任务在PostgreSQL中持久化，但当前启动恢复政策按单进程设计，不支持并行多副本部署。数据库首次尚未就绪时worker会由Compose重启后再连接。

容器基础镜像使用维护中的主版本标签，未生成依赖锁文件，也未在本轮构建镜像；具体环境安装结果由用户运行后确认。

## 4. 必要配置

| 参数 | 用途 |
| --- | --- |
| SEC_USER_AGENT | SEC访问身份，填写真实应用名称与联系邮箱；未填不伪造身份发请求 |
| DEEPSEEK_API_KEY | DeepSeek API密钥，只保存在服务端环境，不填入页面 |
| DEEPSEEK_MODEL / BASE_URL | 默认deepseek-flash与官方接口，可按实际账号调整 |
| DEEPSEEK_THINKING | 是否开启思考；默认false，减少不必要的思考输出开销 |
| DATABASE_URL / DATA_DIR | 外部数据库及原文路径；为空数据库使用SQLite开发模式 |
| HISTORY_YEARS / MARKET_CACHE_HOURS | 后台行情深度与复核期限 |
| DISCLOSURE_CACHE_HOURS | 一致预期、日程、EPS修订/趋势的动态资料有效期，默认24小时；不控制财报及业绩附件 |
| FINANCIAL_REPORT_COUNT | 最近定期财报期间数，默认2（1–20）；控制财务事实、定期财报原文与默认AI财报范围 |
| YAHOO_COOLDOWN_SECONDS | Yahoo限流后暂停重复请求的秒数，默认120；force不能跳过冷却，冷却到期不保证供应商已恢复 |
| FMP_API_KEY | 可选备用预期与日程来源，需自身账号和相应接口权限；不再用于自动电话会获取 |
| MATERIAL_LINK_LIMIT | 每期最多自动获取的业绩附件/转录候选链接数，默认3；超出范围明确记录 |
| VALUATION_POLICY_FILE | 自动估值草稿配置，默认项目`config/valuation-policy.json`；参数有来源且可修改，结果不自动确认 |
| TECHNICAL_STRENGTH_FILE | 技术强弱配置，默认项目`config/technical-strength.json`；参数变化使用有效缓存重算，行情取得时间不变 |
| PIVOT_LEFT / PIVOT_RIGHT / ATR_WINDOW / ZONE_TOLERANCE_ATR | 技术区间参数，配置与算法版本随快照记录 |
| AI_CHUNK_CHARS / AI_MAX_CHUNKS / AI_FACT_LIMIT / AI_OUTPUT_TOKENS | 分段、总分析量、结构化事实与输出预算；字符数不等于准确token数 |
| AI_MERGE_OUTPUT_TOKENS | 汇总阶段独立输出上限，默认10000；不改变分段缓存键 |
| AI_VALIDATION_REPAIRS | 每次分析调用最多追加一次引用/格式纠正，默认1，可设0；会额外使用接口额度 |

财报处理共用`financial_reports.process_financial_reports`，命令行与网页已接入，日历发布适配器尚未接入。DeepSeek按原文版本永久复用，不因行情/预期/提示/模型/token参数改变重跑。旧草稿含有的动态信息保留分析当时时点，最新数据独立显示；失败后只手动继续。详情见[公共方法](15-financial-report-lifecycle.md)。

修改服务端配置后重启Python服务生效；第一版尚未提供完整图形化配置版本编辑器。既有研究结果的算法/输入快照保留，不将配置修改回填到历史结论。

旧`FINANCIAL_HISTORY_YEARS`、`FINANCIAL_QUARTERS`、`SEC_ARCHIVE_LIMIT`已停用，旧.env键会被忽略，当前默认按2期。SEC批量原始响应仍完整存档，研究快照只取指定期数；两期数据不足以独立推导完整TTM或长期增长路径，不能因此编造估值假设。

## 5. 数据与研究边界

最新规则：财报、结构化财务、披露索引和业绩附件永久保存，不按24小时过期；`DISCLOSURE_CACHE_HOURS`仅用于动态预期、日程、修订和趋势。普通查询复用完整且范围/版本匹配的档案；首次、补缺、范围变化或显式`--force`人工同步可请求来源，但已有同版本原文不重复下载。后续财报日历发布事件会追加新一期及修订资料，本轮未接入该接口，因此尚不自动发现新财报。原始材料存档时间与新财报是否已到达分别理解，不重设旧档案的取得时间。

- 初次交付未做真实联网接入验证；随后用户授权的AAPL来源抓取和DeepSeek实际分析另行记录于[执行诊断](12-research-repairs.md)，不代表所有证券、账号分支或整体验收已通过。
- 当前取消电话会自动获取与必需性。公司IR资料尚无通用抓取器，自动原文来源为SEC业绩附件，可单独搜索或手工导入公司官方材料。不保证所有公司均在这些材料中给出经营指引，不内置扫描PDF OCR。
- 正文/Q&A与发言人是启发式识别，不声称人工核对完毕。保留完整原文，即使问答分区未知也不编造。
- SEC标准概念有局限；未自动解析所有自定义KPI、分部、non-GAAP调节项和累计现金流重构。单季资料缺失时保留未知；历史披露精度只有日期的事实不宣称有精确公布时分秒。
- 财报前历史一致预期无法事后补造。供应商FY/季度标签与会计口径不足时保留不可比状态，不自动计算历史beat/miss。财报后完整四向对照仍依赖可比的原始材料；AI提供带证据的草稿，不是已核实数据结论。
- 当前可自动定位指引关键词原文段落，仍需引用证据确认期间与数字。普通研究估值为明确假设和补充证据的机械草稿，PE按历史披露匹配、WACC须配置来源；网页或`--valuation`仍支持独立完整假设。它不代表可靠的未来价格范围，完整营运资本、公司自身WACC、利润正常化及稀释尚待完善。
- ETF只展示适用的身份/基金辅助信息、行情与技术结构，不执行公司EPS/DCF；深入ETF成分/费用档案仍需后续数据适配。
- 已实现当前时点快照保留，尚未建设完整历史证券池、财报可知时间回测与策略回测功能。
- 新增研究不涉及账户、风险仓位、券商订单、自动交易或后续5个待讨论模块。

## 6. 用户自行运行时重点检查

1. 查询股票，查看身份、币种、缓存日期、技术区间；前端没有K线图。
2. 重复更新观察缓存复用；模拟上游失败时原有缓存应保留并显示警告。
3. 设置SEC后查看财务与披露索引；导入电话会，核对正文、问答和原文段落编号。
4. 使用公司确认的未来财报事件冻结预期；已过公布时点的事件应拒绝冻结。
5. 输入有依据的EPS与DCF假设，检查期间、数量级、WACC>g与一年后状态要求。
6. 设置DeepSeek后分析选定材料，核对证据、usage和再次调用的缓存；这会使用你的API额度。
7. 保存研究快照，核对导出版本及原文下载。

这些是用户操作清单，没有写入测试文件或自动化测试脚本。初次交付只做代码阅读；用户随后授权的AAPL真实强制抓取已执行，成功项和限制分别记录在[原始诊断](11-AAPL-data-review.md)和[修复后结果](12-research-repairs.md)，不代表其他分支或整体验收已通过。

## 7. 界面方向

采用冷灰背景、蓝墨正文、蓝色操作、绿/红技术区间标记；证券标题使用衬线字体，数据使用等宽字体，中文正文使用系统字体。页面的主体是可追溯区间与资料覆盖，而非行情图或收益宣传。已考虑窄屏、键盘焦点、中文错误和空状态，不依赖远程字体/图标库。

使用frontend-design与ui-ux-pro-max技能作设计参考。技能检索的营销落地页/手写字体结果不适合此研究工作台，未照搬；采用其通用交互和可访问性原则自行组织研究布局。按用户要求未进行截图、浏览器或运行式视觉验收。

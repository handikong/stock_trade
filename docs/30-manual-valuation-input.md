# 手工财务输入、程序估值与技术支撑压力

2026-11-05[网页研究录入](36-web-workspace.md)已开发：七字段EPS＋PE粘贴、预览、草稿、本人确认保存，复用同一公共方法及固定日期/哈希/追加版本；旧草稿可继续，新增预测需新建输入。页面不展示已停用财报/电话会/PE获取/AI原文分析。旧输入仍兼容后端读取。本轮无应用/浏览器/API运行或自测，待用户验收。

2026-11-05最新PE也由用户录入，默认[七字段JSON与提问](templates/gpt-manual-eps-pe-prompt.md)，[当前规则/命令](35-manual-eps-pe.md)。自动PE公共入口已停用，研究只请求行情，不使用旧自动PE补齐；v1/v2及旧v3输入保留兼容。

2026-11-05最新默认仅录入[三个EPS字段](templates/gpt-eps-only-prompt.md)，PE由[独立指标适配层](34-eps-only-automatic-pe.md)取得。下方v1/v2作为兼容格式，不再要求用户每次返回大JSON。普通研究/levels可获取PE，录入和cached只读；财报/电话会/身份/AI链路继续停用。

2026-11-03最新默认改为[精简预测EPS＋历史PE输入v2](33-compact-eps-pe.md)，[提问全文已内嵌JSON](templates/gpt-eps-pe-prompt.md)，不用再附大模板。只有EPS平均值也能计算，不要求DCF/实际TTM/折算率；旧v1及下方规则继续兼容，仅适用于完整旧格式。

2026-11-03已提供独立`stock-input`入口：默认粘贴/文件预览不写库，--save草稿、--confirm确认、--show/--history查询及--template模板；不需启动网页或stock-research获取行情。见[录入/技术/推送验证顺序](32-cli-verification-workflow.md)，本轮未实际运行。

2026-11-03最终需求覆盖早期自动研究规则：**不再抓取或读取财报/电话会议来研究，也不自动抓财务预期或网站简析；用户向GPT取得并核对财务输入。程序仍计算EPS×PE、DCF，以及基于K线的技术支撑压力。** 本轮已修改后台和命令行逻辑，网页录入后续开发；没有启动应用、运行命令/测试、请求来源或模型。

2026-11-03后续已接[技术与基本面结合层](31-fundamental-technical-confluence.md)：原技术强弱之外增加每区间估值支持/冲突/分歧/缺失与研究共振。JSON新增可选technical_alignment供未来EPS目标折算，缺失不影响原估值但不能直接用于当前支撑压力；DCF默认同日参照。配置与FT01—FT10见专题。本次仍未实际运行或测试。

## 1. 当前流程

1. 优先复制[精简GPT提问模板（内含JSON）](templates/gpt-eps-pe-prompt.md)，按真实日期查询。需要完整v1/DCF时才使用[旧提问模板](templates/gpt-manual-valuation-prompt.md)与[旧JSON结构](templates/manual-valuation.template.json)。
2. 系统校验资料；未确认只保存草稿。GPT不能在数据里塞confirmed=true替本人确认。
3. 本人核对后显式确认。系统按股票保存版本，以Decimal计算EPS×PE，复用公共FCFF计算器计算手工DCF；不从原始财报重建TTM或推演假设补值。
4. 查询/命令行/飞书买点研究共享最新确认输入。缺资料只提示，不阻塞行情技术和退出预警。
5. 日线缓存、MA/ATR、技术支撑压力强弱与第二模块的趋势/策略逻辑保留，估值与技术区间分别展示，不混成一个所谓保证价。

手工方式让版本与输入可控，不能保证GPT的数据或研究假设正确；人工确认不等于公司确认或交易风控通过。

## 2. 停用与保留范围

| 范围 | 当前行为 |
| --- | --- |
| stock-research普通/force刷新 | 只刷新/复用行情技术，再读取手工估值缺项 |
| SEC/第三方财务、业绩附件、电话会议、预期/日程、网站RSS、自动估值 | 公共刷新入口返回disabled，不因force、旧配置enabled=true或旧任务恢复 |
| DeepSeek原文分析/搜索、URL原件下载队列 | 拒绝新增或将旧排队项标停用，不发请求 |
| build_report默认报告 | 不读取旧财报/电话会正文或扫描旧AI分析；旧来源字段为null，估值来自manual_valuation |
| 旧资料/算法/计算器记录 | 原文件和数据库记录保留；内部include_history显式读档，不进入当前估值或通知 |
| 单股研究/通知 | 手工估值缺失、未确认或待复核直接提示，不回退旧自动值 |
| 第二模块 | 行情、原色带、EMA回踩、MACD箭头、共同退出、回测交易规则不变 |

低层老获取实现以私有legacy保留供日后查阅，不由正常研究入口调用。旧手工EPS/DCF计算器格式仍可计算归档，但不自动转为带来源、期间和确认记录的新当前输入。没有新增网页控件；旧页面尚不能完整显示/录入新手工契约。

## 3. 手工数据契约

`schema_version=manual-valuation-v1`，详见空白JSON。额外未知字段拒绝，防止拼错后静默忽略。数值为十进制字符串，null是缺失；EPS以每股报价币种计、PE以倍数计。证券代码/币种必须匹配已知资料；未知类型可由本人明确声明security_type=EQUITY，不能覆盖已知ETF或冲突币种，不额外查询身份。

| 区域 | 语义 |
| --- | --- |
| as_of / latest_report_period_end | 手工资料日、本人已核对的最近财报期；不以保存日替换，不表示自动获知以后新报 |
| current_ttm | 已披露完整约12个月稀释EPS、起止日、准则/口径与来源；不能两季简单年化 |
| observed_pe | 外部观察PE、原股价/EPS/时点/口径；不因最新行情改写旧观察值 |
| target | 目标估值日、EPS期间/口径、正常化说明、保守/基准/乐观EPS与目标PE、各项依据及来源 |
| dcf | 可为空或待补；完整时使用手工路径计算，不读取旧SEC字段 |
| sources | id/title/publisher/url/published_at/accessed_at/supports；链接仅引用，导入不访问 |
| missing_fields / risks | 输入者的缺项/风险；程序独立验证，不仅信任GPT自报清单 |

source_ids必须存在于sources；完整项目需来源访问时间（含时区）和supports。未知发布时间可null。系统检查时间/格式/引用，不声称访问过或核实了链接语义。

**EPS期间与模型。** target_ttm是目标日之前12个月，期末必须等于目标日；ntm是资料日之后12个月，必须对齐该期间；fy1是明确起止的供应商下一财年。显示时保留实际使用的类型与期间，不把FY1换名为目标TTM。三个情景同一口径，eps_origin区分机构一致预期、官方指引、研究假设。EPS非正可存为不适用资料，不生成正PE估值。当前TTM、观察PE、目标EPS、DCF独立列缺口，不用一个模型的输入填另一个模型。

**DCF路径。** 金额与股数声明amount_scale/shares_scale，均为units或均为millions；两者不能混。税率、WACC、长期增长为小数。years需2—15年完整EBIT/税率/DA/capex/经营营运资本增加，路径从as_of开始按未来整年计算，不把旧假设改标今日。需明确现金/债务/其他优先索取权/稀释股数、行业适用性、来源、稳定期/股份薪酬政策，WACC大于长期增长。未输入year_one_state，不生成一年目标价；输入完整时DCF目标日为资料日一周年，与EPS目标日期分别展示。三情景EPS不能用来倒推DCF输入。

## 4. 草稿、确认、版本和复核

- `manual_valuation.evaluate/preview`校验并计算预览，不联网、不写库；有效结构的缺值可存草稿。
- `manual_valuation.save(...confirm=False)`保存草稿；`confirm=True`须有基本信息和至少一个完整/不适用项目，不能确认全空模板。API确认须提供预览input_hash；确认参数在数据对象外部。
- 按证券文件锁串行存档。相同状态最新内容去重，改变追加版本。新草稿不覆盖最新确认；最新确认失效也不回退旧确认。
- 普通读取重新检查证券口径/输入/目标日期，过期或冲突标needs_review并撤下当前计算价格；不自动改日期。可用MANUAL_VALUATION_REVIEW_DAYS配置复核间隔，默认不固定天数。
- 不再读取旧财报索引来触发更新；财报日历未接入，是否已发布新报由用户核对并更新输入。不能承诺及时识别所有新财报。
- 永久保存资料日、输入哈希、来源、确认时间、版本；手工记录不进入当前历史回测，不使用未来资料改写历史收益。

## 5. 命令与后台接口

以下命令由用户执行，本轮未实际运行：

```bash
# 行情/技术照常处理，手工资料没有就提示
.venv/bin/stock-research GOOG
# 只读缓存和当前确认手工数据，不联网
.venv/bin/stock-research GOOG --cached
# 先自行填写真实资料，导入仅保存未确认草稿
.venv/bin/stock-research GOOG --valuation /你的路径/GOOG-manual.json
# 本人核对后显式确认，后续普通查询复用
.venv/bin/stock-research GOOG --valuation /你的路径/GOOG-manual.json --confirm-valuation
```

不能将空白模板当成真实数据确认；--cached不能同时导入。普通导入命令仍会处理行情技术；财务获取链路始终关闭。

后台新增POST `/api/v1/research/{symbol}/manual-valuations/preview`（请求体为数据JSON）和POST `/manual-valuations`（请求体为`{data,confirm,expected_hash}`，hash使用预览结果）。现有访问口令及同源限制保持，尚无新网页UI。

报告新增manual_valuation（状态、当前输入、各模型缺项、程序计算结果、来源、草稿与确认信息）；旧valuation_draft不再作当前输出。通知只读此结果，版本引用manual_valuation和market；发送/去重/真实成交边界不变。

## 6. 用户核对清单

| 编号 | 预期 |
| --- | --- |
| MV01 | 股票/币种/日期/类型/来源引用无效时中文拒绝，不确认未知数据 |
| MV02 | 无手工输入显示缺项，不发SEC/预期/RSS/电话会/AI请求，不回退旧自动估值；force只更新行情 |
| MV03 | 普通导入仅草稿；显式确认才读取，GPT内嵌confirmed字段拒绝，API预览hash变化拒绝 |
| MV04 | FY1/NTM/目标TTM不混，三情景与实际EPS分开，来源和盈利质量说明保留 |
| MV05 | 同内容最新状态去重，变更追加，新草稿不覆盖确认；目标过期后不回退旧价格 |
| MV06 | Decimal EPS×PE和公共DCF计算器，缺值不填0，非正EPS不套倍数，DCF单位/输入完整性检查 |
| MV07 | 命令行/研究/通知复用手工版本，缺数据不阻塞技术和退出，支撑压力仍取日线计算 |
| MV08 | 旧来源公共入口与旧排队搜索/下载被停用；默认报告不读取/发布旧原件派生结论 |
| MV09 | DCF目标日以资料日周年标注，缺year_one_state不造目标价；复核间隔可配，未接自动新报感知 |

本轮只阅读代码、推演调用路径、状态/金额/日期/边界，未新增测试文件、未执行测试或应用命令、未抓取来源/调用模型/发送群消息。网页和实际运行由用户后续核对。

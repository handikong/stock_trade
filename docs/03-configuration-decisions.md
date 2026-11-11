# 配置契约与设计决策

[仓位管理](45-position-planning.md)的风险初值位于`config/portfolio-policy.json`（`PORTFOLIO_POLICY_FILE`）；现金、可用现金、参考本金、净入金和本金亏损比例在网页按账户输入。仓位数与股数按账户、大盘和组合风险动态计算。

[大盘简报](44-market-brief.md)沿用`POST_CLOSE_SCHEDULE_FILE`，默认北京时间周一至周六07:00、12:00补跑截止，飞书独立开关。`MARKET_BRIEF_SLOPE_SESSIONS`、`MARKET_BRIEF_VOLUME_HIGH`、`MARKET_BRIEF_VOLUME_LOW`、`MARKET_BRIEF_LEVEL_BUFFER_ATR`可配置，报告记录所用参数。

每周估值沿用`POST_CLOSE_SCHEDULE_FILE`。仓库配置启用`weekly_valuation_enabled`，`weekly_valuation_time`默认`08:00`，固定北京时间周六，当天可补跑；未声明开关的旧自定义配置默认停用。估值数据独立保存，详见[每周自动估值参考](43-weekly-valuation.md)。

2026-11-05新增[指数候选流动性配置](39-index-universe.md)：`US_UNIVERSE_POLICY_FILE`默认`config/us-universe-policy.json`，20个完整交易日、日均成交金额100000000 USD、价格5 USD、来源最长7日、请求间隔0.5秒均可配。`config/us-index-members.json`是有出处/实际取得日的当前来源快照，SP500使用SPY持仓代理，不视为精确官方指数授权数据。人工增删优先于同步；未取得数据不视为低流动性，新UNKNOWN不加入、旧UNKNOWN保留。仅显式导入更新关注名单，不改MG7回测默认或自动下单；详见UNIV规则与U核对。

2026-11-05新增[盘后任务配置](38-post-close-schedule.md)：POST_CLOSE_SCHEDULE_FILE默认config/post-close-schedule.json，启用、Asia/Shanghai、2026-11-06起、06:00/07:00、补跑截止10:00、轮询30秒；时间顺序/时区校验，保存每个Job的名单/目标交易日及配置快照。当前开关/窗口投递前复核，不因改配置为同交易日创建第二份任务。仅自动BUY，不接账户金额；需后台常驻，不是Codex/OS定时器。

2026-11-05网页参数：本版仅US/USD，A股入口disabled；持仓数量/均价正值、版本校验与Decimal汇总，不预填真实账户本金。扫描refresh默认false，手动勾选才更新；两个工作台任务类型按全局active_key去重。RESEARCH_DISPLAY_TIMEZONE用于页面时间，APP_TOKEN沿用可选本地口令，不新增多用户配置。七字段预览保存固定NTM日期，“新建输入”才开新期间。见[网页契约](36-web-workspace.md)。

2026-11-05最新改为[EPS＋PE全部手工输入](35-manual-eps-pe.md)。新增MANUAL_INPUT_POLICY_FILE，默认config/manual-input-policy.json保存USD/ntm/unknown盈利与PE分母/quartiles约定；可明确配置后新导入，不重写历史。自动PE配置enabled=false且公共获取入口已停用，单改旧enabled/force不恢复，旧数据不回退。行业PE可省略，未知口径只输出估值假设，原技术/共振配置不变。

2026-11-05当前默认为三档EPS JSON＋系统自动PE，字段和配置见[docs/34](34-eps-only-automatic-pe.md)。新增AUTOMATIC_PE_POLICY_FILE：config/automatic-pe-policy.json定义输入NTM/USD/未知盈利口径约定、历史窗口3/5年、主5年、至少8个季频样本、PE缓存168h/失败冷却30m/样本最大150天/行业资料400天、Yahoo定义与显式行业代理映射。EPS原件固定日期与口径，变更配置不重写旧情景。只有有依据确认定义后才能将假设用于技术确认；旧v1/v2配置兼容，普通cached/通知不联网。上述均非自动验证通过的生产参数。

2026-11-03最新输入契约：默认精简manual-valuation-v2（预测EPS＋历史PE窗口/统计/口径/来源），--template输出v2，v1兼容。EPS仅mean可用，范围同时缺失时三情景共用mean并标识；不要求DCF及technical_alignment。历史PE窗口显式录入，不硬编码5年；Forward PE分母匹配才参与技术确认，TTM跨期仅算假设。同币种、股票、日期、盈利口径校验及原复核/共振配置不变，详见[规则及字段](33-compact-eps-pe.md)。旧v1未来目标仍需折算输入，不能套用于v2当前前瞻参照。

2026-11-03综合判断新增FUNDAMENTAL_TECHNICAL_POLICY_FILE，默认config/fundamental-technical-policy.json：DCF允许资料日间隔0天、共振最低strong及2个独立模型，均可配置。manual-valuation-v1新增可选technical_alignment（股权要求收益率、明确终点价格不含分红口径及来源），用于未来EPS目标折算；无输入不猜。旧JSON仍可计算原估值。见[结合口径与配置](31-fundamental-technical-confluence.md)。

2026-11-03最终配置：第一模块切换手工财务输入、程序估值与K线技术计算。--valuation导入manual-valuation-v1草稿，加--confirm-valuation显式确认；MANUAL_VALUATION_REVIEW_DAYS可选、默认不固定复核天数。原财报/预期/RSS/搜索/自动估值公共入口停用，旧enabled/force不恢复，行情参数保留。见[当前契约](30-manual-valuation-input.md)，下方自动研究配置只作旧记录。

2026-11-03：新增stock-research --details（与json互斥）、RESEARCH_DISPLAY_TIMEZONE=Asia/Shanghai及EXTERNAL_BRIEFS_POLICY_FILE。默认表格、facts-limit兼容及估值v2 components/partial见[报表](28-research-console-report.md)。公开RSS来源/缓存/重试/节选见config/external-briefs.json和[简析](29-external-briefs.md)。DeepSeek原文分析已停用，不存在自动回退；下方旧预算参数只兼容存档，不表示可启用原文分析。

2026-11-01通知范围：stock-notify新增可选位置参数股票代码，例如`stock-notify NVDA --send --refresh`；未填或run沿用全池，history保留。指定单股不修改股票池；--refresh仅刷新所选范围，--scan-id可筛选其中该股票。扫描scope/requested_symbol与结果scope/symbol分别记录，单股/批量共享事件去重键。语义与NF12—NF14见[群推送专题](27-signal-research-feishu.md)。

2026-10-31群通知配置：新增本地FEISHU_WEBHOOK_URL、可选FEISHU_SIGNING_SECRET（均SecretStr），FEISHU_TIMEOUT_SECONDS默认15秒（3～60），FEISHU_MAX_MESSAGES_PER_RUN默认20（1～100）。凭据不进档案/日志，目标按地址哈希去重。stock-notify默认预览，--send显式投递；--refresh只更新行情，--cached-research只读研究，--retry-failed/--retry-unknown人工选择重试。未设定时任务。精确语义与用户验收见[推送专题](27-signal-research-feishu.md)。

2026-10-31报表配置：研究回测新增risk_free_annual=0与annualization_sessions=252，均为统计假设，不是已取得的市场利率。CLI --risk-free-rate/--annualization-sessions可覆盖，--report模式只改变统计、不改变交易；增强报告保存独立analytics.methods并在再次读取时沿用。口径见[终端报表](26-console-backtest-report.md)。

2026-10-31新增`config/backtest-policy.json`，用户确认本次回测总资金10000 USD，可用--cash覆盖；不改实际本金基准。仓位/风险/费用/滑点/结算/排序均为可改研究模板，具体数值、Decimal语义及未实现真实账户能力见[组合回测](25-portfolio-backtest.md)。既有原买点/三项退出不变，尚未确认的收盘保护线建议不自动生效。

2026-10-31新增展示参数`stock-trend --strategy-details`（须配合--strategies）：默认改为配对过程，加此参数恢复逐项条件视图；JSON保留原字段并增加flows。该参数不影响计算、退出规则或资金，配对格式版本独立于交易算法；见[展示语义](24-signal-flows.md)。

2026-10-30最终退出配置：exits新增fast_ema_period=5、slow_ema_period=10，快＜慢；enabled与两策略冻结保护线语义保留。旧ema_below_closes仅兼容存档、不参与当前退出。原色带与macd_arrow周期分别复用，不混成同一套MACD。当前版本和旧代码注释备份见[共同退出](23-common-exit.md)。以下两日收低EMA和单独死叉为旧版说明，不再执行。

2026-10-30新增`band-strategies.json.exits`：enabled=true、ema_below_closes=2、macd_low_lookback=10、macd_stop_buffer_atr=0.3，旧配置缺段补默认并输出完整版本。EMA保护线仍复用原stop_buffer_atr，均线复用pullback.ema_period；MACD退出复用macd_arrow周期。范围、冻结、禁用和版本语义见[技术退出](22-strategy-exits.md)。初值未通过收益验证。

2026-10-30新增`config/watchlist.json`：MG7首次种子名单、名称和版本；仅首次初始化读取，后续名单由`stock-watchlist add/remove`修改数据库，不因重启或修改种子文件自动重置。扫描继续使用现有色带/双策略配置，不新增交易门槛。日期、缓存、去重与版本语义见[股票池说明](21-watchlist-scan.md)。

版本：v0.1；日期：2026-10-23。

## 1. 配置与状态分离

配置是用户期望的规则；现金、持仓和成交是事实；仓位建议、风险余量和状态是计算结果。用户不能通过修改“当前现金”参数改变真实账本；纠正数据必须使用可审计的导入修正/冲正流程。

配置使用 DRAFT → SCHEDULED → ACTIVE → SUPERSEDED 生命周期。每个版本保存创建/生效时间、作者、修改原因、父版本、差异、影响预览摘要。旧版本不可覆盖。

系统可以为用户预填初始偏好，但不自动激活。126k 等未来本金变化也不通过代码发布完成，应通过设置或真实现金流处理。

比率在存储中统一使用 0..1 小数，界面显示百分比；金额带 ISO 币种；日数带自然日/交易日类型；所有时间明确时区。缺省/null 与 0 不同，不能隐式互换。

## 2. 账户和资金参数

| 配置键 | 类型/单位 | 约束与语义 | 初始草稿 |
| --- | --- | --- | --- |
| account.base_currency | 币种 | 必填；不可从股票市场或语言猜测 | 未设置 |
| account.display_timezone | IANA 时区 | 不影响交易所交易日归属 | Asia/Shanghai |
| account.market_calendar | 日历标识 | 与证券交易所对应，支持节假日/半日市 | 美股交易所日历，提供方待接入 |
| risk.reference_principal | 基准币种金额 | >0；不是实际现金字段 | 125000，仅参考用户描述 |
| risk.reference_started_at | 时间戳 | 必填；决定现金流和风险基准 | 未设置 |
| risk.protection_mode | principal / peak / both | 最高点模式须有单位净值基准 | principal |
| risk.cashflow_policy | fixed_initial_loss_budget | 第一版按 EQ-02；不自动扩大追加资金的损失预算 | fixed_initial_loss_budget |
| risk.principal_loss_fraction | 比率 | 0≤r<1；0 表示不分配新增风险预算，不能保证现实零损失 | 0.05，未激活 |
| risk.peak_drawdown_fraction | 可空比率 | 仅 peak/both 必填；不得因默认值暗中启用 | null |
| risk.risk_amount_basis | principal / equity | 单笔/组合风险百分比的计算基数 | principal |
| goal.annual_return | 可空比率 | 评估目标，不参与风险额度和信号判定 | 0.15，非收益承诺 |
| book.tactical.cap_fraction | 比率 | 机动资金占比上限；含已预约投入 | 0.10，未激活 |
| book.capital_basis | principal / equity | 资金池占比基数；按本金时外部流入如何重新分配需显式操作 | principal |
| book.core.cap_fraction | 比率 | 与机动上限之和≤1；未分配部分为待用现金 | 0.90，包含现金，未激活 |
| book.tactical.review_interval | 复查频率 | 决定何时评估补充/调整资金池 | 首次设置时选定 |
| account.cash_availability_policy | 券商数据 / 保守账本 | 不把未确认可用资金当购买力 | 保守账本 |

占比上限限制新投入与新增计划；持仓因价格上涨超过金额上限时生成再平衡复查，不自动把未实现盈利当作新违规买入。当前市值仍须接受账户总风险和集中度检查。

## 3. 风险和仓位参数

| 配置键 | 定义与校验 |
| --- | --- |
| risk.watch_usage / restricted_usage / defensive_usage | 对亏损预算使用率的三个阈值；0<watch<restricted<defensive<1；越线状态独立处理 |
| risk.watch_new_risk_multiplier | WATCH 状态下新风险折减，0..1 |
| risk.recovery_margin / recovery_sessions | 恢复滞回幅度与完整交易日数；必须防止状态抖动 |
| risk.per_trade_fraction.core / tactical | 每笔计划风险上限；必须>0，且≤相应资金池/账户风险上限 |
| risk.total_planned_risk_fraction | 已持仓＋已预约计划的总计划风险上限 |
| book.core.max_risk_fraction / tactical.max_risk_fraction | 分池风险上限，与资金占比上限不同 |
| risk.operational_reserve_amount / gap_reserve_amount | 从到保护线的缓冲中保留的金额；不能当收益损失重复入账 |
| risk.stress_scenarios | 命名情景、证券/行业冲击、汇率冲击及允许账户损失；缺失不算通过 |
| portfolio.max_equity_fraction | 股票＋股票 ETF 的总风险资产金额上限，不能因基金名称绕过 |
| portfolio.max_security_fraction | 同一证券在全部资金池的合并市值/预约金额上限 |
| portfolio.max_sector_fraction / max_theme_fraction | 含 ETF 成分/行业映射；映射不足时采用配置的保守上限 |
| portfolio.max_positions.core / tactical / total | 持仓只数上限；同一证券跨池在总数中去重，在池内各占一个名额 |
| portfolio.minimum_cash_fraction | 不得由新计划侵占的现金比例；若已有持仓导致不满足则生成调整草稿 |
| execution.max_participation_fraction | 计划金额占标的近期日成交额比例上限 |
| execution.slippage_model / fees | 价格偏差、固定费用、变动费用，按账户和证券配置 |
| execution.fractional_shares / quantity_step | 来自券商能力；未知时按整股处理 |
| execution.stopout_cooldown_sessions | 止损后新开仓冷静期，减仓不受限 |
| execution.plan_exception_policy | 允许有记录的盘中例外或禁止；例外不能跳过账户风控 |

这些数值未由用户确认，生产配置保持未设置。开发和回放可加载显著标记的研究配置，测试数据不得反写到真实账户。

## 4. 策略与研究参数

| 分组 | 主要配置 | 语义/限制 |
| --- | --- | --- |
| universe | security_types, price_min, market_cap_min, liquidity_window, median_dollar_volume_min, spread_max, listing_age_min | 价格币种必须明确；ETF 不强制套用股票市值字段 |
| universe | abnormal_return_window/limit, volatility_limit, event_exclusions | 阈值可调，命中原因可解释；不能宣称可以识别全部操纵股票 |
| fundamentals | industry_profile_id, required_metrics, metric_constraints, review_expiry | 每行业适用字段、方向和阈值；未知不自动通过 |
| fundamentals | thesis_checks, invalidation_checks, manual_evidence_policy | 观察指标与证据；模糊的 AI 信心不能替代可执行条件 |
| discovery | benchmark, lookbacks, weights, top_n, overextension_limit | 权重有效且归一化；比较使用同币种、同期间口径 |
| trend | short_window, long_window, slope_window, average_method | short<long；不足历史数据时不生成趋势结论 |
| structure | lookback, pivot_left, pivot_right, atr_window, atr_method, merge_tolerance_atr | 所有点必须按确认时点可知；确定性聚类与排序 |
| entry | pullback_window, zone_touch_tolerance, max_distance_atr, entry_band, max_entry_price, volume_filter | 相对区间或价格生成后固定到计划快照；价格必须为正 |
| entry | min_net_reward_risk, no_resistance_policy | STR-03 必须有可解释压力区间；核心无可见压力时可采用明确声明的跟踪退出政策 |
| regime | enabled, benchmark, gate_expression | 只影响新风险预算/入场；不是盈利承诺 |
| exit | structure_buffer_atr, confirmation_bars, confirmation_timeframe, trailing_mode | 长期与机动配置隔离；退出触发价与保证成交价区分 |
| exit | partial_profit_rules, max_holding_period, thesis_review_interval | 长期默认没有固定小幅盈利即卖出或一个月到期规则；如开启必须预登记 |
| tactical | configured_stabilization_bars, stabilization_reference_window, max_tranches, tranche_schedule | 企稳窗口与此前观察段不重叠；总额/总风险在首笔前锁定 |
| tactical | max_holding_period, expiry_action | 可按自然月/日/交易日配置；初始偏好一个自然月；到期提醒人工退出 |
| events | earnings_entry_policy, window_before/after, holding_review_policy | 未知时间不是不存在事件；持仓风险评估独立于入场政策 |

第一版策略启用需要存在完整、通过约束校验的档案。无需让用户逐个填写所有技术字段：界面可提供带说明的研究模板，一次载入、预览和修改；模板版本与生产账户是否激活分开保存。

开发期示例：短/长均线 50/200、斜率观察 20、ATR 14，可用于验证配置流转和历史长度检查；这些窗口不是推荐盈利参数，也不是写在策略函数里的常量。任何使用者都可以修改，修改后产生新策略版本。

## 5. 数据、计划和通知参数

| 配置键 | 语义 |
| --- | --- |
| data.provider_by_dataset / max_age_by_dataset | 日线、盘中价、财报、ETF 成分、汇率独立来源及容忍延迟 |
| data.missing_required_policy | 第一版固定为阻止对应新增风险评估通过；可以显示研究草稿 |
| jobs.post_close_delay | 相对交易所实际收盘的延迟；等待数据完整后运行，不按固定北京时间调度 |
| jobs.filing_refresh / reconciliation_schedule | 披露和账本复查频率；任务失败显式展示 |
| plan.valid_sessions / activation_cutoff | 计划有效交易时段、盘后激活截止规则 |
| notifications.channels / quiet_hours | 用户选择的渠道与安静时段；风险通知能否越过安静时段由用户设置 |
| notifications.cooldown / escalation / stale_after | 去重、升级与过期消息政策 |
| notifications.intraday_enabled / session_scope | 常规/盘前盘后选择；与数据源覆盖和券商执行能力一致 |
| ai.enabled / provider / model / budget | AI 可关闭；关闭不破坏记账、风控、规则评估和提醒 |

实时数据订阅未配置时，盘中模块显示未启用或延迟模式，不能使用演示数据假装运行。通知渠道未配置时保留站内事件，不声称外部消息已送达。

## 6. 配置修改生效

1. 编辑形成新草稿，校验字段和跨字段约束。
2. 用最新有效账户快照预演：风险线、可用仓位、受影响持仓/计划、是否更宽松。
3. 用户选择生效时间；普通设置可立即/下交易日，风险放宽默认下交易日。
4. 立即放宽时显示影响并记录理由；这是产品内有意义的变更确认，不是要求开发者在每次改代码前找用户批准。
5. 生效以原子方式切换版本，重评受影响计划，暂停不再合规的剩余买入；不撤销真实成交。
6. 生效时数据过期则保持待评估状态；风险参数不能通过预览时的旧余额绕过执行时检查。

审计、正确金额口径、交易事实不可伪造和未知数据不能伪装通过属于软件正确性约束，不提供隐藏的“忽略一切”开关。用户改变业务需求时可以调整规则实现，但必须保留可追溯历史。

## 7. 决策记录

| 编号 | 决策 | 原因/影响 |
| --- | --- | --- |
| D01 | 先完成设计文档，再按后续指示编码 | 用户明确要求把规则先存到项目 |
| D02 | 所有偏好参数可配置 | 用户明确拒绝将金额、比例固定死 |
| D03 | 默认草稿本金保护，利润回撤限制独立可选 | 用户明确接受利润回撤 |
| D04 | 第一版现金流政策采用固定初始损失预算 | 防止存取款伪装收益或自动扩大损失预算；公式公开 |
| D05 | 盘后定计划，盘中提醒，人工成交 | 匹配时间安排并减少盘中无目的交易 |
| D06 | 核心与机动分别管理，共用总风险 | 保留有限左侧偏好，同时限制风险外溢 |
| D07 | 长期持有逻辑与机动到期/止盈独立 | 防止小赚就跑和失败波段转长线 |
| D08 | Go 业务服务＋Python 共享规则/研究包＋PostgreSQL | 匹配用户技能，避免两套风控公式；不先拆微服务 |
| D09 | AI 为证据辅助层，非交易裁决者 | 保留可解释、可验证和无 AI 时的核心能力 |
| D10 | 首次上线前做规则回放与模拟闭环 | 验证执行正确性；不能据此推断未来盈利 |
| D11 | 个股研究作为首个独立交付模块 | 用户先讨论该模块；不依赖账户本金即可研究，交易许可仍需完整风控 |
| D12 | 财报前瞻、官方业绩材料与管理层指引属于核心研究范围 | 用户随后取消电话会获取及必需性；预期、实际、指引分开，材料存档 |
| D13 | 程序采集/存档/计算，模型基于原文分析 | 防止模型记忆或猜测冒充最新事实；模型可辅助查找但必须回源 |
| D14 | 今日DCF价值与一年后估值分别展示 | 避免将内在价值误标成一年市场价格预测；均为假设情景 |
| D15 | 趋势/策略/动态观察池设为第二产品模块 | 用户确认讨论方案并要求写入项目 |
| D16 | 三态趋势与判断状态分离，主周期与背景周期分别显示 | 防止未知被当震荡、短期反弹被当长期反转 |
| D17 | 一个主观察池，按策略独立记录机会 | 趋势改变无需复制股票，历史与旧计划可以追溯 |
| D18 | 下降左侧修复与震荡区间采用不同模板 | 避免趋势改变后继续使用失效买点或改名掩盖失败 |

用户已确认的是产品需求；D04、D08 等是本次提出的可调整设计选择，不宣称已由用户逐项批准。

## 8. 首次启用待填项

计价币种、数据源及预算、券商/账户限制、CSV 字段、实际现金与持仓、风险基准时间、参数模板、通知渠道、资金使用期限。缺少这些项不阻塞模块实现和模拟测试；正式账户激活时按相关功能逐项校验，不要求一次填齐与当前功能无关的字段。

## 9. 个股研究配置扩展

`research.*` 与 `valuation.*` 配置键详见 [个股研究模块第14节](07-stock-research-module.md)。遵循本文版本、生效、单位与未知值语义。研究页可编辑假设并试算，但情景假设不是财报事实，研究计算不改变账户风险配置。

独立研究不要求账户基准币种已填写，使用证券报价币种并明确标识。DCF现金流币种不一致时需要有时点的汇率和一致折现假设，不能沿用这一便利跳过估值币种校验。

用户于2026-10-24指定财报默认只取最近2期，使用`FINANCIAL_REPORT_COUNT`（1–20，默认2）统一控制财报期数、结构化财务范围与默认AI财报选择。旧`FINANCIAL_HISTORY_YEARS`、`FINANCIAL_QUARTERS`和`SEC_ARCHIVE_LIMIT`已不再参与计算，已有.env中的旧键由配置加载器忽略。改变期数后原快照/原文保留，正常刷新重新建立对应范围的缓存，不将旧缓存视为新范围已更新。定期财报与业绩发布稿是不同材料，只取得结构化事实不等于完整基本面研究；电话会已取消必需性。

随后取消独立身份查询；行情自带的币种/类型仍用于计算口径，不为公司简介增加请求。限流退避参数`YAHOO_COOLDOWN_SECONDS`默认120，独立于行情缓存，force不能绕过。可选`FMP_API_KEY`仅启用预期/日程备用分支，权限按账号实际可用性判断，不保证免费。`MATERIAL_LINK_LIMIT`默认3，控制每期附件数量，超限保留说明。详见[修复说明](12-research-repairs.md)。

最新用户变更：取消电话会自动获取及必需性，取代此前要求电话会完整覆盖的条款；不新增启用开关。材料缓存新增策略版本，普通刷新不沿用旧转录抓取策略。指引仍独立研究，以SEC业绩附件或导入的公司官方原文为依据；没有找到不代表公司没有披露，不能用市场一致预期或模型假设填补。

## 10. 第二模块配置扩展

最新财报规则：财务事实、披露索引及原文/业绩附件不设TTL，完整记录直接复用。`DISCLOSURE_CACHE_HOURS`保留兼容键名，仅控制一致预期、日程、EPS修订及趋势。后续由发布事件追加新报和修订，保留旧版；日历触发本轮尚未接入。首次获取、缺失补齐、范围/版本变化及显式人工同步可请求来源，同版本原件复用。

最新DeepSeek规则：首次完整取得财报且无对应分析、实际新增未覆盖的财报/修订/补充原文版本时才分析；其余永久复用本地返回。缓存判断只依赖原文类型、期间和内容哈希，已完成结果不因模型/提示/token参数、行情、当前预期或估值假设变化重新付费。旧动态上下文保留原时点并单独提示。`--analyze`可手动继续失败任务，`--force`只同步来源；默认查询和进程重启不重复重试收费。财报公共方法已共用于命令行/网页，未来日历接同一入口，详见[生命周期](15-financial-report-lifecycle.md)。

2026-10-24随后新增两个可修改配置文件：`config/valuation-policy.json`控制自动估值草稿的PE分位、增长情景、预测年数、WACC来源等；`config/technical-strength.json`控制技术结构的强/一般/弱门槛、独立接触、角色确认、量能与时效条件。可用`VALUATION_POLICY_FILE`和`TECHNICAL_STRENGTH_FILE`切换路径。配置读取时保存版本，不将模板当成用户已经确认的数值或交易指令。普通查询重算变化的派生结果，cached只读并提示旧版本；详见[估值草稿](13-valuation-drafts.md)及RESEARCH-02。

趋势周期/分类阈值/切换确认、策略适用性、自动入池与观察提醒参数见 [第二模块第11节](08-trend-strategy-module.md)。它细化本文件的trend/structure配置，复用第一模块价格结构，不能建立第二份互相矛盾的设置。数值参数仍按版本管理，不将窗口、持仓数或金额写死。

2026-10-24用户启动第二模块趋势阶段后，新增`config/trend-policy.json`（当前标准SMA50/200）与`config/trend-short-history.json`（仅2026样本、1—2月预热、3月展示，SMA20/30）。`TREND_POLICY_FILE`、`TREND_HISTORY_POLICY_FILE`可改路径，CLI可用`--policy`指定。窗口、斜率、ER、位移、边界触及与确认/保持均显式配置且有顺序校验；共用第一模块ATR/拐点算法及左右窗口。两套窗口代表不同研究周期，短样本不能冒充长期判断。模板始终是未验证研究，不赋予交易许可；详见[趋势命令](16-trend-cli.md)。

用户反馈AAPL旧版待确认过长后，算法版本新增证据阶段与阻塞解释。阶段复用上述斜率、价格位置、均线排列、ER/位移及结构检查，没有另一套隐含阈值；它不改变确认规则。历史分段键增加阶段及未满足检查编号，数值小幅变化不单独拆段。阶段枚举、派生优先级和缓存语义见[趋势命令第4.1及8节](16-trend-cli.md)。严格确认偏迟、保持被短期ER打断仍待讨论，不按单段行情降门槛。

用户随后指定自己的富途公式并确认绿带偏上、红带偏下、震荡另判。新增`config/futu-macd-policy.json`及`stock-trend --method futu-macd --start ...`。长EMA10/20、DEA9、短EMA3/10、过滤10根及价格字段都显式配置，`--policy`切换；无旧SMA/ER/拐点门槛。EMA首值、CROSS与FILTER语义保存于参数快照，改语义需算法新版本。当前仅输出偏向和信号，震荡未评估；原方法暂保留默认，不将此偏向默认为已确认三态趋势。见[富途专题](17-futu-macd.md)。

2026-10-25最新优化：该文件增加独立`trend`配置段，主趋势层可用`enabled=false`关闭；没有该段的旧自定义配置继续仅输出原色带。包含进入确认、初始结构/备用窗口、破坏与恢复缓冲、转弱观察、最长观察升级、持续反向门槛及连续价格确认。ATR/拐点窗口复用现有公共设置；原价MACD必须配同口径原价ATR/结构。默认模板进入2根、破坏/恢复缓冲0.3 ATR，反向5根和0.2 ATR加2根价格确认；仅研究初值，未运行验证。旧锚点不会按窗口或时间失效，阈值按设置时ATR冻结，仅新结构可收紧。参数、语义与未执行的用户验收场景见[富途专题第7节](17-futu-macd.md)。

区间波段STR-05新增下沿邻近范围、企稳确认、上沿目标区域、区间有效突破、成本后空间阈值与独立退出/期限参数。修改趋势或策略参数会重评相关机会与计划，不追溯篡改历史成交及此前分类。

2026-10-27买点配置`config/band-strategies.json`更新为`ema20-peak-pullback-v5`：默认仅启用EMA_PULLBACK，回调EMA独立20周期；高点回看20根、高点前上涨回看10根，上涨至少3个高点当日ATR，高点日收盘离EMA至少2 ATR，回踩邻近容差0.25 ATR。这些波段参数仍是未验证初值；配置可通过`--strategy-policy`切换。高点当天须原始绿带，确认严格使用下一交易日开盘对前日EMA、收盘对当日EMA，不要求突破前高。

新增用户指定的`pullback.max_consecutive_red_bars=5`，合法整数0～120，0表示一根红带即失效。旧配置缺少此字段时默认补5，并输出实际完整配置。第6根起回踩策略按下降处理，失效边界随持续超限日期推进，转绿不清除；上涨起点≥边界、高点>边界且满足原有上涨背景才可重新取得资格。回踩与确认均检查，计数包含预热期，不因展示区间或旧高点移出窗口而复活。新算法版本为`ema-peak-pullback-next-session-v5`；原始色带、旧结构主趋势快照及其他策略草稿不改动。

旧pullback.lookback、max_break_below_ema_atr、max_distance_ema_atr、max_distance_trigger_atr字段移除，旧配置明确报错；价格缓冲与最大风险距离仅提供本策略的参考价格/风险信息，不再作为其技术信号过滤。算法版本及完整配置哈希随结果导出，策略每次从绑定行情快照计算，不误用旧门槛结果。详细口径与用户核对清单见[EMA20高点回踩策略](19-ema20-pullback.md)。

2026-10-29配置升级为`multi-strategy-ema-v5-macd-icon1-v1`，新增`macd_arrow`：enabled=true、short=12、long=26、m=9，各周期整数且short<long。旧配置缺段默认启用该策略并输出实际配置；可显式enabled=false关闭。箭头策略独立于回踩配置，汇总算法`multi-strategy-ema-pullback-v5-macd-icon1-v1`，其独立算法`macd-icon1-buy-v1`。不生成未讨论的止损/追价/目标价，也不自动分配资金账户；见[图标1规则](20-macd-reversal-discussion.md)。

2026-11-11策略配置新增 `face`。仓库默认启用，旧自定义配置缺该段时关闭。默认CCI周期14、上穿阈值−110、均线下穿后至少8根、候选计数窗口5根。按同一行情快照计算，FACE参数进入策略配置哈希。见[FACE策略](51-face-cci.md)。

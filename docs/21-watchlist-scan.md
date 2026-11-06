# MG7股票池与双策略批量扫描

2026-11-05扩充为[纳指100＋标普500候选流动性导入](39-index-universe.md)：518只来源去重候选，经20日均成交金额/最低价筛选，显式stock-universe --apply写现有关注列表；MG7仍只是首次默认种子，不靠改种子文件给既有库加股票。人工add/remove写独立覆盖记录供后续指数同步遵循；原手动移除不自动恢复，历史/持仓保留。后台06:00/07:00共用扩容后的活动名单，两策略和回测默认组合不改。真实导入与限制见专题；无新增/运行测试。

2026-11-01公共scan新增可选symbol，供`stock-notify NVDA`复用单股计算/持久化；不添加/恢复关注名单，不初始化MG7。档案新增scope=SINGLE/WATCHLIST及requested_symbol，旧记录视为批量。普通stock-watchlist scan调用方式和默认全池行为不变；通知命令及NF12—NF14见[单股群推送](27-signal-research-feishu.md)。本轮未运行测试。

2026-10-31新增独立[信号研究与飞书群推送](27-signal-research-feishu.md)：stock-notify复用scan当前有效事件，普通scan行为不变、不调用财报/AI或发送。只有stock-notify --send执行外部投递；--refresh显式更新行情。历史/旧行情不推送，失败/未知记录并人工选择重试。未设定时任务，未实际发送或运行测试。

2026-10-31展示更新：scan按股票输出配对的买入—预警—卖出过程，并存入扫描结果供last读取；旧批次仍可按原事件视图读取。旧行情标记和信号去重不变，见[完整过程](24-signal-flows.md)。未运行测试。

2026-10-30最终退出更新：当前扫描采用[冻结保护线＋三项状态共同确认](23-common-exit.md)，保存exit_alerts预警层级变化和exit_tracking状态。旧v1退出已注释停用，历史事件保留但不代表新版仍触发，当前结果查看last。本轮未运行测试。

2026-10-30后续增量：扫描现同时记录BUY/EXIT技术信号，`history --kind BUY/EXIT`可分别查询。退出按原买点关联并单独去重，多条原买点同日退出不合并；买点主键保持兼容。详见[退出规则](22-strategy-exits.md)。以下“退出未实现”是早期阶段记录；真实持仓、成交和资金管理仍未实现。

日期：2026-10-30。按用户要求实现自选池、两策略扫描、汇总和信号历史。**本轮只做代码逻辑审阅，未运行测试，由用户测试**；尚未实际初始化数据库或获取7只股票。

## 1. 范围

首次使用初始化AAPL、MSFT、GOOGL、AMZN、META、NVDA、TSLA。Alphabet选择GOOGL，不重复纳入GOOG。可以添加、移出和查看；无信号也保留关注，移出保留历史。

复用EMA20高点回踩、MACD12/26/9图标1，独立判断、允许同日分别命中。两套公式、5根红带限制和默认参数不变。仅技术买入提示，不生成成交。

没有实现自动全市场筛选、完整机会生命周期、财报门槛、账户/持仓、退出、交易计划、定时任务和外部推送。扫描不启动网页/worker，不抓财报，不调用DeepSeek。

## 2. 使用

在项目根目录运行。当前虚拟环境已补启动文件；其他环境通过`pip install -e ./research`安装入口。

```bash
source .venv/bin/activate
stock-watchlist list
stock-watchlist add AVGO
stock-watchlist remove AVGO
stock-watchlist list --all

# 默认各股票最后可用日；已有行情直接复用，缺失才请求yf
stock-watchlist scan

# 不联网；2026年1月起预热，展示3月起买点
stock-watchlist scan --offline --history-start 2026-01-01 --start 2026-03-01

# 手动刷新全部池内股票的日线
stock-watchlist scan --refresh --history-start 2026-01-01 --start 2026-03-01

# 显式终点表示历史回放
stock-watchlist scan --offline --history-start 2026-01-01 --start 2026-03-01 --end 2026-10-23

stock-watchlist history
stock-watchlist history --symbol NVDA --strategy MACD_ICON1 --start 2026-03-01
stock-watchlist history --json --limit 500
stock-watchlist last
stock-watchlist last --json --output data/watchlist-scan-review.json
```

找不到命令时，用`.venv/bin/stock-watchlist`或`.venv/bin/python -m stock_research.watchlist_cli`替代。`history --json`提供event_id，`stock-watchlist revisions <event_id>`查看所有版本；`last --scan-id <scan_id>`读取指定扫描。

各子命令支持`--json`和`--output`，不覆盖已有文件。日志实时输出stderr，结果stdout。退出码成功0；scan含失败/旧行情/警告2；命令或持久化失败1；中断130。

## 3. 配置与缓存

- `config/watchlist.json`只用于首次初始化，之后增删名单存数据库。移出MG7成员不会在重启后自动加回；add可恢复。重复add/remove幂等，全部移出后scan报股票池为空。
- 股票代码标准化为大写并去掉可选开头`$`，不查询证券身份；录入不保证代码存在，获取失败单独报告。
- 策略配置沿用`config/band-strategies.json`，可传`--strategy-policy`；色带配置沿用`config/futu-macd-policy.json`，可传`--policy`。突破/红带反弹草稿默认关闭。
- 展示默认各股范围内最后可用日，预热默认展示起点所在年1月1日。跨年或统一起点时显式指定`--history-start`。`--start`只裁剪输出，预热仍参与EMA和连续红带计数；预热不足明确警告。
- 普通扫描已有行情就复用，不因旧缓存自动更新；缺行情才请求yf。`--refresh`显式更新全部股票，仍遵守限流冷却；失败保留旧行情但显示警告。
- `--offline`不联网，但会计算并保存指标和扫描，并非只读数据库。

## 4. 数据截止与当前信号

扫描开始时按XNYS日历、收盘和现有延迟配置固定最近已完成交易日。周末/假日截止映射至前一交易日；拒绝未完成和未来终点。

只有未显式指定`--end`、计算截止等于固定最近已完成日、行情通过现有TTL/完整性/覆盖检查、检查时应有交易日一致、且没有获取/预热/截止等警告，才列入“最近已完成交易日的信号”。计算与新鲜度档案还必须相同或完整计算窗口及复权口径逐项一致。

其余可算结果显示为历史/待复核。单股ERROR不等于没买点，也不中断其他股票。每股保存实际数据截止和原因，不用批次日期代替实际日线日期。历史回放不会仅因TTL过期返回失败，但实际缺数据/预热警告仍为退出码2。

“首次发现”表示首次写入本地档案，不表示今天发生。首次回放可发现大量旧信号，它们放在历史区；重复扫描标记已记录，不重置首次时间。这里不发送外部通知。

参考价为策略价格口径，默认调整价，不是成交报价。收盘确认，最早下一交易时段使用；行情有效不等于账户风控通过。现有复权档案回放不等于无偏收益回测。

## 5. 保存与并发

沿用研究数据库，启动时增量建表，不删除原数据：

| 表 | 内容 |
| --- | --- |
| watchlist_state | 首次种子版本与名单 |
| watchlist_entries | 关注/移出状态 |
| watchlist_scans | 每次各股结果，包括未命中、配置和输入版本 |
| watchlist_signals | 股票＋策略ID＋信号日唯一事件、首次发现时间 |
| watchlist_signal_revisions | 完整信号证据与来源修订，只追加 |

事件主键为三项确定性哈希，修订主键包含事件和完整内容/来源。同输入不新增事件或修订；参数/行情变化可以新增修订，首次时间不变。保存整套策略参数，因此修改另一策略也可能产生修订，但事件数不变。

重新扫描后旧日不再命中，不删除历史。`history`表示“曾经发现”，不是当前机会；某次scan的signals/signal_records才是那次范围和配置下实际命中，可用last核对。历史默认最近100条，最多10000条。

刷新取得相同日线时按内容复用旧指标，分别保存新鲜度检查与策略计算的行情ID；比较完整窗口与复权口径，不篡改旧指标来源。每条信号保留原始证据、行情版本、完整参数与预热起点。

当前仅本机单用户：文件锁串行保护修改/扫描，每股独立事务。失败保留已完成股票，Ctrl-C标记中断并可用last读取；强杀可能留下running批次，可看已落库范围。只读查看不占写锁，修改名单需等扫描结束。不提供跨机器共享库并发写保证。

## 6. 用户核对清单

本轮只审阅调用、时点、去重、事务、异常隔离、缓存复用和输出，没有执行以下检查：

| 编号 | 预期 |
| --- | --- |
| W01 | 首次list为7只；remove后重开仍移出；add恢复且历史不变 |
| W02 | offline无行情明确失败，其他股票继续，无网络/财报/模型请求 |
| W03 | 同窗口重复scan第二次首次发现0，事件ID/首次时间不变 |
| W04 | 同窗口/配置与stock-trend --strategies比对，两策略日期/价格/证据一致 |
| W05 | 同股同日两策略保留两事件，图标1不叠加EMA红带限制 |
| W06 | 旧行情/历史end不进当前区；旧信号首次发现不冒充今日买点 |
| W07 | 参数/行情改变保留旧版本；不再命中的旧事件不进入当前扫描结果 |
| W08 | 单股失败继续，重复写去重，并发拒绝，中断后last可读部分结果 |
| W09 | 相同窗口刷新允许复用指标；窗口/复权不同不能借另一档案声明新鲜 |
| W10 | 周末/未收盘日按最近完整日；跨年预热不足警告，未来end拒绝 |

NVDA旧预览04-02、07-08、08-04只能作为同数据/初始化口径对照，不能声称本次集成已运行命中。

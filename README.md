# 个人股票交易决策系统

用于管理美股持仓、评估买入和加仓计划、扫描策略信号、研究估值与支撑压力，并通过飞书发送提醒。支持研究美股及 ETF，实际交易由本人在券商端执行。

A 股提供独立的人民币持仓总览和收息关注池。点击工作台顶部「A 股」登记沪深股票和 ETF，侧栏「收息关注池」可更新中证红利成分、手动增删关注。沪深股票的 EPS／PE 资料核对命令见 [A 股持仓与收息关注池](docs/49-cn-sources.md)。

## 1. 配置 .env

在服务器项目根目录复制示例。已有 `.env` 时，保留原配置，只补所需字段。

```bash
[ -e .env ] || cp .env.example .env
```

以下是可直接使用的默认值。仅使用网页、日线研究和策略扫描时，API 凭据可留空。默认使用项目目录下的 SQLite 数据库 `data/research.db`。

```dotenv
# 默认 SQLite。使用外部数据库时再填写连接配置。
DATABASE_URL=
POSTGRES_HOST=
POSTGRES_USER=researcher
POSTGRES_DB=stock_research
POSTGRES_PASSWORD=
RESEARCH_DISPLAY_TIMEZONE=Asia/Shanghai

# 可选的 DeepSeek 大盘简报解读
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 可选的飞书群推送
FEISHU_WEBHOOK_URL=
FEISHU_SIGNING_SECRET=

# 可选的 Alpaca 盘中监控
INTRADAY_ENABLED=false
INTRADAY_FEISHU_ENABLED=true
APCA_API_KEY_ID=
APCA_API_SECRET_KEY=
```

### 飞书群推送

在目标飞书群打开 **群设置 → 群机器人 → 添加机器人 → 自定义机器人**，复制完整的 Webhook 地址到 `FEISHU_WEBHOOK_URL`。如果机器人启用了「签名校验」，将同一个机器人的签名密钥填到 `FEISHU_SIGNING_SECRET`。Webhook 和签名密钥保存在服务器 `.env`。 [飞书自定义机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)

### Alpaca 盘中监控

登录 Alpaca，选择 **Paper Trading** 账户，在 **API Keys** 区域查看或生成密钥。将同一对 **API Key ID** 和 **Secret Key** 分别填到 `APCA_API_KEY_ID` 和 `APCA_API_SECRET_KEY`。Secret 只在生成时显示一次，遗失后需要重新生成并同时替换两项。 [Alpaca 控制台](https://app.alpaca.markets/account/login) · [官方密钥说明](https://alpaca.markets/learn/api-key-security-best-practices-for-alpaca-builders)

填齐后设 `INTRADAY_ENABLED=true`。若只想更新盘中行情，可设 `INTRADAY_FEISHU_ENABLED=false`。启用盘中飞书还需要配置上面的群机器人。

### DeepSeek 大盘解读

登录 DeepSeek 开放平台，在 **API Keys** 页面创建密钥，填到 `DEEPSEEK_API_KEY`。模型和地址可直接使用上面的默认值。该密钥用于大盘简报解读，留空时照常生成程序的技术研判。 [DeepSeek API Keys](https://platform.deepseek.com/api_keys) · [官方接入文档](https://api-docs.deepseek.com/)

### 外部数据库（可选）

需要使用已有 PostgreSQL 时，从数据库控制台或管理员获取连接串，填到 `DATABASE_URL`。也可留空该项，填写 `POSTGRES_HOST`、`POSTGRES_USER`、`POSTGRES_DB` 和 `POSTGRES_PASSWORD`。两种都配置时优先使用连接串。

## 2. 部署和更新

Linux 服务器准备 Git、Bash 4.4+、`flock` 和带 `venv` 的 Python 3.11+。启用盘中监控时，脚本还会用 `curl`、`tar` 和 `sha256sum` 下载并校验对应架构的 Go 官方包，支持 Linux x86_64 和 ARM64。

首次部署，在配置好 `.env` 的项目根目录执行：

```bash
bash deploy.sh
```

已经部署的服务器，拉取新版代码或修改配置后执行：

```bash
bash update.sh
```

首次部署安装 Python 依赖并初始化数据库。日常更新拉取代码，按盘中配置准备 Go 并构建行情程序，成功后重启后台。日常更新跳过 Python 依赖安装和数据库初始化。

从 A 股人气榜版本升级到收息关注池时，请先执行 `git pull --ff-only`，再执行一次 `bash deploy.sh`，以安装中证成分 XLS 所需的 `xlrd`。以后日常更新仍用 `bash update.sh`。

默认从 `http://服务器IP:8000` 打开工作台。后台日志在 `data/deploy/worker.log`。部署参数和访问方式见 [Linux 部署说明](docs/41-linux-deploy.md)。

## 3. 功能

| 功能 | 在哪里用 |
| --- | --- |
| 持仓与账户 | 美股「我的持仓」支持账户风险和市值汇总。A 股「持仓总览」单独录入持仓和人民币成本，用有效收盘价计算市值和浮盈亏。 [美股仓位管理](docs/45-position-planning.md) · [A 股持仓](docs/49-cn-sources.md) |
| 当日盈亏与净值 | 两个市场分别保存每个交易日的收盘净值，扣除外部净入金后显示当日盈亏。点击净值卡可看近一周、近一月、近一年。 [净值口径与操作](docs/50-account-equity.md) |
| 买入与加仓计划 | 「评估买入」结合技术、估值、现金、大盘及同向持仓风险，给出首仓、左侧或右侧加仓价带、最多股数和保护位。实际成交后手工登记。 [买入计划](docs/45-position-planning.md) |
| 股票池与策略 | 「扫描股票池」可手动增删并按公司总市值排序。默认同步富途美股热榜前 100 条，仅股票自动入池。扫描使用 EMA20 高点回踩和 MACD 零轴下金叉两套买点，并记录共用退出预警。 [富途热榜](docs/42-futu-heat-watchlist.md) · [策略扫描](docs/21-watchlist-scan.md) · [退出规则](docs/23-common-exit.md) |
| 个股研究 | 查询已缓存日线计算的支撑、压力和强弱，查看 EPS × Forward PE 三档估值及技术配合。每周刷新关注股估值，可在网页更正。K 线用于后台计算。 [每周估值](docs/43-weekly-valuation.md) · [技术与估值](docs/31-fundamental-technical-confluence.md) |
| 大盘与持仓复盘 | 大盘简报参考 SPY、QQQ 等基准、成交量、VIX 和宏观消息。持仓复盘列出近期走势、关键位和加减仓、止盈止损复核。 [大盘简报](docs/44-market-brief.md) · [持仓复盘](docs/46-holdings-brief.md) |
| 盘中监控 | 美股常规交易时段连接 Alpaca IEX，订阅 QQQ、SPY 和实际持仓，每 5 秒更新。跟踪保护位、破位和加仓条件，北京时间 02:00 起停止发起盘中飞书推送，行情继续更新至收盘。 [盘中监控](docs/48-intraday.md) |
| 飞书与推送记录 | 发送符合条件的策略、关注池、持仓复盘和盘中提醒。网页按北京时间日期和类型查看正文、接收结果及失败原因。 [飞书卡片](docs/37-feishu-brief-card.md) · [推送记录](docs/47-push-history.md) |
| 组合回测 | 命令行输出账户绩效、逐股盈亏和逐笔模拟买卖，含最大回撤与夏普率。默认初始模拟资金 10,000 USD，可更改。 [回测口径](docs/25-portfolio-backtest.md) · [回测报表](docs/26-console-backtest-report.md) |

## 4. 默认任务时间

以下均为北京时间。收盘行情和策略扫描按各市场交易日历运行。推送需要已配置飞书机器人。

| 时间 | 任务 |
| --- | --- |
| 每日 06:00 起 | 更新富途热榜，关注池有新增时推送。 |
| 有新美股收盘的日期 06:00 起 | 更新关注股完整日线。 |
| 有新美股收盘的日期 07:00 起 | 等日线任务结束后扫描，发现有效买点时推送。 |
| 周一至周六 07:00 起 | 美股周一开盘计划、周二至周五日报、周六周报，默认发送飞书。用 `market_brief_feishu_enabled` 控制。 |
| 周一至周五 08:00 起 | 复盘已登记持仓并推送。 |
| 周六 08:00 起 | 更新关注股估值，等待同日较早任务完成。 |
| A 股交易日 15:35 起 | 更新收息关注池、人民币持仓和大盘基准日线。 |
| A 股交易日 08:00 起 | 准备上个交易日行情并扫描两套买点策略。 |
| A 股交易日 09:00 起 | 扫描完成后推送有效买点、持仓复盘和已启用的大盘简报，09:30 停止补发。 |
| 周六 07:00／08:00 起 | A 股生成大盘周报、更新关注股估值。 |

盘后任务配置见 [post-close-schedule.json](config/post-close-schedule.json)。[A 股任务与验收](docs/49-cn-sources.md) · [美股盘中提醒配置](config/intraday-policy.json)

## 5. 常用命令

```bash
# 查询个股，或只读取已有支撑和压力
.venv/bin/stock-research GOOG
.venv/bin/stock-levels GOOG --cached

# 查看计划任务和最新大盘简报
.venv/bin/python -m stock_research.schedule_cli status
.venv/bin/python -m stock_research.schedule_cli market-brief

# 预览测试卡；第二条实际发送一条测试消息
.venv/bin/stock-notify test
.venv/bin/stock-notify test --send

# 默认 MG7 组合回测，指定 10,000 USD 模拟账户总资金
.venv/bin/stock-backtest --cash 10000

# 追踪服务器后台日志
tail -f data/deploy/worker.log
```

更多规则见 [交易规则](docs/02-trading-rules.md)、[配置约定](docs/03-configuration-decisions.md)及[用户验收场景](docs/05-acceptance-roadmap.md)。

## 开发约定

页面、命令行和错误提示使用中文，关键业务逻辑保留必要的中文注释。代码交付采用静态审阅，实际运行测试和验收由用户执行。开发时按项目约定保留验收清单。

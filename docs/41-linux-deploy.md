# Linux 一键部署（无 Docker）

准备 Git、Bash 4.4+、`flock`（通常由 util-linux 提供）及 Python 3.11+（含 venv）。使用有项目写权限的普通用户。先把本项目提交并推送到自己的远程仓库，服务器第一次执行：

```bash
git clone <你的仓库地址> stock_trade
cd stock_trade
bash deploy.sh
```

后续更新执行 `bash update.sh`，它会复用同一部署流程。脚本会对当前分支执行 `git pull --ff-only`、创建或复用虚拟环境、安装 `research` 的 Python 依赖、建表和初始化名单，然后重启应用后台。拉取、依赖安装或初始化失败时，不会先停掉旧后台。SQLite 由 Python 自带的 `sqlite3` 模块提供，脚本先检查支持情况，无需另装数据库服务或 SQLite 命令行工具。已有 `.env` 不覆盖；缺少时创建 SQLite 默认配置，默认库文件为 `data/research.db`。需要飞书时另填实际的本机配置。

新数据库首次写入 `config/deploy-watchlist.json` 中的509只美股。这是从截至2026-11-04完整行情的纳指100和标普500代理筛选档案取得的固定初始名单，不是实时全市场榜单。初始化不下载行情、不扫描策略、不发送飞书；日线和实时榜单由后续盘后任务更新。数据库只要已有关注记录或初始化状态，就不会补写初始池，包括用户已全部手动移出的情况。仅初始化、不拉代码或启动服务时，可执行：

```bash
.venv/bin/python -m stock_research.deploy_init
```

如果要迁移本机已经积累的完整状态，先备份并恢复 SQLite 数据库到服务器，再运行脚本；种子只带初始股票代码，不包含旧行情、手工持仓、历史上榜次数和推送记录。服务器已有库不要直接覆盖。

后台固定一个 Uvicorn Worker，默认监听 `0.0.0.0:8000`，从外部打开 `http://服务器IP:8000`。服务器防火墙或云安全组需允许访问端口；公网访问建议放在 HTTPS 反向代理后。对外监听时，若未设 `APP_TOKEN`，脚本会生成口令存入服务器的 `.env`，不会打印或覆盖已有有效口令；网页要求输入该口令。若只需本机或反向代理访问，可执行 `STOCK_TRADE_HOST=127.0.0.1 bash update.sh`。

PID 文件为 `data/deploy/worker.pid`，日志为 `data/deploy/worker.log`。脚本使用本机部署锁，确认 PID 对应本项目进程后才发送 TERM；不按端口强杀别的服务。如果此前手工启动的旧后台占用8000，先由用户停止它，再执行脚本。脚本从回环地址等待端口开始监听，不通过实抓榜单或发送消息来验收。

```bash
tail -f data/deploy/worker.log
.venv/bin/python -m stock_research.schedule_cli status
.venv/bin/python -m stock_research.schedule_cli leaders
```

应用会在 SSH 断开后继续运行；脚本不会重启操作系统，也没有配置机器重启后的自启动。盘后计划仍按北京时间06:00取榜及更新行情、07:00等更新结束后扫描、10:00结束补跑。

用户验收：新库首次509只；重复部署保留手工增删和空名单；拉取失败不停止旧进程；未知进程占端口不杀；完成后只有一个后台并能查询计划。此轮未在服务器运行部署、初始化应用库或执行测试。

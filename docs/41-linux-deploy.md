# Linux 一键部署（无 Docker）

准备 Git、Bash 4.4+、`flock`（通常由 util-linux 提供）及 Python 3.11+（含 venv）。使用有项目写权限的普通用户。先把本项目提交并推送到自己的远程仓库，服务器第一次执行：

```bash
git clone <你的仓库地址> stock_trade
cd stock_trade
bash deploy.sh
```

`deploy.sh` 创建或复用虚拟环境、安装 Python 依赖、建表和首次导入名单，再启动后台。日常更新用 `bash update.sh`，仅对当前分支执行 `git pull --ff-only` 并重启后台，不安装依赖或运行部署初始化命令。依赖发生变化时，拉取后重新运行 `bash deploy.sh`。拉取、依赖安装或初始化失败时，不会先停掉旧后台。SQLite 由 Python 自带的 `sqlite3` 模块提供，脚本先检查支持情况，无需另装数据库服务或 SQLite 命令行工具。已有 `.env` 不覆盖；缺少时创建 SQLite 默认配置，默认库文件为 `data/research.db`。需要飞书时另填实际的本机配置。

默认启用富途热榜时，新数据库建表后等待首次富途榜单，不导入旧509只指数初始池。已有库也在第一次成功的富途同步中替换关注池，历史资料仍保留；立即同步可执行 `.venv/bin/python -m stock_research.futu_heat_cli sync`。只有明确停用富途热榜的配置，才在空库使用 `config/deploy-watchlist.json` 旧种子。仅初始化、不拉代码或启动服务时，可执行：

```bash
.venv/bin/python -m stock_research.deploy_init
```

如果要迁移本机已经积累的完整状态，先备份并恢复 SQLite 数据库到服务器，再运行脚本；种子只带初始股票代码，不包含旧行情、手工持仓、历史上榜次数和推送记录。服务器已有库不要直接覆盖。

后台固定一个 Uvicorn Worker，默认监听 `0.0.0.0:8000`，从外部打开 `http://服务器IP:8000`。系统无内置访问口令，旧 `APP_TOKEN` 配置会被忽略。用防火墙或云安全组只允许可信 IP 访问；公网访问可通过带访问控制的 HTTPS 反向代理。若只需本机或反向代理访问，可执行 `STOCK_TRADE_HOST=127.0.0.1 bash update.sh`。

PID 文件为 `data/deploy/worker.pid`，日志为 `data/deploy/worker.log`。脚本使用本机部署锁，确认 PID 对应本项目进程后才发送 TERM；不按端口强杀别的服务。如果此前手工启动的旧后台占用8000，先由用户停止它，再执行脚本。脚本从回环地址等待端口开始监听，不通过实抓榜单或发送消息来验收。

```bash
tail -f data/deploy/worker.log
.venv/bin/python -m stock_research.schedule_cli status
.venv/bin/python -m stock_research.schedule_cli leaders
```

应用会在 SSH 断开后继续运行；脚本不会重启操作系统，也没有配置机器重启后的自启动。盘后计划仍按北京时间06:00取榜及更新行情、07:00等更新结束后扫描、10:00结束补跑。

用户验收：新库首次509只；重复部署保留手工增删和空名单；日常更新不安装依赖、不运行部署初始化；网页无口令弹窗；拉取失败不停止旧进程；未知进程占端口不杀；完成后只有一个后台并能查询计划。此轮未在服务器运行部署、初始化应用库或执行测试。

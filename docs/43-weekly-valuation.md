# 每周自动估值参考

北京时间每周六08:00起，现有后台读取当时全部活动关注股，逐只重新请求 Stock Analysis 的公开财年 EPS 和历史季度 Forward PE。当天可补跑，周日不补前一天。先让同日热榜、行情和买点任务处理完，再启动周估值。每个周六只有一个任务，同日进程中断时从未写入的股票续跑。财报发布、估值修订和周中新加股票都由下个周六批次统一处理。

使用公开 [Forecast 示例](https://stockanalysis.com/stocks/aapl/forecast/) 和 [Ratios 示例](https://stockanalysis.com/stocks/aapl/financials/ratios/?p=quarterly)。取公开的首个预测财年、三档 EPS 及财年截止日。历史 PE 使用近五年有明确日期的正值季度样本，不含 Current；至少八个样本才计算 P25、P50、P75。币种读取预测页财务币种说明，省略币种时按美国站点美元默认值，并保留该取值依据。非美元财务 EPS 或已知股类尺度异常先留待核对，不折算成美元估值。数据源条款与访问限制见 [Terms of Use](https://stockanalysis.com/terms-of-use/) 和 [robots.txt](https://stockanalysis.com/robots.txt)。采集器尊重 robots.txt，按顺序限速请求，遇到拒绝、限流或网络请求失败停止本批，保留已保存数据。

按低档 EPS × P25、基准 EPS × P50、高档 EPS × P75 计算三档价格。纯自动试算要求同批完整资料、正值 EPS、美元每股口径和有效财年。EPS 是财年预测，历史 Forward PE 的分母口径待核对，价格只作情景参考。两类数据各保留实际取得时间，缺项保留最近可采用的旧值。网页、CLI及飞书读取同一版估值；技术共振仍要求可核对的可比口径。无需模型或财报原文。

## 网页更正

打开个股研究页，点击「更正估值」，可改财年截止日、三档每股美元 EPS、三档 Forward PE，也可补齐网站缺项。保存后立即重新计算；当前页显示更正时间及来源数据取得时间。三档需按从低到高排列，PE 须为正；EPS 为零或负时仍可记录，价格区间留空。可点「恢复自动数据」撤销全部更正；清空已更正字段再保存可恢复单项。若编辑期间其他操作已保存更新，重新打开最新版本后再更正。

周六仍会重新抓取。EPS 和 PE 分组：某组源值或 EPS 财年/口径发生变化时，该组采用新数据，另一组更正照常保留；抓取失败或源数据相同则保留原更正。更正与采集都追加至同一估值版本链，旧手工估值只可通过 `stock-input SYMBOL --show` / `--history` 查看。完整的网页更正可补足自动采集缺项，试算注明更正；已过期财年仍不展示价格。保存更正不会触发策略扫描或发送飞书。

## 操作

```bash
# 已部署服务器拉取代码并重启后台
bash update.sh
# 查看周六任务状态
.venv/bin/python -m stock_research.schedule_cli status
# 查看一只股票已保存的估值和技术数据
.venv/bin/stock-research AAPL --cached
# 可选：即时预览当前关注股的源数据，不写估值
.venv/bin/python -m stock_research.stockanalysis_mg7 --watchlist
```

网页「个股研究 → 前瞻估值参考」展示同一份缓存。全局开关、估值开关与时间见`config/post-close-schedule.json`。覆盖了`POST_CLOSE_SCHEDULE_FILE`的服务器需在所用文件中设置`weekly_valuation_enabled: true`和`weekly_valuation_time: "08:00"`。

2026-11-07本地来源预览尝试全部68只，67只有公开EPS表、48只有季度Forward PE表、45只同时有三档EPS和至少八个有效PE样本。这些数字表示源字段覆盖，尚未应用最新币种和试算校验。GOOG公开季度表缺Forward PE，未代用GOOGL；BRK.B预测EPS的股类尺度待核对。此预览独立归档，后台估值缓存由正式周六任务写入。

## 用户核对

- WV01 周六08:00以后入队，重启不重复，同日中断续跑；其他日期不补跑。
- WV02 使用最新活动关注池；新增成员在下一次名单快照中出现。
- WV03 同批正值数据可试算；缺项、少样本、非美元及股类异常分别提示，旧数据留存。
- WV04 网页、CLI及飞书读取同一版当前估值；旧手工估值仅供查档，待核对口径只作情景参考。
- WV05 网页更正或恢复后立即重算；股类、币种、三档顺序、日期、非正EPS和过期版本都各有对应反馈。
- WV06 周六源值未变或失败保留更正；EPS、PE分别接管本组变更，编辑并发按版本拒绝旧提交。

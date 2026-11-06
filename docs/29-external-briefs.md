# 公开简析与停用DeepSeek原文分析

2026-11-03最终覆盖：公开简析自动入口与配置已停用，研究不再读取RSS或旧摘要，用户自行向GPT取得并核对财务输入；程序仍计算估值及技术支撑压力。见[当前规则](30-manual-valuation-input.md)。下文RSS实现/旧GOOG结果保留历史，不表示当前会自动请求。

2026-11-03。用户取消DeepSeek原文分析，确认先读可公开访问的网站/机构简析、逐条附出处，覆盖以前首次/新报自动分析规则。

## 1. 来源

默认Seeking Alpha股票RSS，依据[官方目录](https://about.seekingalpha.com/feeds)，模板为https://seekingalpha.com/api/sa/combined/{symbol}.xml。仅获取RSS/Atom公开节选，不追下载文章全文/付费正文，不抓会议转录、不调用AI重写或翻译。作者观点不自动等于机构正式研报。

每条显示来源、作者自报、真实发布时间、链接和原语言节选；标题/短预览标headline_only，不当实质分析。主体与财报期未核实，不能仅靠日期绑定一期财报。

网页已核对目录，工具对部分XML不支持或访问失败；未通过项目命令证明源抓取成功。403/429、验证页、无相关摘要时如实缺失，不启用StockAnalysis网页爬取或机构付费研报。

## 2. 停用范围

财报公共入口只获取/归档，analyze_reports默认False、传True也不分析。analyze/call_model公开入口停止原文模型；旧实现保留为私有legacy，不由生产入口调用。旧CLI分析参数中文拒绝，API analyze返回410不入队；队列拒绝新增analysis，旧排队项返回disabled。原网页按钮停用，旧AI另作历史档案。

已有原文、事实、分段/用量和旧分析不删除；PE/EPS/DCF继续程序计算。旧手动资料搜索独立保留，不属于普通研究自动步骤。常驻服务需用户重启使新版生效，修改不能撤销已发出的请求。

## 3. 配置和持久化

config/external-briefs.json可改，EXTERNAL_BRIEFS_POLICY_FILE可切换，目前仅支持公开RSS/Atom，不是任意网页或机构API适配器。

| 参数 | 默认语义 |
| --- | --- |
| enabled | true，关闭后不抓取，旧档案留存 |
| cache_hours | 24小时动态索引，不改财报永久保存 |
| retry_minutes | 30分钟失败缓存/429来源级冷却，force不绕过429 |
| lookback_days | 45天按真实发布时间筛选，未知不补今天 |
| max_items | 展示5条，摘要优先、较新时间优先 |
| summary_max_chars/min_summary_chars | 最多保存1200字符，至少60字符才标实质公开节选；不是准确性评分 |
| include/exclude_keywords | 财报/业绩/指引候选，排除转录/诉讼等；匹配不等于核实 |
| sources | 1～5个有权读取的公开源，HTTPS模板、名称及参考说明，不写私有令牌 |

下载复用公开HTTPS校验及每次重定向校验，禁止内网，单源最多2MB，拒绝DTD/实体XML。源失败不阻断其他研究；配置错误不使已有报告崩溃。

复用快照表：external_brief_article按内容去重/留版本，external_briefs存索引和状态，brief_provider_limit记429。全部失败保留旧条目及原last_success_at且stale，不以失败时间刷新旧材料；部分失败也提示，缓存命中保留警告。

观点不进入SEC实际值、人工核对指引、EPS增长/WACC或历史回测。普通研究/发送当前买点读取公开源，cached和通知预览只读，技术扫描不请求，退出预警不等待研究。通知只节选前两条并附链接，完整档案本地保留。

## 4. 命令与验收

```bash
.venv/bin/stock-research GOOG
.venv/bin/stock-research GOOG --cached
.venv/bin/stock-research GOOG --details
.venv/bin/stock-research GOOG --json --output data/my-goog-briefs.json
.venv/bin/stock-notify GOOG --send --refresh
```

| 编号 | 用户核对预期 |
| --- | --- |
| BR01 | 普通/首次/新财报/推送不请求原文模型，不再参与42/16判断 |
| BR02 | 旧CLI拒绝、API410、旧队列disabled，档案保留 |
| BR03 | 公开摘要附来源/时间/链接/原语，标题与实质节选分开 |
| BR04 | 失败/无摘要明确缺口，不抓正文或用AI补写，其他研究继续 |
| BR05 | 失败旧材料标旧，索引TTL/失败缓存/429跨股票冷却生效 |
| BR06 | 同内容不重复，更新留版本，未知时间/主体/财报期不冒充确认 |
| BR07 | cached/预览不请求，技术扫描不隐式获取，观点不进入事实/指引/估值/回测 |
| BR08 | 源配置错误不阻断已有报告，页面不暗示可续跑原文分析 |

本次仅代码逻辑审阅，未运行新版命令/应用、测试、程序RSS抓取、模型或群投递，由用户验证。

## 5. 后续GOOG实际命令核对

用户于2026-11-03明确授权运行`.venv/bin/stock-research GOOG`并分析输出。01:52命令命中01:49既有RSS索引：来源状态ok、匹配8条，展示5条全部summary为空，实质摘要0条。另混入一条大盘文章及新闻集合页链接，不能认为已满足个股简析需求。本次没有重新请求RSS、下载文章、调用模型或群投递；抓取成功不等于摘要取得。详见[实际核对](../data/GOOG-research-review-20261103.md)，未修改来源或筛选代码。

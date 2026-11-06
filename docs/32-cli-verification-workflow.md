# 网页开发前的命令行验证流程

2026-11-05推送展示改为[简洁富文本卡片](37-feishu-brief-card.md)，GOOG无信号效果仍用`.venv/bin/stock-notify GOOG --research-only --refresh --send`查看。默认五项决策摘要，详细解释/编号留档；新旧卡片CLI兼容。未实际运行/投递，由用户验收。

2026-11-05用户已明确启动网页开发，交付[美股持仓/扫描/研究工作台](36-web-workspace.md)。本文CLI仍可独立核对，旧“网页待开发”由最新进度覆盖；不表示本清单已经通过。新WEB清单同样由用户执行，未运行页面/接口自测。

2026-11-05最新默认--template输出[七字段EPS＋PE](templates/gpt-manual-eps-pe-prompt.md)，行业PE可选。导入即预览计算，不需要获取PE缓存；普通research/levels仅刷新行情，旧自动PE已停用且不回退。当前操作和MP验收见[docs/35](35-manual-eps-pe.md)，下方自动PE流程仅留历史记录。

2026-11-05最新默认--template仅输出三个EPS字段，--file直接接收三值；旧v1/v2仍支持。使用[极简提问/JSON](templates/gpt-eps-only-prompt.md)，录入后查询stock-research取得历史/行业PE并计算，stock-levels可看技术与两组估值；cached不联网。详见[最新顺序、来源及EP清单](34-eps-only-automatic-pe.md)。旧文中“非cached只请求行情”已扩为行情＋独立PE指标，不恢复财报或AI。

2026-11-03默认模板更新为精简v2：[直接复制的提问与JSON](templates/gpt-eps-pe-prompt.md)、[新字段/计算/CP核对清单](33-compact-eps-pe.md)。--template输出v2，--file同时支持v1/v2，旧确认版本无需迁移。先用预测EPS与历史PE计算，不要求完整DCF；其余确认、技术核对和推送顺序不变。

2026-11-03用户要求先用命令行完成GPT数据录入、支撑压力比对与推送验证，再开发网页，避免遗漏。用户先选择沿用信号推送，随后补充需要没有信号也能测试；最终范围同时保留正式信号推送和独立连通性测试。

已开发入口，不等于已通过验证。本轮未执行这些应用命令、测试套件、来源抓取、模型或飞书投递；下列步骤由用户操作，验收结果目前全部待核对。没有新增自动化test文件/脚本/夹具。

## 1. 推荐顺序与副作用

| 顺序 | 命令 | 作用与副作用 |
| --- | --- | --- |
| 1 | stock-input GOOG --template | 输出空白模板，只读已知证券口径，未知币种/类型保持null |
| 2 | stock-input GOOG --file 数据.json | 默认预览、校验和计算，不保存、不请求行情/来源/模型 |
| 3 | stock-input GOOG --file 数据.json --save | 显式保存未确认草稿，其他当前确认不变 |
| 4 | stock-input GOOG --file 数据.json --confirm | 本人明确确认这份内容，保存确认版本；不要求先有行情 |
| 5 | stock-input GOOG --show / --history | 只读当前状态/版本历史 |
| 6 | stock-levels GOOG | 复用有效行情或补齐日线，单独输出技术与基本面对照，不扫描策略/通知 |
| 7 | stock-levels GOOG --cached | 固定已有行情比对，不请求行情或写库 |
| 8 | stock-notify test | 无需股票/信号的测试消息预览，不请求飞书或写档案 |
| 9 | stock-notify test --send | 无需信号，显式发送一条测试消息，写独立尝试档案 |
| 10 | stock-notify GOOG [--refresh] | 正式信号预览；refresh可更新行情，不发送 |
| 11 | stock-notify GOOG --send --refresh | 正式当前有效信号投递，沿用原去重，不凭空补造信号 |

所有命令在项目根目录运行，可加`.venv/bin/`。日志stderr、结果stdout；输入和支撑压力命令支持--json/--output，输出文件已存在时拒绝覆盖。预览与只读命令不初始化或迁移数据库；若配置远程数据库仍需连接读取，不等于禁止连接业务数据库。

## 2. 录入GPT数据

先使用[GPT提问模板](templates/gpt-manual-valuation-prompt.md)，附完整JSON结构。需要实际数据时本人询问、核对；系统不替你调用GPT。

```bash
# 生成空白文件，已有文件会拒绝覆盖；未知币种/证券类型必须本人填写
.venv/bin/stock-input GOOG --template --output data/GOOG-input-template.json

# 直接粘贴，默认只预览。结束后另起一行输入END回车，也可Ctrl+D
.venv/bin/stock-input GOOG

# 文件预览，核对来源、EPS期间、口径、盈利质量、模型价格及缺项
.venv/bin/stock-input GOOG --file data/GOOG-gpt.json
.venv/bin/stock-input GOOG --file data/GOOG-gpt.json --details

# 可选：先存草稿；草稿不是有效确认数据
.venv/bin/stock-input GOOG --file data/GOOG-gpt.json --save

# 本人已核对后明确确认。--confirm也可不带--file直接粘贴内容
.venv/bin/stock-input GOOG --file data/GOOG-gpt.json --confirm

# 查看保存后状态与历史
.venv/bin/stock-input GOOG --show
.venv/bin/stock-input GOOG --history --limit 20
```

支持`--file -`管道输入，最多1MB UTF-8，允许一层完整的外部```json围栏，不从额外说明里猜JSON；重复键拒绝，防止同名字段被静默覆盖。不把JSON中的confirmed字段当成用户确认。

独立录入直接复用manual_valuation.evaluate/save/read，不调用stock-research刷新；首次保存才初始化业务存储/创建本股票档案。缺少现有证券口径时，可明确填写security_type=EQUITY与报价币种；不能覆盖已知基金或币种冲突。空白模板不能确认，部分缺项可以草稿保存；确认至少需一个完整或不适用项目，其他模型仍分别列缺口。

预览输出input_hash。需要严格对照先前预览时，可把该哈希复制到确认命令：

```bash
.venv/bin/stock-input GOOG --file data/GOOG-gpt.json --confirm --expected-hash 预览输出的64位input_hash
```

不传expected-hash时，--confirm表示明确确认本次读入的内容，程序用本次校验哈希保存，不弹额外确认框。按证券锁和最新状态内容去重，改动追加版本，新草稿不覆盖确认；过期/冲突不回退旧值，详见[生命周期](30-manual-valuation-input.md)。

## 3. 独立支撑压力比对

```bash
# 仅处理行情/技术；有效缓存直接复用，缺少或旧行情补yf
.venv/bin/stock-levels GOOG

# 显式刷新日线；不抓财报/电话会议/预期/网站简析
.venv/bin/stock-levels GOOG --refresh

# 比较时优先固定缓存，避免每次价格数据变化
.venv/bin/stock-levels GOOG --cached
.venv/bin/stock-levels GOOG --cached --details

# 保存完整可比对数据，不输出整组K线
.venv/bin/stock-levels GOOG --cached --json --output data/GOOG-levels-review.json
```

默认输出最近完整日线/价格、行情状态/版本、手工版本、技术支撑压力及强弱、基本面配合/交集/共振、缺项和原因；details附全部技术点、角色确认、评分与估值逐模型依据。直接复用stock-research相同技术/综合渲染函数，不重新实现指标。

该命令不执行策略扫描、不产生真实仓位/成交、不发消息；缺少手工输入仍能看技术表。原估值/时点规则见[结合专题](31-fundamental-technical-confluence.md)：未来目标缺technical_alignment不参与当前判断，DCF默认同日，不能只改as_of日期凑共振，须核对预测路径。

## 4. 没有信号也能测飞书

Webhook及可选签名密钥只保存在本地.env（FEISHU_WEBHOOK_URL/FEISHU_SIGNING_SECRET）；不要粘到JSON、聊天、终端命令历史或仓库。已有适配器继续校验官方域名、签名、超时和业务码，不输出凭据。

```bash
# 不需要股票、行情或数据库，也不需要策略信号；不会发送
.venv/bin/stock-notify test

# 明确发送一条连通性测试消息到已配置的群
.venv/bin/stock-notify test --send

# 结构化结果也只含消息/状态/编号，不含Webhook或签名
.venv/bin/stock-notify test --send --json
```

以上两个--send例子是**替代操作，不必都执行**；每次显式调用都会发一条新编号消息。test操作不接受refresh/scan-id/cached-research/正式信号重试参数，不进入股票池扫描、研究、SignalDelivery或正式SENT去重。

消息明确标记“飞书连通性测试”，含测试编号和UTC时间。默认预览不创建档案；实际发送先写`DATA_DIR/notification-probes/<编号>.json`的SENDING状态，再请求一次飞书，记录SENT/FAILED/UNKNOWN；Webhook只记目标哈希。档案不可写时不发送。成功只说明接口确认接收，请本人对照群消息编号；不等于已读。

读超时、中断、发送后档案更新失败均保守标UNKNOWN、不自动重试。强制结束留下SENDING也按送达未知处理，须先看群再决定是否重新执行。重新执行不是重放旧正式信号，不应在未知时盲目重复。

## 5. 正式信号推送仍单独核对

```bash
# 看当前有效信号和消息内容，不发送；--refresh可更新GOOG行情
.venv/bin/stock-notify GOOG --refresh

# 确认内容后，发送当前有效的买点/退出/预警，研究只读手工数据
.venv/bin/stock-notify GOOG --send --refresh

# 只看正式投递记录，独立连通性测试不在该表
.venv/bin/stock-notify history
```

没有当前有效信号时，原正式命令仍不会发一条研究报告顶替信号。2026-11-05新增显式单股研究快照，可直接查看谷歌研究卡效果：

```bash
.venv/bin/stock-notify GOOG --research-only --refresh --send
```

去掉`--send`仅预览；该模式不扫描策略、无信号也发一条标记“手动快照（非信号）”的研究消息，独立归档不占正式去重。只测连通性继续使用`test --send`。两种手动消息SENT均不表示策略/真实信号去重已验收；完整规则和NF15—NF19见[群推送专题](27-signal-research-feishu.md)。本轮未运行命令或实际发送。

## 6. 退出码与入口安装记录

- 0：操作完成。预览0不等于已保存或已发送。
- 2：已输出，但资料缺项/旧行情/部分模型不可比，或飞书明确失败/送达未知等需核对；看结果说明，不能统一当崩溃或验收通过。
- 1：输入/配置/文件/基础行情等失败；130：主动中断。

pyproject.toml已登记stock-input、stock-levels，两个模块支持`python -m`。本机尝试仅离线注册入口（--no-deps --no-build-isolation --no-index）时因缺少setuptools.build_meta失败；未执行任何应用验证。随后按既有stock-research入口方式创建`.venv/bin/stock-input`、`.venv/bin/stock-levels`本地启动文件（有不同旧内容不覆盖），没有运行这些入口。已有stock-notify入口直接加载更新后的代码。

其他环境可按常规安装`pip install -e ./research`注册入口；若只缺启动文件且本包已是editable，也可直接使用：

```bash
.venv/bin/python -m stock_research.input_cli GOOG --show
.venv/bin/python -m stock_research.levels_cli GOOG --cached
.venv/bin/python -m stock_research.notify_cli test
```

## 7. 网页开发前保留的人工验收记录

| 编号 | 用户核对项 | 当前状态 |
| --- | --- | --- |
| CLI01 | 文件/粘贴/管道/完整围栏同内容同哈希；重复键/错股票/错币种拒绝，不乱提取正文 | 待用户执行 |
| CLI02 | 无库存预览不创建SQLite；空模板未知口径不猜，预览无来源/模型请求 | 待用户执行 |
| CLI03 | 草稿与确认分开，修改追加/同内容去重，show/history一致，expected-hash变化拒绝 | 待用户执行 |
| CLI04 | 部分缺项或不适用如实标识，EPS/DCF与手工值、期间、来源和数量级一致 | 待用户执行 |
| CLI05 | levels与research同行情/手工/配置时区间及结合结果一致，cached无行情请求 | 待用户执行 |
| CLI06 | levels缺手工资料仍可看技术，details/JSON含全部依据，拒绝覆盖既有导出 | 待用户执行 |
| CLI07 | notify test默认无网络/档案；无股票、缓存、数据库或信号也能预览测试内容 | 待用户执行 |
| CLI08 | test --send每次仅一条、带唯一编号，官方成功/拒绝/未知语义正确，档案不泄露密钥 | 待用户执行 |
| CLI09 | 测试消息不写正式SignalDelivery/股票池，不占正式信号去重；中断/超时不自动重发 | 待用户执行 |
| CLI10 | 有信号后正式预览/投递/去重正常，退出优先及手工研究缺项提示不变 | 待用户执行 |

后续网页工作仍待开始：复用同一输入校验/确认生命周期和技术/综合公共结果；在上述结果由用户核对并记录之后再做录入/展示，不另写规则。

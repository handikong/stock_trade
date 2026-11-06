# 三档EPS录入与自动PE指标

2026-11-05本路线已由用户后续要求停用：当前采用[EPS和PE全部手工录入](35-manual-eps-pe.md)，普通查询不获取PE，不回退本专题自动缓存；下方实现仅留档，不能当作当前操作指南。

2026-11-05最新用户要求：每次只向大模型取得三个EPS，返回JSON并手工录入；历史PE与行业PE由代码获取并计算。该默认路径覆盖docs/33的“用户也必须取得历史PE”流程。旧v1/v2档案继续兼容，不删除a.json或任何旧财报/研究档案。

## 1. 用户输入

新默认模板：[eps-only.template.json](templates/eps-only.template.json)，[极简提问](templates/gpt-eps-only-prompt.md)。对象只含eps_conservative、eps_base、eps_optimistic三个字段，十进制字符串或null，不能增加确认字段。错序/非数值拒绝，缺值可存草稿但不能确认完整EPS，非正EPS倍数法不适用。它们是用户研究情景，不假称分析师最低/均值/最高。

股票由命令指定。系统封装manual-eps-v3档案，记录录入日（RESEARCH_DISPLAY_TIMEZONE）、证券报价币种、固定NTM起止（录入次日至一年后）和当时input_convention盈利口径；读回不移动期间。报价币种未知时使用配置明确的美股USD约定，普通股适用性仍须行情确认，未知类型可留草稿、不猜成公司。仅三值无法验证大模型真实查询日/GAAP口径/来源；默认basis=unspecified，预览明确标注。若填的是财年、旧预测或其他币种，用户应更正，不能靠这三个数自动识别。

程序不请求大模型，不恢复财报、电话会、身份查询、EPS自动预测或原文分析。

## 2. 自动PE来源和缓存

`automatic_pe.refresh_history/refresh_industry`为独立指标适配层，`cached`只读缓存重算分位数。普通stock-research及非cached的stock-levels执行行情→历史PE→行业PE→手工计算，失败步骤相互独立。普通watchlist/趋势/回测不调用；stock-input预览/确认/显示及通知研究只读PE，不联网。

- **历史PE**：本机yf 0.2.66没有公开历史估值方法，适配层使用SDK网络对象调用Yahoo fundamentals-timeseries的quarterlyPeRatio/quarterlyForwardPeRatio；只请求比率，不请求财报正文/公司简介。不依赖升级SDK，不调用get_info，复用全局Yahoo限流冷却。私有SDK接口和上游字段可能变化，此轮未实际验证返回。PE数字统一12位小数，非正/非有限/未来/重复冲突样本剔除或明确拒绝。原始分期比率样本保存在新增快照，不拿今日EPS除历史股价补造PE，不声称无修订历史回测数据。
- **行业PE**：固定公开[NYU Damodaran美国行业PE表](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/pedata.html)，解析Forward PE/Trailing PE、公司数量、原始资料月份，保留抓取UTC时间。来源是发布者给出的行业口径，不伪装成本程序计算的同行中位数；年度表不是实时行业行情。最多接受配置指定资料年龄，按月份第一天保守计龄，不把刷新时间当新数据日期。
- 行业映射独立配置，当前GOOG/GOOGL映射Advertising且明确为广告业务代理，不声称完全代表Alphabet全部业务，不调用身份查询推断其他股票。其他股票无映射时行业缺失可提示，自身历史PE仍可计算。
- 新快照类型pe_history、pe_industry_table及各自_attempt，现有通用快照表无需新增业务表。成功数据只追加，失败保留旧记录，旧数据超过TTL不作为当前估值输入。按股票/全局行业本地文件锁去重；失败冷却和Yahoo全局429均不被force绕过。行业整表缓存跨股票共享。

## 3. 配置定义

`config/automatic-pe-policy.json`，可用AUTOMATIC_PE_POLICY_FILE指定。

| 配置 | 默认 | 语义 |
| --- | --- | --- |
| enabled | true | 只控制独立PE适配，不恢复旧财报链路 |
| history_years/primary_years | [3,5] / 5 | 两窗口均统计，主窗口可改，范围1～10年 |
| minimum_samples | 8 | 至少8个有效季度样本才计算分位，不冒充完整五年 |
| maximum_sample_age_days | 150 | 最新历史样本的最大日期间隔 |
| cache_hours | 168 | 网络缓存有效期；配置变更只重算，不改抓取时间 |
| failure_cooldown_minutes | 30 | PE步骤失败冷却，包括显式刷新 |
| industry_maximum_age_days | 400 | 允许年度资料代理的最大年龄，实际月份始终显示 |
| input_convention | USD / ntm / unspecified | 系统三值输入约定，未知盈利口径不猜；非一致预期认证 |
| yahoo_definition | unspecified | 供应商历史Forward PE的EPS期间和盈利口径；有依据核对后才配置为ntm等 |
| industries | GOOG/GOOGL广告代理 | 显式代理名称和限制，不按大盘行业自动填充 |

四分位用排序样本的线性插值，位置(n−1)×q，Decimal精度48，q=0.25/0.5/0.75。报告保留3/5年窗口、实际样本开始结束、数量、季频、未满窗口提示、来源和取得时间。优先Forward，只有Forward样本不足时才用有足够样本的TTM作为明确的跨期假设。选主窗口或可用备用窗口均保留实际窗口名称，不能把两年数据标成五年完整历史。

## 4. 计算与技术关系

自身历史估值=保守/基准/乐观EPS×PE的25/50/75分位；行业比较估值=三档EPS分别×同一个行业PE参照。并列表格，不相加、不混合权重、不把两组PE当EPS和DCF两个独立方法。缺一来源只排除对应一组，EPS录入和另一路仍可用。

默认EPS盈利口径和Yahoo Forward分母未核对，仍可显示**倍数假设**，不宣称同口径基本面确认。只有已保存EPS basis和PE definition盈利口径一致、NTM定义已确认且使用Forward时，才允许历史PE组进入现有技术比较。行业代理不自动加入技术共振投票。原共振模型数量门槛不变，不因两个PE参照而假造第二估值方法。

PE变化不改已确认EPS输入哈希，派生版本单独加入PE快照与配置，研究/levels/通知版本共享。预测期结束、证券冲突或配置的人工复核间隔触发时，不发布当前估值，不回退旧确认。新草稿不覆盖确认。原始三值反复导入以当次录入日固定期间，同日同值幂等，跨日录入是新情景快照；用户不能把重新粘贴旧数理解为已自动更新研究。

## 5. 命令

```bash
# 用户将模型返回三值保存为data/GOOG-eps.json后，预览与本人确认
.venv/bin/stock-input GOOG --file data/GOOG-eps.json
.venv/bin/stock-input GOOG --file data/GOOG-eps.json --confirm
# 查询行情/PE（缓存优先），计算历史和行业两组估值
.venv/bin/stock-research GOOG
# 独立技术比对也显示三值EPS及两组估值；cached不联网
.venv/bin/stock-levels GOOG --cached
```

首次无证券档案/类型未知时，先查询一次stock-research GOOG取得行情身份附带字段，再确认EPS；不追加get_info。--template现在输出三个null字段，旧v2模板仍可用明确文件取得。通知仍仅有有效信号且用户明确发送才发，不因EPS录入/PE抓取而发送。

## 6. 用户核对清单（本轮未执行）

| 编号 | 核对项 |
| --- | --- |
| EP01 | 仅三字段、字符串/null；错序/类型/多余字段拒绝，缺值/非正EPS不制造正估值 |
| EP02 | 股票来自命令、NTM按录入日固定、原始情景不冒充一致预期；新日/同日幂等和哈希符合规则 |
| EP03 | Yahoo仅请求两比率、季频样本真实数量/区间、Forward/TTM区分，未调用身份/财报/AI |
| EP04 | 3/5年分位与样本可核对，样本少/过旧/只部分窗口明确提示，不用当前EPS补造历史PE |
| EP05 | 行业表日期/月精度/样本数量/代理限制如实显示，未知映射/太旧/失败单独缺失 |
| EP06 | TTL命中无重复请求；force不绕过失败/429；同股票/全局行业锁去重，失败不覆盖旧成功 |
| EP07 | input只读预览、确认、研究、levels、通知计算一致，--cached不联网/写库，不自动发送 |
| EP08 | 未核对口径仅假设；确认定义后才可进入技术比较；行业参照不伪装第二独立模型 |
| EP09 | v1/v2读取不变，草稿不覆盖确认，过期清除当前估值，PE刷新不改EPS原件哈希 |

本轮仅阅读代码、上游接口实现和公开来源说明并推演逻辑，未新增/运行测试文件、临时断言脚本、应用/CLI、实际PE抓取、模型或消息投递。上游网络可用性、返回覆盖和页面解析仍须用户执行上述核对；未声称已取得GOOG实际历史/行业PE。

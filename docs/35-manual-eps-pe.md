# EPS及PE全部手工录入

2026-11-05后续已接入[网页研究页](36-web-workspace.md)：复制提问、七字段粘贴、预览、草稿、确认保存；复用本规则与CLI结果。A股暂不参与，不恢复任何财务/PE来源请求；网页尚未实际运行验证。

2026-11-05用户最新要求PE也由本人问大模型后录入，提供提问和返回JSON。覆盖docs/34自动PE默认流程，用户无需继续获取PE缓存。程序负责Decimal估值和技术K线，原自动PE档案保留。

## 输入和计算

[提问全文](templates/gpt-manual-eps-pe-prompt.md)已含[七字段JSON](templates/manual-eps-pe.template.json)，stock-input --template默认同结构。EPS三字段同旧格式，增加pe_low、pe_median、pe_high和可选industry_pe，使用十进制字符串/null。行业项缺失或null不作为待补条件，不阻塞历史PE估值；PE三档需要正数且递增。EPS非正时倍数法不适用，缺失不补0、不伪造正估值。

- 保守＝eps_conservative×pe_low。
- 基准＝eps_base×pe_median。
- 乐观＝eps_optimistic×pe_high。
- 行业比较＝三档EPS分别×industry_pe，另表显示，不与历史PE相加/平均，不作为第二独立估值方法。

raw七字段封装manual-eps-pe-v4，证券从CLI取得，as_of为录入日，EPS期间沿用固定NTM约定；读回不移动期间。metadata来自config/manual-input-policy.json（MANUAL_INPUT_POLICY_FILE），包含显式USD币种约定、ntm期间、未知盈利口径、未知PE分母和quartiles统计约定。未知口径仅允许显示手工倍数研究假设，不声称来源已经核实；完整来源/统计窗口等仍可用旧v2格式录入。若需要修改统计定义，可以配置min_median_max，但应同步提问并明确不是分位数。修改配置不重写已保存档案，须新录入形成新版本。

公共manual_multiples.evaluate复用旧EPS证券/日期/数值校验，手工PE单独检查，计算以Decimal精度48执行。preview/save/read/哈希/锁/确认与追加版本沿用，不把GPT数据中的确认字段当授权。EPS或PE完整项目可单独确认并明确另一项待补，未齐不生成该组价格。新草稿不覆盖旧确认；过期或口径冲突需复核，不回退旧确认。

## 停用自动PE及旧格式

service.refresh_sources和非cached的levels只获取行情并读取手工估值；automatic_pe.refresh_history/refresh_industry公共方法直接返回停用，force不恢复。automatic-pe-policy.json的enabled改false，但仅改回true不能恢复公共入口。旧私有适配及成功/失败档案不删除，不被当前研究补齐使用。普通查询/通知/手工预览/回测均不联网取PE。

旧v1/v2手工PE及DCF照原规则保留。旧v3 EPS-only可读取/存草稿或确认EPS项目，但展示手工PE待补，移除“运行研究即可自动补PE”的旧提示，不读取自动缓存重建估值。用户需要以新七字段导入新版本，不能将旧EPS日期静默移动到新记录中。

新v4默认只算假设。只有本人依据可靠资料明确系统EPS盈利口径和PE的NTM定义，再以该约定新录入，才能让历史PE组参与已有基本面/技术比较。行业参照只作独立比较，原技术角色、强弱、策略、退出和共振门槛保持。默认未说明来源、行业分类、日期和盈利定义的七数字不能被程序视为已验证事实。

## 用户命令及待核对清单

```bash
.venv/bin/stock-input GOOG --file data/GOOG-input.json
.venv/bin/stock-input GOOG --file data/GOOG-input.json --confirm
.venv/bin/stock-levels GOOG --cached
```

预览即可计算，确认后研究/levels/通知读取同版本；stock-research普通运行必要时仅取行情。没有行情或未知证券类型时仍不宣称研究完整。通知仍不因导入而发送。

| 编号 | 用户待核对 |
| --- | --- |
| MP01 | EPS三档×PE三档逐项相符，行业比较单独计算、不合并PE |
| MP02 | industry_pe=null或省略不阻塞历史估值；缺历史PE时行业组仍可单独计算 |
| MP03 | 错序、非正PE、NaN、数字类型而非字符串、多余字段拒绝；null不补造；非正EPS不适用 |
| MP04 | 草稿/确认/哈希/同内容幂等/新日期新版本/固定期间/到期复核继续成立 |
| MP05 | input/research/levels/通知共享结果；查询不取PE，不调用财报、身份或模型 |
| MP06 | 旧v1/v2照旧，v3只提示手工PE待补；旧自动PE不参与当前结果，force及旧enabled不恢复 |
| MP07 | 默认未知定义仅假设，不虚构统计日期/来源；明确匹配约定后才进入技术比较，行业不伪装第二方法 |

本轮仅代码阅读和逻辑推演，未新增/运行测试文件、临时断言脚本、应用/CLI/网络抓取/模型/投递，未改用户EPS文件或真实研究记录，网页仍后续。

# 精简预测EPS与历史PE估值

2026-11-05默认流程已进一步改为[三值EPS＋程序PE](34-eps-only-automatic-pe.md)，模型不再需要返回历史PE；本专题v2保留兼容，--template现输出三EPS字段。

2026-11-03用户确认提供可直接复制且包含JSON的提问模板，程序读取预测EPS与历史PE范围并计算。该精简路径覆盖此前完整三情景、DCF和折算输入的默认模板；旧v1档案及计算兼容，不删除或改写a.json，不为旧文件补造预测数据。

## 输入与计算

提问全文：[gpt-eps-pe-prompt.md](templates/gpt-eps-pe-prompt.md)，已包含返回JSON，不需另外附模板。默认空结构：[manual-valuation-v2.template.json](templates/manual-valuation-v2.template.json)。`stock-input --template`输出v2，未知证券口径不猜测。

公共manual_valuation.evaluate/save/read按schema_version分流，v2由compact_valuation确定性计算。所有入口共用证券/币种/日期/来源校验、预览/草稿/本人确认、哈希、证券锁及追加版本。stock-research --valuation也支持v2。来源不下载，财务数据不自动抓取，普通查询不调用模型。

| 字段 | 语义 |
| --- | --- |
| eps.period_type | fy1为明确财年起止；ntm严格为查询次日至一年后，不混同 |
| eps.basis / historical_pe.earnings_basis | gaap_diluted、adjusted_diluted、normalized_diluted，两处必须相同 |
| eps.low/mean/high | 同一期间的预测低值/平均/高值；mean必填，允许仅mean |
| historical_pe.eps_reference | fy1、ntm、ttm，保存实际分母定义 |
| historical_pe.statistic | quartiles是25/50/75分位，min_median_max是最小/中位/最大，均非历史均值 |
| historical_pe.low/median/high | 完整正数且递增；统计起止日由输入指定，不硬编码5年 |
| historical_pe.note | 简述采样频率、样本/异常处理和来源限制 |
| sources | id/title/HTTPS url/updated_at，更新时间未知可null，不验证网页真实内容 |
| notes | 简短限制说明，不作为程序指令或已确认状态 |

保守=EPS低值×PE低值，基准=EPS平均值×PE中位值，乐观=EPS高值×PE高值，全部Decimal。只取得mean时low/high必须同时null，三个情景共用mean，显示fallback，估值范围仅由PE变化产生；一边缺失不默补。EPS存在非正情景则倍数估值不适用。缺mean/PE/期间/来源可存草稿；无完整可用项目拒绝确认。v2不要求实际TTM、观察PE、财报期末、DCF、折算率，不制造这些缺项。

输出称**前瞻估值参考**，不是一年后终点股价、未来最高最低价、概率区间或交易许可。历史倍数用于研究假设，不保证回归。财年不冒充NTM。预测期结束要求复核；读取不移动旧期间，不冒充新滚动预测。人工复核间隔仍由MANUAL_VALUATION_REVIEW_DAYS控制，默认不新增固定过期天数。

## 技术结合与兼容

K线区间、角色和强弱不变。v2同盈利口径且historical_pe.eps_reference==eps.period_type时，用Forward PE参照比较当前技术报价区间，标注固定预测期间及资料日，不套v1未来终点股价折现过程。仅一个EPS模型，不制造DCF缺口，不把三情景当独立模型。共振门槛沿用配置：默认两模型，单EPS可显示配合/交集，但不标双模型共振。

历史TTM PE或不匹配的Forward PE仍计算倍数假设并警告，不参与同口径基本面确认。GAAP/调整后盈利口径冲突拒绝导入。基金、币种冲突规则保持。模板精简不代表有权编造历史统计。

CLI录入、研究表格、levels、通知共用新结果；录入不触发推送。本轮未发消息。v1仍按原DCF/目标时点/technical_alignment规则读取。新草稿不覆盖旧确认；确认新v2后不回退旧自动研究。网页后续开发。

## 用户操作

把模型返回对象另存为data/GOOG-input-v2.json，保留a.json供对比：

```bash
# 预览并计算，不保存、不联网
.venv/bin/stock-input GOOG --file data/GOOG-input-v2.json
# 本人核对后确认保存
.venv/bin/stock-input GOOG --file data/GOOG-input-v2.json --confirm
# 复用K线，独立比对支撑压力；行情旧时提示更新
.venv/bin/stock-levels GOOG --cached
# 完整研究表，缓存优先，必要时仅更新行情
.venv/bin/stock-research GOOG
```

预览不等于保存；只读levels不会直接读文件。缺有效行情时估值可单独看，结合层待更新。退出码2仍为部分资料不足等，非统一崩溃。

## 用户验收清单（未执行）

| 编号 | 用户核对项 |
| --- | --- |
| CP01 | 完整EPS与PE三值，逐项乘积及区间正确 |
| CP02 | 仅mean时共用EPS且明确fallback，不发DCF/TTM缺失警告 |
| CP03 | 单边范围缺失不默补；错序、NaN、负PE、类型错误拒绝；非正EPS不适用 |
| CP04 | 股票/币种/基金/未来资料日/期间/来源冲突明确处理，不联网核实链接 |
| CP05 | 盈利口径冲突拒绝；TTM跨期只算假设、不同口径不发布综合确认 |
| CP06 | 草稿/确认/最新版本/哈希/幂等正确；v1继续原规则，不强制迁移 |
| CP07 | input/research/levels/通知结果一致；单EPS不冒充双模型，不自动发送 |
| CP08 | 预测期结束/复核间隔/证券冲突时要求复核，清除当前可用价格，不回退旧版本 |

仅代码阅读及逻辑推演，未新增测试文件/脚本，未运行测试、应用/CLI、抓取、模型或群投递；上述由用户执行。

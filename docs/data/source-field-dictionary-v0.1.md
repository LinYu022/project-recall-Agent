# 来源字段字典与时间映射 v0.1

对应 `source-record/0.1`；适用范围是 `examples/nhtsa-sources.json` 的三个探索来源，不是所有 NHTSA 接口或批量文件的通用规范。字段说明与本轮样本指纹见 `field-mapping-references-2026-10-26.json`。

## 证据与格式边界

已取得 [投诉批量字典 CMPL.txt](https://static.nhtsa.gov/odi/ffdd/cmpl/CMPL.txt) 和 [召回批量字典 RCL.txt](https://static.nhtsa.gov/odi/ffdd/rcl/RCL.txt)。二者都说明批量文件采用 YYYYMMDD，不能据此解析 API 的斜杠日期。

CMPL 区分事件日期、加入文件日期、机构收到投诉日期，并说明 ODI 编号在多部件行中可能重复；RCL 区分报告收到日期和记录创建日期，另有批量记录 ID。当前 API 没有提供全部这些字段。不能把 API 的数组位置当作批量记录 ID，也不能把提交日期或创建日期改名为公开日期。

官方字典的原文件留在 `.local/field-dictionaries/`，公开文档只保存 URL、大小、哈希及解释。映射策略来自官方语义和已归档 API 样本的分别核对；本项目定义的复合键、版本哈希并非官方编号。

## API 与文件字段

| 来源 / 原字段 | 本轮用途 | 缺失或异常处理 |
| --- | --- | --- |
| 召回 `NHTSACampaignNumber` | 召回编号，必须等于来源选择 | 缺失/格式不符/范围不符则拒绝该行 |
| 召回 `Make/Model/ModelYear/Component` | 与召回编号共同构成原值复合行键 | 非空字符串；不做别名归并 |
| 召回 `Summary` | 必需原始摘要 | 非空字符串，否则拒绝 |
| 召回 `ReportReceivedDate` | `report_received_date.raw` | 本轮格式未核实，value=null，status=format_unverified |
| 召回 `PotentialNumberofUnitsAffected` 等 | 原样保存在 payload | 本步不用于统计分母或业务推断 |
| 投诉 `odiNumber` | 正整数，转换为字符串作记录键 | 缺失、布尔、非整数、非正数拒绝；当前适配器不接受字符串编号 |
| 投诉 `summary` | 必需的投诉自述原文 | 非空字符串，否则拒绝；不视为已证实事实 |
| 投诉 `products` | 原数组保留；至少一项与来源品牌/车型/车型年精确匹配 | 缺失或范围不符拒绝该行；不拆部件或推断个车适用性 |
| 投诉 `dateOfIncident` | `event_date` | 按本样本观察格式 MM/DD/YYYY 严格解析；无值为 missing，非法日期为 invalid |
| 投诉 `dateComplaintFiled` | `filed_date` | 同上；不映射成已验证的机构收到时间 |
| 投诉 VIN、部件、伤亡及未知新增字段 | 保存在本地数据库 payload 和原快照 | 不出现在公开报告；本步不解释或修正 |
| 调查 PDF body | 文件哈希/长度版本与 URL 身份 | 仅登记文件元数据；text_parse_status=not_started |

投诉样本的 103 行中，事件日期第二段大于 12 的有 59 项，提交日期有 64 项，第一段大于 12 的均为 0。这支持该接口样本按 MM/DD/YYYY 解释，映射 basis 明示为 `observed_api_MM/DD/YYYY_v0.1`，不是官方 API 格式保证。日期只保留到日，不凭空添加时区或午夜时间。

## 时间映射表

| 概念 | 保存方式 | 当前可用性 |
| --- | --- | --- |
| 事件发生日 | normalized.dates.event_date | 投诉 API 有；其他缺失 |
| 投诉提交日 | normalized.dates.filed_date | 投诉 API 有；与机构收到日分开 |
| 召回报告收到日 | normalized.dates.report_received_date | 原值有，解析待核验 |
| 内容版本公开时间 | normalized.version_public_at | 全部 null，无历史公开证明 |
| 本地抓取完成时间 | source_snapshots.fetched_at | 来源于已保存获取清单 |
| 本地版本首次观察时间 | 所有关联快照 fetched_at 的最小值 | 查询时计算；不是数据库导入时间 |
| 入库先后 | 不用于事件排序或历史可见性 | 较旧清单可以晚导入 |

每个日期对象保存 `raw/value/status/basis`。非法可选日期不丢掉整条投诉，而是保留记录和质量标记；缺记录身份、内容或范围会隔离该行。使用日期过滤时必须检查状态，不能把 null 当作满足区间。

## 内容版本与血缘

原始响应哈希基于完整 body 原字节。`content_sha256` 则基于 JSON 行按键排序后的 UTF-8 紧凑编码，区分未知字段或内容变化，但不因 JSON 对象键顺序改变而变化；数组顺序仍保留。它不能冒充原文件字节哈希。

`version_id = hash(来源类型, record_key, content_sha256, mapping_version)`。召回没有稳定 API 行编号，复合键成员变化会产生新键，本步不推断两者是同一实体修订；名称归并在 P1-03。投诉接口当前以一条 ODI 聚合记录为版本单位，未来接入一诉多部件的批量格式须另定映射。

每个版本通过 snapshot_rows 关联回 snapshot_id 和 `/results/序号`，文档使用 `$document`。相同内容在多份快照出现可共享版本，但快照与行位置各自保留。原始字节仍是完整追溯依据，JSONB 不是 PDF 备份。

变更映射策略须新建 mapping_version，不覆盖旧派生记录；batch_sha256 检查相同版本名下派生内容的意外变化。v0.1 不自动选择“最新有效版本”、不把旧版从新响应消失解释为删除。历史筛选现由 ingestion/temporal.py 执行：只用截止前所选观察证据，冲突版本明确排除，规则见第三步学习文档。

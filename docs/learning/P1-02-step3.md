# P1-02 第三小步：历史过滤、迟到记录与资料验收

日期：2026-10-26。状态：本小步和 P1-02 实现及本地验收完成；等待学习者确认理解后进入 P1-03。真实资料检视、受控机制测试与模型实验分开记录。

## 目标、依赖与架构作用

依赖前两步的快照原字节、获取清单、记录版本、日期映射和位置血缘。本步将时间模式变成可执行的筛选规则，补齐迟到/修订验证、下载策略、质量报告与可用性矩阵。

固定快照分析回答“用这些指定内容看某个事件区间”；历史模式则要求“只使用知识截止前可证明已存在的具体版本”。事件日、提交日和本地观察时间分别承担不同职责，不能替换。

本步新增 `ingestion/temporal.py`。输入是明确的 snapshot_ids 和复用的 TimeScope；先核验原始哈希与映射，再筛选版本，输出选择清单、排除原因、原始位置、输入集合和结果指纹。CLI 不输出投诉摘要/VIN，完整选择可保存在本地新文件。

## 阶段接口问题与修订建议

原计划第 3.3 节要求 snapshot_analysis 固定 release_id，但 DatasetRelease 构建与完整性检查排在 P1-06。P1-02 时直接声称已经绑定正式发布版本，会混淆阶段产物。

采用的分阶段方案：本模块提供明确标 `purpose=raw_snapshot_inspection`、`dataset_release=null` 的检视入口，以快照 ID、body 哈希、映射版本和批次指纹固定输入；P1-06 将完整发布清单解析为这组固定输入，核对完整性后复用本步时间规则。正式调查 TaskSpec 的 release_id 要求保持不变。未修改原计划文件，未删减发布一致性目标。

## A：具体版本的观察证据先于事件区间

当前证据类型只有从公开官方源取得原字节的本地获取清单，不能从 payload 的发布日期、接收日期、HTTP Last-Modified 推断更早公开时间。

```python
visible = snapshot.fetched_at <= cutoff
```

这意味着该版本不晚于本地观察时已存在，不意味着精确发布日期已知。`historical_as_of` 只使用所选清单中满足条件的引用；新清单不隐式进入先前固定的输入集合。来自后来响应的事件日也不能先用于过滤、再绕过可见性。

规则：

| 情形 | 行为 |
| --- | --- |
| 事件很早，但直到截止后才观察到该版本 | 排除，version_not_observed_by_cutoff |
| 新摘要在截止后出现，旧版有截止前证据 | 旧版可用，新版排除 |
| 同一记录两个不同版本均在截止前可见 | 两版都标冲突并排除，不猜谁替代谁 |
| 成功空响应也在截止后取得 | unsupported，不冒充历史零记录 |
| 需要事件区间，但 event_date 未知/非法 | 排除，不能改用 filed_date |
| event_date 确认在区间外 | 普通过滤；可返回有范围限定的 empty |

时间区间包含两端；知识截止必须带时区。快照分析对每个来源选择只接受一个不同 body，防止把两个更新批次混称单一快照；重复获取的相同 body 可以共存。不同来源造成同一记录多版本时会标记冲突，而不会静默归并。

当前历史覆盖只来自所选本地观察。独立早期归档的验证适配器尚未建立；不会通过一个任意 supplied_publication_date 字段放行历史内容。没有早期归档也必须完成过滤/拒绝机制，不能假装已支持任意历史日期。

## B：状态要表达信息不足

| 状态 | 含义 | CLI 退出码 |
| --- | --- | ---: |
| selected | 存在合格版本，没有未解决的覆盖/日期问题 | 0 |
| empty | 已具备对应快照证据，但合法筛选范围内无结果 | 0 |
| partial | 有合格版本，同时存在不可核实版本、日期问题、拒绝行或冲突 | 4 |
| unsupported | 无法在要求下选出可靠版本 | 5 |
| 操作失败 | 输入错误、文件缺失、损坏等 | 2 |

普通区间外过滤不使结果成为 partial；未知时间或版本冲突会。输出明确区分 `fixed_snapshot_analysis` 与 `bounded_observed_versions_as_of`，不由程序自行把当前数据升级成历史还原。selected 也不等于证据充分、总体完整或唯一官方有效修订。

## 范例、练习与参考答案

以下例子使用本机已保存的三个真实快照。其他机器先 capture，替换请求文件中的 snapshot_ids；Git 仓库不包含原始投诉文件。

```powershell
uv run --locked --offline python -m ingestion select examples/time-snapshot.json
uv run --locked --offline python -m ingestion select examples/time-historical-2014.json
uv run --locked --offline python -m ingestion select examples/time-historical-observed.json
```

本轮参考答案依次为：selected/106 个版本、unsupported/0 个版本、selected/106 个版本。第二条实际排除 106 个版本，原因均为截止前无观察证据；第三条截止为 2026-10-26 08:00 UTC，三个响应当时都已实际保存，仍只限这些观察。

用 `--output .local/my-time-selection.json` 保存可复核结果。该路径必须尚不存在，避免覆盖既有选择；不加该选项只打印摘要。完整结果包含版本引用、排除条目、输入哈希与选择哈希，不包含投诉正文。

练习及参考答案直接给出：

| 受控场景 | 参考答案 |
| --- | --- |
| 2010 年事件、2020 年 4 月才提交/观察，知识截止为 2020 年 3 月 | 历史模式排除；当前固定快照可把它纳入 2010 年事件区间 |
| 同 ODI 的新版在截止后出现 | 保留旧版选择；新内容不回写进旧回答 |
| 两个版本均有截止前证据，没有替代关系说明 | 报冲突，不自动选最后入库版本 |
| filed_date 已知、event_date 缺失，要求事件统计 | 拒绝该条事件区间匹配，不能把提交日当事件日 |
| 后来导入新快照 | 旧选择的内容及指纹保持不变 |

```powershell
uv run --locked --offline python -m pytest -q tests/test_temporal.py --basetemp .local/practice-time
```

13 项无数据库机制测试使用明确标识的合成行和人为观察日期，不是 NHTSA 历史资料。另有 2 项真实 PostgreSQL 测试，将合成版本按不同顺序入库，核对时间筛选 ID 与持久化版本、迟到记录和旧快照绑定。

## C：资料可用性与质量报告

`scripts/verify_data_quality.py` 用锁定的开发依赖 pypdf 6.21.2 读取调查样本。该步骤是可用性探测，不改变 source-record/0.1 的文件元数据映射，也不提前交付 P1-04 的分块/表格解析器。

实际确认 PDF 为 1 页，可提取 2160 字符，定位到 Action 与 Summary。用 Poppler 渲染完整页面后目视检查，正文、表格和标题可读；有 Symbol / ArialUnicode 字体提示，未宣称字体像素一致。首次自动检查失败是因为内部来源 ID PE10006 与文件显示 PE 10-006 不同，改为检查文件实际显示形式后通过，没有改动原文件。

人工复核确认该真实调查文书提供超出召回车型 JSON 的调查背景、结案与继续监测说明；具体位置与范围写入矩阵。原始 PDF 的标注日期仍不是具体版本早期公开的证明。本轮未缺少调查资料，因此不需要用生成文本弥补。

产物：

- `docs/data/acquisition-strategy-v0.1.md`：来源范围、传输上限、失败、更新、范围扩展与正式批量数据边界。
- `docs/data/availability-matrix-P1-02.md`：下载、解析、定位及额外信息逐来源实测。
- `docs/data/quality-report-P1-02.md` 及 JSON：数量、日期状态、异常与历史选择结果。

```powershell
uv run --locked --offline python -m scripts.verify_data_quality --output .local/data-quality-replay.json
```

实际质量结果：4 份获取清单、3 个原始对象，108 个接受单元、0 拒绝，106 个不同版本。投诉的 103 个事件日和提交日均可按当前策略解析，未发现事件日晚于提交日；召回的 2 个报告日期仍保留格式待核验。精确公开时间已知为 0，总体完整性未建立。这些都不是模型准确率。

## 本地验证与模块验收

94 项无数据库测试、24 项真实 PostgreSQL 测试全部通过，合计 118 项 pytest 检查，0 失败/错误/跳过。另有 7 类真实回环 HTTP 检查，Ruff 和当前 17 个源码文件 mypy 通过。数据库再导入四份真实已下载快照两轮后仍为 106 版本、108 位置关联，schema 已清理、临时数据库及 HTTP 服务已停止。

真实快照时间选择已回放一致；人工时间夹具与真实观察分开。GitHub 工作流未在远端运行；没有真实模型调用、token/费用或调查质量实验。新增开发依赖 pypdf 已写入锁文件。

验收记录：

- `docs/verification/2026-10-26-temporal-offline.json`
- `docs/verification/2026-10-26-temporal-postgres.json`
- `docs/verification/2026-10-26-temporal-real-snapshots.json`
- `docs/verification/2026-10-26-temporal-http.json`
- `docs/verification/2026-10-26-pdf-availability.json`

P1-02 原始快照、字段字典、时间映射、质量报告、可用性矩阵、下载策略、幂等导入、版本保留、时间模式和迟到/修订/未知可见性验证均已有产物。完整目标保留；正式发布一致性、实体归并和引用解析继续由各后续模块完成。

对应实现提交：`e904dad`。模块验收汇总：`docs/verification/2026-10-26-P1-02-acceptance.json`。

本模块到此停止。等待学习者确认理解，再进入 P1-03：实体标准化、结构化查询与关联类型。

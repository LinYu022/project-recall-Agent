# P1-02 第二小步：记录版本、时间映射与幂等导入

日期：2026-10-26。状态：本小步完成，P1-02 仍进行中。没有真实模型调用；真实数据库实验与合成失败场景分别记录。

## 目标、前置依赖与整体位置

依赖第一步的原始对象和获取清单，以及 P1-01 的数据库迁移和事务。数据流为：

`本地原始快照 → 哈希/容器复核 → 逐行映射与质量标记 → 一个数据库事务 → 内容版本和原始位置关联`

本步为 P1-03 的结构化查询准备可追溯数据，但不实现实体标准化或统计工具。全部资料沿用已看过的探索来源，仍是 exploration_only。调查 demo 的模拟配置未改为真实数据。

## A：先定义身份，再谈去重

同一 URL、同一次获取、同一条投诉、同一内容版本是不同身份。`ingestion/records.py` 规定：投诉以正整数 odiNumber 作键；召回以编号、品牌、车型、车型年、部件原值组合成键；PDF 以固定 URL 作文档键。

```python
version_id = content_hash([snapshot.source.kind, record_key, digest, MAPPING_VERSION])
```

digest 是完整 JSON 行的规范编码哈希，原样保存未知新增字段；它与响应 body 的原字节哈希不同。相同记录内容产生相同版本 ID，摘要改变会产生新版本。映射规则的语义版本是 source-record/0.1，改变日期策略时也必须更新它。

本步不擅自合并名称差异：复合键中的车型名改变会创建新键，是否同一实体留待 P1-03。重复原始行各自保留位置，报告同时展示输入行数、不同记录键数和不同版本数，不能简单把接受行数当作唯一投诉量。

## B：时间依据要比日期字符串更完整

已下载两份官方批量字段字典，指纹已保存；其 YYYYMMDD 规则与当前 API 的斜杠日期不同，不能直接复用。完整对照见 `docs/data/source-field-dictionary-v0.1.md`。

投诉 API 样本支持 MM/DD/YYYY，本轮以明确的 observed_api 依据解析为日期，不补时区。`dateComplaintFiled` 保存为 filed_date，不宣称等于机构收到日。召回 ReportReceivedDate="09/02/2010" 保留原值并标 format_unverified，未猜成 2 月 9 日或 9 月 2 日。所有内容版本公开时间仍为 null。

日期字段结构包含 raw/value/status/basis。缺失、非法、格式未核验是三种状态；非法可选日期保留记录和质量标记。身份或必需内容缺失时，隔离该行并记录机器错误码和原始 JSON Pointer。公开报告不包含投诉原文或 VIN。

`first_observed_at` 根据版本关联的所有快照获取时间取最小值；不使用入库时间。测试先导入较新的清单，再导入受控的较旧清单，证明首次观察随证据更新，已存在内容版本无须覆盖。受控修改只发生在测试临时文件中，不伪造真实历史归档。

## C：数据库迁移与共同提交

新增 `0002_source_records`，不改写历史 0001。新结构为：

| 表 | 作用 |
| --- | --- |
| source_snapshots | 已成功获取的快照、原始哈希、获取时间和原清单 |
| record_versions | 不覆盖的记录/文件内容版本及带版本的映射结果 |
| snapshot_imports | 一份快照在一个映射版本下的导入摘要；组合主键防重复 |
| snapshot_rows | 每个输入位置关联到内容版本，或保存拒绝错误码 |

数据库检查输入数=接受数+拒绝数，外键保持血缘，单行必须“版本/错误”二选一。SQL 不能单凭 CHECK 对账另一张表的总行数，所以导入器和实际回读还会逐行核对。迁移测试覆盖当前结构对账、升降级及旧控制表数据保留。

`ingestion/importer.py` 在 `engine.begin()` 中保存整批结果。以 `INSERT ... ON CONFLICT DO NOTHING ... RETURNING` 取得导入权，重复请求返回 already_imported；内容/映射与已有身份冲突则明确报错。这里的数据库唯一键使两个并发导入同一快照只能提交一次，避免仅靠“先查再写”的竞态。

```python
claim = insert(snapshot_imports).values(...)
claim = claim.on_conflict_do_nothing(index_elements=["snapshot_id", "mapping_version"]).returning(
    snapshot_imports.c.snapshot_id
)
```

重复导入仍先核验本地原字节，不因数据库已有记录而绕过完整性检查。逐行预期校验失败可以与有效行共同提交，形成可复核的部分接受结果；数据库意外失败会回滚整个导入事务，两者不要混淆。

## 范例、练习与参考答案

先用已有快照预检查，不需要数据库或网络：

```powershell
uv run --locked --offline python -m ingestion inspect d0be027566e94ad2aa0737d166df64b1
```

本机本轮参考输出：input_units=103、accepted=103、rejected=0、distinct_versions=103。其他机器先按第一步 capture 取得自己的 snapshot_id；仓库不包含原始投诉数据，旧 ID 不会自动可用。

准备已启动的开发数据库，按 P1-01 文档设置 RECALL_DATABASE_URL 后：

```powershell
uv run --locked python -m alembic upgrade head
uv run --locked python -m ingestion import d0be027566e94ad2aa0737d166df64b1
uv run --locked python -m ingestion import d0be027566e94ad2aa0737d166df64b1
```

参考答案：首次 imported/new_versions=103；在空库上重复同一快照应为 already_imported/new_versions=0。同内容先前已从另一快照导入时，首次导入这份清单也可能 new_versions=0，但保留新的观察关联。`accepted` 是这份输入的接受数量，不是本次新增数量。

练习直接给参考场景：

| 场景 | 预期与可运行测试 |
| --- | --- |
| 投诉摘要变更、ODI 不变 | 保存两个版本，旧快照仍指旧版；changed_content 用例 |
| 一行缺 ODI 编号 | 隔离该行、保存位置，接受+拒绝对账；invalid_rows 用例 |
| 日期 02/29/2021 | 原值保留、value=null/status=invalid；记录不因此消失 |
| 两个线程导入同一快照 | 一个 imported、一个 already_imported；concurrent_reimport 用例 |
| 第二条数据库写入违反约束 | 整批无残留；late_database_failure 用例 |
| 相同映射名下悄悄改变解析逻辑 | 批次指纹不符而拒绝；unchanged_mapping_version 用例 |

```powershell
uv run --locked --offline python -m pytest -q tests/test_record_mapping.py --basetemp .local/practice-record-map
```

数据库场景需要专用测试数据库；本机也可用已解压的便携 PostgreSQL，一次验证并自动关闭：

```powershell
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --source-index docs/data/raw-snapshot-observations-2026-10-26.json
```

`--source-index` 是可选的真实已下载资料验证；不传时只跑可公开复现的合成数据库测试。指定该选项需本机保有第一步的清单和原字节。脚本创建全新实例，验证程序只在自己生成的 schema 中导入和清理，不改 public。临时实例停止后不会持续提供查询；长期保存请使用自己的开发数据库。

CLI 的 inspect/import 没有拒绝行时退出 0，有拒绝行时退出 4；后者仍已保存有效行和失败索引。操作、配置或数据库失败退出 2。可选日期警告不会使 CLI 返回 4，须读取 date_issues。捕获阶段的退出码约定继续保留。

## 本轮真实结果

真实 PostgreSQL 18.6 中，经在线迁移和真实 CLI，将四份既有官方快照各导入两轮：

| 指标 | 最终数量 |
| --- | ---: |
| 快照清单 | 4 |
| 内容版本 | 106：103 投诉、2 召回车型记录、1 PDF 文件元数据 |
| 导入摘要 | 4 |
| 快照位置关联 | 108：两个召回快照各 2 行，投诉 103 行，PDF 1 文件 |
| 第二轮新增版本 | 0 |

回读 payload 与原映射一致，原始位置关联一致；未知公开时间仍为 null。两份召回快照均提示 2 项格式未核验，总计涉及 2 个不同召回记录版本。PDF 不计作文本已解析。测试 schema 已清理、临时 PostgreSQL 已关闭；本轮未重新获取业务 API。

无数据库组 81 项通过；真实数据库组 22 项通过，合计 103 项 pytest 检查。数据库故障/修订/乱序/并发场景使用合成资料；另有独立的真实已下载资料导入实验。Ruff 和 16 个指定源码文件 mypy 通过，7 类真实回环 HTTP 回归通过并关闭服务。GitHub 端尚未运行，真实模型调用 0 次。无新增依赖。

记录见 `docs/verification/2026-10-26-source-import-postgres.json`、`2026-10-26-source-import-offline.json`、`2026-10-26-source-import-http.json`。字段依据指纹位于 `docs/data/field-mapping-references-2026-10-26.json`。

对应实现提交：`13e2125`。原始资料与数据库目录未提交，完整原计划保持不变。

P1-02 还需完成 historical_as_of 的版本过滤/明确拒绝、迟到记录验证、资料可用性矩阵、总体数据质量报告与下载范围策略。没有真实历史归档时只声明实有覆盖，受控测试不能变为历史实验结果。下一轮仍在本模块，完成后才等待确认进入 P1-03。

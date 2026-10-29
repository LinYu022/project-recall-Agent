# P1-03 固定快照查询 API

这是独立的本地查询入口 `app.query_api:create_query_app`。现有 `app.api:create_app` 保持模拟教学行为。查询 API 不联网取数、不导入/投影、不调用模型，也不写关系工件。

## 服务端准备

服务端配置是 `examples/query-readonly.json`，也可通过 RECALL_QUERY_CONFIG 指向自己的配置文件。snapshot_root 和 allowed_snapshot_ids 由服务端读取；请求不能改变根目录、数据库、规则或输入任意 SQL。

示例快照 ID 属于本机；其他机器先 capture，另存自己的服务端配置、请求和观察索引。原内容变化时必须重做事实审查，不能改写旧观察清单。启动相对路径以项目根目录为基准。

计数需要开发 PostgreSQL：先按 P1-01 准备 RECALL_DATABASE_URL，再执行迁移、`ingestion import`、`entities project`。查询入口不会自动修复缺投影，防止读请求悄悄改变实验数据。关联 API 只读取原始快照和固定规则，无数据库也可运行；缺数据库时计数返回 503，历史不支持等无需数据库的拒绝仍可返回。

```powershell
uv run --locked uvicorn app.query_api:create_query_app --factory --host 127.0.0.1 --port 8001
```

接口文档位于 http://127.0.0.1:8001/docs 。GET `/health` 只说明服务与配置状态，database_configured 不代表连接或查询通过。这里是本地单用户开发服务；正式任务授权和产品部署分别保留到 P1-08/P1-17。快照允许列表是服务器数据选择范围，不冒充完整授权服务。

## 请求与响应

| 路由 | 请求文件 | 主要响应 |
| --- | --- | --- |
| POST /queries/complaints/count | examples/count-complaints.json | distinct_complaints、matching_component_rows、未知日期数量、规则/时间/输入指纹 |
| POST /queries/entity-links | examples/query-entity-links.json | 时间校验外层对象、assessment 中的五类关系及双方证据 |
| GET /health | 无 | saved_source_snapshots、model_provider=null、配置状态 |

计数入口复用 ComplaintCountRequest 与 EXISTS + COUNT DISTINCT 查询，不使用检索 top-k、模型回答或直接连接的行数作为投诉总数。数据库事务是 READ ONLY / REPEATABLE READ；真实 PostgreSQL 集成测试现场读取事务参数确认这两项配置。

关联请求比旧 CLI 的 LinkRequest 多出必填 temporal。两端位置必须属于该输入集合；先运行 select_versions，若返回 partial/unsupported，就不生成肯定关联。两端版本必须已选中，且所引用的具体抓取清单本身在截止内；较晚重复抓取不能冒充较早引用。事件区间筛的是投诉事故日，不能拿召回接收日替代。召回缺事故日的筛选会明确不支持。

成功评估返回 query_sha256、selection_sha256、输入清单及时间口径。内部 assessment 保留原 snapshot_only 内容哈希，不改写旧工件；是否满足时间条件以外层 query 断言为准。dataset_release=null，正式发布绑定与完整性验证在 P1-06 完成。

历史模式目前只能检查输入中保存的实际观察版本，不声明任意历史日期的完整还原。通过历史检查时仍标记 bounded_observed_versions_as_of / selected_local_receipts_only。2014 或 2000 截止下，没有此前观察的当前资料不能参与。

## 业务状态与 HTTP 状态

请求可以合法完成，却不能完成用户想要的证据判断。HTTP 200 表示查询执行完成，业务状态必须另看：

| 业务状态 | 含义 |
| --- | --- |
| counted、count=0 | 所选归档响应内，按已知标签没有匹配；不证明全国无记录或无缺陷 |
| partial | 已确认部分单独计数，并列出未知日期数量；不能把已确认 0 解读为无适用记录 |
| unsupported、count=null / assessment=null | 输入无法支持该时间或来源口径；没有制造零结果或肯定关系 |
| assessed | 关联规则执行完成；必须逐条看 confirmation_status，不表示所有关系都确认 |

| HTTP | 稳定代码/含义 |
| --- | --- |
| 403 | snapshot_not_in_server_selection，快照不在服务端范围 |
| 404 | snapshot_not_found，允许的清单在本地缺失 |
| 409 | integrity_error / projection_not_ready，来源不完整或投影缺失/规则指纹不一致 |
| 422 | invalid_request / invalid_query，字段、位置、精确名称或输入版本组合不合法 |
| 500 | source_io_error，本地读取失败 |
| 503 | query_database_unavailable，数据库未配置或操作失败 |

错误响应不回显数据库错误、密码、原始投诉或输入值。模式/schema 不符和文件内容被篡改不会被包装成“0 条”。使用 POST 是因为查询对象结构较复杂；这些路由仍是只读操作。若需保存响应，可由调用方另存，不在查询中隐式发布或归档。

## 验证入口

```powershell
uv run --locked --offline python -m scripts.verify_offline
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --source-index docs/data/raw-snapshot-observations-2026-10-26.json --entity-checks --query-checks
```

第一条用合成资料跑接口边界与真实回环 HTTP，不需官方原文件或数据库。第二条要求本机保有来源清单对应原文件；启动临时数据库、运行 SQL/API 测试、幂等导入真实旧快照、对账并启动真实查询 HTTP，最后清理 schema 和停止服务。

参考结果：归档响应 103 个不同 ODI；动力或制动并集 86，匹配部件位置 88；单制动 5；未知生产日期场景 partial/已确认 0/未知 86。五类关联的三组参考见 evals/entity-link-reference-v0.1.json。参考结论由实现代理核对，独立人工金标准仍未建立，后续正式评测前需要复核。

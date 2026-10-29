# 任务契约 v0.2

当前代码入口：`domain/tasks.py` 的 `TaskSpec`；结构导出：`evals/schemas/task-spec-0.2.schema.json`。它是调查任务的输入规格，尚非持久化运行任务、发布版本或授权对象。

相对 v0.1，增加 `task_kind`，预留 `grant_id` 和 `revision_id`，并执行时间及版本的跨字段校验。已有 10 个探索任务显式迁移至 v0.2，旧版本不静默接受。

## 字段与业务条件

- 复用 v0.1 的范围、问题、来源、必需输出、禁止推断与缺信息行为。
- `task_id`、`release_id`、`grant_id`、`revision_id` 分别表示任务、数据版本、授权和计划修订。
- `task_kind` 区分记录查询、跨源调查、单投诉阅读、投诉统计。只有投诉统计强制事件区间；单投诉编号查询可不筛日期。
- `status=draft` 可暂缺数据版本；`ready` 必须提供非空 `release_id`。ready 是输入准备状态，不是执行状态或执行许可。
- `historical_as_of` 必须有带时区的知识截止；`snapshot_analysis` 不带知识截止，避免混淆两种模式。
- 事件区间使用包含起止日的日期，结束日不得早于开始日；车型年不作为事件日。
- 未声明字段拒绝；车型年拒绝布尔值和字符串。召回编号按当前美国车辆 V 类范围校验，尚未支持设备等其他类别。

## 三层校验的区别

1. JSON Schema 校验结构和可表达的字段约束。
2. Python `model_validator` 校验跨字段条件；自动导出的 JSON Schema 不包含这些自定义逻辑，仅通过 Schema 不足以提交任务。
3. 未来执行层核对发布版本存在且完整、授权有效、工具与范围被允许、历史版本可见性及证据充分性。当前 CLI 不执行这些检查。

来源 ID 约束资料引用，不能替代工具白名单或 Authorization。输入含有 grant_id，也不构成授权有效的证明。

## 可复现入口

```powershell
uv run --locked python -m app validate-task evals/exploration/tasks/EXP-001.json
uv run --locked python -m app export-task-schema evals/schemas/task-spec-0.2.schema.json
```

退出码：0 为成功，2 为契约校验失败，3 为文件读取/写入失败。错误输出不包含整份原始输入。

# P1-01 第二小步：只读开发配置、假查询与执行记录

日期：2026-10-26（Asia/Shanghai）。模块仍为 P1-01，尚未完成全部验收。本步为真实运行的离线工程测试，数据是显式构造的训练夹具；没有 NHTSA 请求、模型调用或模型质量实验。

## 目标、前置依赖与架构作用

依赖第一步的 `TaskSpec` 和已锁定的 Python 环境，不增加第三方依赖。让一项请求经过允许范围检查、工具调用、输出校验和结果保存，使后续 API、模型和编排器都有统一的执行入口。

“只读”指业务工具只读取夹具；本地仍需写入审计工件和日志。完整输入输出称为工件，事件只记录摘要和指向工件的引用。一次业务任务可产生多个运行，每个运行中的每次调用有单独编号；不能只用 task_id 区分所有动作。

## 小步 A：显式配置与可替换的工具

`examples/dev-readonly.toml` 指定模拟数据模式、数据版本、开发 grant_id、revision_id、允许工具、允许来源和每次运行调用上限。`runtime/config.py` 使用标准库 TOML 读取及 Pydantic 校验，工具和数据模式受限，不能通过配置启动真实查询。

支持 shell 环境变量 `RECALL_ARTIFACT_DIR` 覆盖结果目录，便于测试隔离；`.env.example` 只提供说明，尚不自动读取 `.env`。本步无密钥要求。

`tools/fake_query.py` 的 `QueryTool` 是 Python Protocol：约定一个 `query(task)` 接口，调用方不必依赖具体工具类。`FakeQueryTool` 根据固定夹具返回结果；测试也可注入一个保证不得被调用的替身，验证拒绝确实发生在执行之前。

示例只使用 EXAMPLE / LEARNING-CAR 和 SIM 来源。`00V000000` 仅为满足当前编号格式的模拟占位，不能作为真实 NHTSA 召回引用。另建示例任务，避免污染 P1-00 的真实资料任务。

## 小步 B：允许范围先于工具执行

`runtime/demo.py` 先检查工具白名单、任务状态、数据/开发授权/修订绑定、车型范围、来源范围和调用上限，再调用工具。

```python
error_code = _denial(task, settings, tool_name, registry)
if error_code:
    status = "denied"
    emit("call_denied", error_code=error_code)
else:
    # 此后才调用 query；本步每次运行最多一次。
    ...
```

这段逻辑在执行层，所以直接调用 Python 入口也受到同样检查。任务中的文字或自行声明的 grant_id 不能修改可信开发配置。这里仅是受限的开发执行约束；用户身份、授权期限、撤销及恢复检查仍由 P1-08 实现，不把本步等同于完整授权服务。

## 小步 C：内容哈希与事件关联

`runtime/artifacts.py` 把规范化 JSON 的 UTF-8 字节计算为 SHA-256，使用哈希作文件名并记录字节数。同内容产生同哈希；读取时重算哈希，内容不一致即拒绝。已有路径的内容不同也不自动覆盖。

哈希用于检测内容变化，不能证明来源真实，也不能抵御同时改写文件和引用值。因此模拟结果即使哈希正确，也不能变成正式证据。

真实成功运行中的控制流：

`DEMO-001 → run_id → 输入工件 → run_started → 允许范围检查 → call_started → 假查询 → 输出工件 → 输出字段校验 → call_succeeded → run_finished → manifest`

每个事件带 run_id、task_id、call_id、release_id、grant_id、revision_id、时间及事件序号。当前 execution_generation 固定为 1，只预留字段；不宣称已经实现执行代次隔离。调用与整次运行分别记录耗时，未作为性能实验指标。

每次运行生成：

```text
.local/demo-runs/<run_id>/
  artifacts/<内容哈希>.json    输入或输出工件
  events.jsonl                一行一个执行事件
  manifest.json               最终状态与工件引用
```

异常日志只保存受控错误代码，避免复制上游异常中的敏感文本。原始请求仍存在输入工件中，日志不是原文替代品。缺少来源字段的原始输出也保存为工件，但标记执行失败，不作为有效结果发布。

## 运行与参考答案

在项目根目录执行，四条调用均不联网：

```powershell
uv run --locked python -m app run-demo examples/demo-task.json
uv run --locked python -m app run-demo examples/demo-task.json --scenario empty
uv run --locked python -m app run-demo examples/demo-task.json --scenario missing-source
uv run --locked python -m app run-demo examples/demo-task.json --scenario tool-error
```

| 场景 | 实际状态 | 记录数 | 退出码 | 意义 |
| --- | --- | --- | --- | --- |
| success | succeeded | 1 | 0 | 合格的模拟输出 |
| empty | succeeded | 0 | 0 | 调用成功但无匹配模拟记录，不代表没有缺陷 |
| missing-source | failed | null | 5 | 原始输出存在，但缺 source_id，未通过校验 |
| tool-error | failed | null | 5 | 工具抛出异常，没有返回输出 |
| 非白名单工具 | denied | null | 4 | 实际调用次数为 0 |

非白名单练习及参考答案：

```powershell
uv run --locked python -m app run-demo examples/demo-task.json --tool not_allowed
```

参考输出包含 `status=denied`、`error_code=tool_not_allowed`、`calls_used=0`；日志有 call_denied，没有 call_started。退出码 4。

回查成功运行的原文范例：

```powershell
$demoResult = uv run --locked python -m app run-demo examples/demo-task.json | ConvertFrom-Json
uv run --locked python -m app inspect-artifact $demoResult.run_dir --sha256 $demoResult.output_artifact.sha256
Get-Content (Join-Path $demoResult.run_dir 'events.jsonl')
```

参考答案：输出工件可看到 SIM 来源编号、模拟记录编号和 synthetic 标记；四个事件通过同一 run_id/call_id 关联。若仅文件内容被改写，读取将拒绝。篡改演示由隔离测试完成，不改写已保存的正常运行。

## 验证与取舍

累计 29 项离线测试通过，0 失败、0 错误、0 跳过。本步新增检查覆盖不同终态、调用前拒绝、来源字段校验、工件篡改、摘要不复制上游异常文本、CLI 退出码与事件关联。每次运行的原始配置快照也保存在输入工件中。

对应实现提交：`b27c3f8`。原始运行工件已留在本地，未上传 GitHub。

实际运行清单：`docs/verification/2026-10-26-runtime-step.json`，包含 5 个场景的 run_id、状态、退出码和工件哈希。运行工件被 Git 忽略；克隆后重新运行会得到新 run_id、时间和耗时，不要求日志逐字节相同。

取舍：使用 JSON 文件而非提前引入数据库，使本步聚焦输入/调用/输出链路；Protocol 允许替换工具而不改控制逻辑；内容哈希便于回查和发现意外覆盖。持久化调度仍按计划由 P1-11 完善，当前文件写入不具备跨文件事务，崩溃或写盘失败可能留下部分记录。

## 剩余工作

继续 P1-01：模型适配器、FastAPI 服务入口、PostgreSQL 初始迁移、离线 CI。完整权限生命周期在 P1-08，全局预算、取消恢复与 worker 在 P1-11；不把单次同步 demo 当作这些能力的完成。

# P1-01 第三小步：模型适配器与最小 API

日期：2026-10-26（Asia/Shanghai）。状态：本小步完成，P1-01 仍待数据库迁移与离线 CI。只运行模拟资料和确定性假模型，没有调用真实 LLM，也没有模型质量实验。

## 目标、依赖与架构位置

依赖前两步的 TaskSpec、开发配置、查询接口、执行器及工件存储。目标是让命令行与 HTTP 复用相同的执行规则，并把模型调用作为独立、可追踪的步骤。

新增 FastAPI、Uvicorn 运行依赖及 httpx2 测试依赖，具体版本由 `uv.lock` 锁定。当前实际安装 FastAPI 0.162.0、Uvicorn 0.54.0、Starlette 1.10.0、httpx2 2.16.0。初次使用 httpx 时，已安装的 Starlette 明确提示其测试客户端改用 httpx2；依据实际提示调整，没有把弃用告警静默屏蔽。

## 小步 A：模型接口先于具体供应商

`models/adapter.py` 定义 ModelIdentity、ModelInput、ModelOutput 和 ModelAdapter。身份保存 provider、model_id、is_simulated 及参数；输入包含任务、版本、问题和本次工具返回的原文片段；输出约定摘要与引用来源 ID。

```python
class ModelAdapter(Protocol):
    identity: ModelIdentity

    def generate(self, request: ModelInput) -> dict: ...
```

Protocol 规定调用接口，具体 SDK 由适配器负责。执行器无需知道某厂商如何组织请求。FakeModelAdapter 当前仅统计模拟记录数并生成固定说明，用于验证接口、调用边界和故障处理，不能作为调查质量基线。

`runtime/model_call.py` 给模型分配独立 call_id，保存调用输入、原始输出、身份和参数。异常或无效输出不发布摘要，仍保留可取得的原始工件及失败事件。调用耗时实际记录，token/费用 `usage=null`，没有虚构用量。

## 小步 B：先检查引用结构，再谈结论支持

```python
seen_sources = {r.source_id for r in output.records}
if not set(parsed.cited_source_ids) <= seen_sources:
    error_code = "model_citation_invalid"
```

实际实现还检查“有记录却完全不给引用”。但来源 ID 合法，只能证明引用来自本次输入，不能证明摘要语义得到支持。正式 Claim/Evidence、证据组合和反证处理仍在 P1-07；不能把这个集合检查称为事实正确性验证。

工具失败或工具输出不合格时不调用模型；模型身份不被允许或模型调用上限为 0 时，在工具执行前就拒绝整次请求。开发配置只允许 fake-summary-v1 且必须声明模拟身份，HTTP 请求不能指定新的外部供应商、服务器配置路径或 API key。

## 小步 C：薄 HTTP 路由与依赖注入

`app/api.py` 的 create_app 建立应用，Services 保存可信配置和工具/模型工厂；`Depends(get_services)` 将其交给路由。测试可替换工厂或依赖，检查未经允许的适配器确实未执行。[FastAPI 依赖说明](https://fastapi.tiangolo.com/tutorial/dependencies/)。

POST 路由只负责接收和验证请求、调用已有 run_demo、映射 HTTP 状态；不复制范围或预算检查。使用同步 `def` 处理当前同步文件与工具操作，由框架在线程池执行，不在 `async def` 中直接塞入阻塞调用。[FastAPI 同步与异步说明](https://fastapi.tiangolo.com/async/)。当前仍是一次请求等待运行完成，不是后台任务队列。

| 入口 | 用途 |
| --- | --- |
| GET /health | 显示本地服务状态、模拟资料与假模型模式 |
| POST /demo/runs | 执行一次查询，可选假模型摘要 |
| GET /demo/runs/{run_id} | 读取最终运行清单 |
| GET /demo/runs/{run_id}/events | 读取事件链路 |
| GET /demo/runs/{run_id}/artifacts/{sha256} | 校验哈希后返回原始 JSON 字节 |
| GET /docs | 自动生成的交互接口说明 |

读取工件时返回保存的字节，避免 HTTP 重新序列化后内容虽然相同、字节哈希却不同。API 的运行响应移除本机 run_dir，提供相对链接；run_id 和摘要路径参数限制格式，防止把任意文件路径当作工件位置。

## 运行、练习与参考答案

在项目根目录启动服务：

```powershell
uv sync --locked --python 3.12
uv run --locked uvicorn app.api:create_app --factory --host 127.0.0.1 --port 8000
```

在另一个 PowerShell 窗口提交示例：

```powershell
$apiTask = Get-Content examples/demo-task.json -Raw | ConvertFrom-Json
$apiBody = @{task=$apiTask} | ConvertTo-Json -Depth 12
$apiResult = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/demo/runs -ContentType 'application/json' -Body $apiBody
$apiResult
Invoke-RestMethod -Uri ('http://127.0.0.1:8000' + $apiResult.links.events)
```

参考答案：HTTP 201，status=succeeded，provider=fake，is_simulated=true；工具和模型分别调用一次，有不同 call_id，summary 引用 SIM-COMPLAINT-001。模型摘要只说明模拟记录数，不作真实车辆结论。事件共 6 条：run_started、call_started、call_succeeded、model_call_started、model_call_succeeded、run_finished。

失败练习直接给出可运行范例，使用 PowerShell 7：

```powershell
$badBody = @{task=$apiTask;model_scenario='bad-citation'} | ConvertTo-Json -Depth 12
$badResponse = Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/demo/runs -ContentType 'application/json' -Body $badBody -SkipHttpErrorCheck
$badResponse.StatusCode
$badResponse.Content
```

参考答案：HTTP 502，error_code=model_citation_invalid，summary=null。未见来源的模型原始输出仍可通过 model_output_artifact 链接核查，但不会变成合格摘要。

| 场景 | HTTP | 参考行为 |
| --- | --- | --- |
| 正常查询与假模型 | 201 | 运行完成、模拟摘要、事件与工件可回查 |
| 查询成功但无记录 | 201 | record_count=0；不得解释为不存在缺陷 |
| 缺少品牌等必填字段 | 422 | 输入校验失败，尚无运行工件 |
| 非白名单工具/不允许的模型 | 403 | 工具和模型调用次数均为 0 |
| 工具或模型抛出异常 | 502 | 区分 tool_error/model_error，不伪装成空结果 |
| 模型输出缺字段 | 502 | model_output_invalid，未发布摘要 |
| 模型引用未见来源 | 502 | model_citation_invalid，保留原始输出 |
| 工件内容被篡改 | 409 | 哈希校验失败 |
| 运行或工件不存在 | 404 | 明确返回不存在 |

CLI 的模型调用是显式选项，保留前两步命令兼容性：

```powershell
uv run --locked python -m app run-demo examples/demo-task.json --with-model
uv run --locked python -m app run-demo examples/demo-task.json --with-model --model-scenario model-error
```

API 请求默认 with_model=true；可传 false 只执行查询。HTTP 创建运行重复提交会生成不同 run_id，尚未实现幂等任务提交。

## 实际验证与故障记录

累计 43 项测试通过，0 失败、0 错误、0 跳过。其中 TestClient 检查进程内 API、依赖注入和字段处理；独立回环脚本实际启动 Uvicorn 并完成 7 类 HTTP 检查，核对下载工件字节哈希，再关闭服务。[FastAPI 测试说明](https://fastapi.tiangolo.com/tutorial/testing/)。

对应实现提交：`88996b3`。

可复现入口：

```powershell
uv run --locked python -m pytest -q --basetemp .local/pytest-step3
uv run --locked python -m scripts.verify_http
```

实际记录为 `docs/verification/2026-10-26-api-step.json` 和 `docs/verification/2026-10-26-http-smoke.json`。验证脚本只监听 127.0.0.1 的临时端口，不调用外部站点或模型，结束后自动停止服务。

遇到并实际修复/处理的问题：

- 当前 Windows 沙箱中 asyncio 初始化 socketpair 时阻塞；用 faulthandler 定位后，在正常权限下成功运行同一健康检查及完整测试。没有改写应用逻辑以掩盖环境问题，也不要求普通用户关闭系统安全设置。
- 直接以文件路径运行验证脚本时无法导入项目 app 包；统一为 `python -m scripts.verify_http` 后验证成功，文档不再提供错误入口。
- HTTP 返回 JSON 若重新编码，会改变内容哈希对应的字节；改为返回已核验的原始字节，测试明确重算响应字节哈希。

## 保留限制与下一步

本地开发 API 尚无用户登录、跨用户隔离或完整授权生命周期，仅按示例命令绑定回环地址。没有真实模型适配器、供应商 token/费用记账和真实质量实验。模型接口已留出替换边界，真实供应商接入时仍须冻结模型、参数和运行限制。

运行文件与清单尚非事务提交；崩溃时可能留下部分记录。当前只读接口读取一次小型运行日志，分页与长上下文管理仍在 P1-10，后台恢复在 P1-11。不会把开发 HTTP demo 当成完整调查产品。

下一小步仍属于 P1-01：PostgreSQL 初始迁移与离线 CI，完成后按模块验收再决定进入 P1-02。

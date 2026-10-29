# P1-03 第三小步：查询 API 与模块验收

日期：2026-10-26。P1-03 实现与本地验收完成，等待理解确认；本轮不进入 P1-04。练习直接附范例与参考答案。

## 目标与整体作用

把已经验证的实体映射、去重计数、范围和关联规则变成可调用的接口。依赖 P1-01 的 FastAPI 与 PostgreSQL、P1-02 的固定快照与时间选择、本模块前两步的规则和证据版本。后续检索/调查可以复用预定义工具，避免自己拼 SQL、计算行数或把候选直接写成因果结论。

本步没有创建调查 Agent，也没有引入模型。完整计划仍保留 P1-04～P1-18；本模块只提供其数据与查询基础。

## A：接口薄层与服务端选择

`app/query_api.py` 新建独立 factory，旧模拟 API 保留。QuerySettings 从服务端文件读取原始快照根目录和允许 ID；请求只提交固定类型的查询参数。参数校验与业务实现分开：

```python
_check_inputs(services, body.temporal.snapshot_ids)
return count_complaints(
    services.engine, services.settings.snapshot_root, body, services.count_rules
)
```

输入合法后仍可能缺投影或数据库，API 分别返回 409 和 503。不会在查询过程中补导入/补投影。数据库连接由工厂创建时由其关闭；测试注入的引擎由测试环境负责，避免接口关闭别人的连接资源。

服务端配置 examples/query-readonly.json、计数请求 examples/count-complaints.json、关联请求 examples/query-entity-links.json。API 不接受 SQL、root、rules 等额外字段。细节与状态表见 `docs/architecture/query-api-v0.1.md`。

## B：先判断时间，再判断关联

`entities/query.py` 定义 TemporalLinkRequest：固定 temporal 输入集合以及 left/right 两个位置。前置验证确保两端都属于输入，随后检查选择是否完整、两端版本是否选中、引用的清单本身是否在截止内：

```python
if selection["status"] not in {"selected", "empty"}:
    return context | {"status": "unsupported", "assessment": None, ...}
```

旧关系判断继续产生 snapshot_only assessment；新外层才断言本次时间选择。保留内层内容哈希可避免改写旧工件；外层包含输入指纹、时间口径和 query_sha256，可独立回放。

相同内容较早抓取过，不等于较晚那次清单也能作为早期引用。测试特意先取得两个版本，再重复取得相同内容，并把 cutoff 放在重复抓取前：即使版本本身已知，也拒绝用较晚定位生成早期关联。

生产日期、官方引用、症状相似等规则没有因 API 的存在变强。历史模式也仍限于实际保存的观察集合，不能宣称已经覆盖召回当年的真实历史资料。

## C：返回失败与返回零的差别

合法查询可返回 HTTP 200 + unsupported，这表示程序运行完但来源/时间不支持答案。故障则使用 403/404/409/422/500/503。partial 保留已知结果和缺口；不用 HTTP 206，避免把业务上的不完整与 HTTP 字节范围响应混淆。

真实计数在 READ ONLY + REPEATABLE READ 事务内执行；本轮数据库测试直接检查了事务参数。它统计不同 ODI，而不是产品×部件连接的行数。对同一请求重复查询得到相同指纹；查询不写数据库、源文件或关系工件。

## 动手范例与参考答案

在项目根目录的一个终端启动查询服务：

```powershell
uv run --locked uvicorn app.query_api:create_query_app --factory --host 127.0.0.1 --port 8001
```

另一个终端查询已保存的真实车门召回，不需要数据库：

```powershell
$requestBody = Get-Content -Raw examples/query-entity-links.json
$result = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/queries/entity-links -ContentType 'application/json' -Body $requestBody
$result.assessment.links | Select-Object relation_type, confirmation_status, stage
```

参考答案：scope_overlap、symptom_similar、applicability_unknown 为 confirmed，后者 stage=unknown；其他两类保持 candidate。`$result.status` 是 assessed，表示执行完成；`causation` 仍为 not_established。

修改一份内存请求，体验失败的时间口径：

```powershell
$queryExample = Get-Content -Raw examples/query-entity-links.json | ConvertFrom-Json
$queryExample.temporal.time.mode = 'historical_as_of'
$queryExample.temporal.time.knowledge_cutoff = '2014-02-15T00:00:00Z'
$historicalBody = $queryExample | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/queries/entity-links -ContentType 'application/json' -Body $historicalBody
```

参考答案：HTTP 200、status=unsupported、assessment=null；不能把 2026 年取得的当前快照直接用于 2014 年历史判断。原始请求文件未修改。

计数需要已准备的开发数据库、导入及实体投影；完整步骤沿用首步学习记录。若没有长期开发数据库，可直接用下方隔离验证，脚本会临时准备并关闭：

```powershell
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --source-index docs/data/raw-snapshot-observations-2026-10-26.json --entity-checks --query-checks
```

参考结果：38 项数据库测试通过；真实 HTTP 查询 13 类通过；schema 清理、服务停止。这个脚本使用本机已有 PostgreSQL 程序和固定原文件，其他机器需准备相应运行环境/资料，不冒称不联网就能从空目录获得数据。

| 成功/失败场景 | 参考答案 |
| --- | --- |
| 真实全部投诉 | counted，103 不同 ODI，124 部件位置 |
| POWER TRAIN 或 SERVICE BRAKES | counted，86 不同 ODI，88 部件位置 |
| 单 SERVICE BRAKES | counted，5 |
| 并集增加生产日期条件，现有日期均未知 | partial，已确认 0，另有未知 86；不能说没有适用记录 |
| 归档投诉中查询不存在的精确部件标签 | counted，0；结论仅限归档标签 |
| 缺历史观察证据 | unsupported，计数或 assessment=null |
| F150 作为精确车型查询 | 422；不能把近似候选当作 F-150 |
| 允许的快照在本地缺失 / 不在服务端允许列表 | 分别 404 / 403 |
| 投影缺失或规则指纹不符 | 409；不自动写入修复，不返回零 |
| 数据库没配置或运行失败 | 503；不暴露连接错误/凭证 |
| 同记录键出现不同内容版本 | 不输出肯定关联，返回不支持完整选择 |

不依赖真实源文件的练习验证：

```powershell
uv run --locked --offline python -m pytest -q tests/test_query_api.py --basetemp .local/practice-query-api
uv run --locked --offline python -m scripts.verify_query_http
```

参考结果：20 项合成接口测试、7 类真实回环 HTTP 通过。这里“真实”指 HTTP 服务和网络栈实际运行，资料为合成，不是模型或真实产品质量实验。

## 实测与模块产物

本轮新增 20 项无数据库接口测试、6 项真实 PostgreSQL/API 集成测试。累计 153 项无数据库、38 项真实数据库测试，合计 191 项 pytest；7 类旧模拟 HTTP、7 类新查询合成 HTTP、13 类已保存官方资料+真实数据库 HTTP 均通过。Ruff、选定 27 个源码文件 mypy、迁移 SQL 生成通过。无新增依赖或数据库迁移，真实模型调用 0，外部来源请求 0，GitHub 远端 CI 未运行。

首次新增回环验证样例用相同召回/车辆/部件键配两段不同摘要，实际造出了内容冲突，时间选择返回 partial，验证断言失败。修改合成部件身份后回归成功，并新增专门测试保留该拒绝行为。没有把失败写成源资料缺失或成功零结果。

补 OpenAPI 错误说明后，类型检查还发现响应表的字典键类型推断过窄；显式标注 `dict[int | str, dict]` 后通过。Python 字典泛型的不变性使这类“值看起来可用”的传参仍需要清晰类型，本轮没有靠关闭类型检查绕过。

记录见 `docs/verification/2026-10-26-query-offline.json`、`2026-10-26-query-postgres.json`，后者包含真实资料 HTTP 摘要。官方原字节继续留在忽略目录。参考答案由实现代理对照原始字段和摘录复核，没有用户逐项独立标注，不把它称作独立人工金标准或模型准确率。

P1-03 已形成：实体映射与不可覆盖投影、去重预定义计数、带来源的生产范围/配置、五类 EntityLink、查询 API、参考答案与回放脚本。已验证未知值不被当作落入范围，精确匹配不晋升因果，候选/排除/未知/确认可显示依据。

保留边界：当前规则目录仅覆盖审查样例；生产范围不等于某辆车制造日；多产品条件无位置绑定时拒绝；没有 VIN 适用性或因果判定；原始快照不是 DatasetRelease；正式评测前仍需独立标注复核。通用文档解析与完整发布分别在 P1-04、P1-06，未删目标。

对应实现提交：`5524c31`。代码、公开规则、参考答案和验证摘要已保存；原始资料、临时数据库、凭证与环境未提交。

本模块完成后暂停。确认理解后，下一模块 P1-04 学习文档解析、分块和稳定引用，重点防止切块丢失适用条件。

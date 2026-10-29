# P1-01 第四小步：数据库迁移、事务与离线 CI

日期：2026-10-26（Asia/Shanghai）。本小步实现及本地验证完成；P1-01 达到当前模块验收条件，等待学习者确认理解后进入 P1-02。本文所有业务记录为模拟数据，没有真实模型调用或模型质量实验。

## 目标、依赖与架构位置

前置为 TaskSpec、模拟执行器、带哈希的工件和事件日志。本步补齐计划要求的初始数据库迁移与离线 CI，让工程骨架可以重复安装、建表和检查。

执行路径仍为 `CLI / API → run_demo → 本地文件`；新增的显式归档路径为 `已完成运行目录 → 血缘与哈希检查 → 数据库事务 → 三张表`。API 不会暗中连接数据库，因此已有不依赖 PostgreSQL 的练习仍然可以运行。

新增 SQLAlchemy Core 定义表和参数化写入；Alembic 管理表结构版本；psycopg 连接真实 PostgreSQL。选择 Core 是因为当前只需要清晰的表和事务，不需要 ORM 对象关系。依赖的精确版本见 `uv.lock`；本次实测 SQLAlchemy 2.1.6、Alembic 1.20.1、psycopg 3.3.6、PostgreSQL 18.6。

## 小步 A：表结构与迁移是两种记录

`database/schema.py` 表示当前代码期望的结构；`database/migrations/versions/0001_control_tables.py` 表示一次固定的历史变更。

| 表 | 主键 / 关联 | 用途 |
| --- | --- | --- |
| task_specs | task_id 主键 | 保存不可覆盖的 TaskSpec 快照与版本 |
| execution_runs | run_id 主键；task_id 外键 | 保存一次已终止运行的状态、上下文 ID 与完整清单 |
| runtime_events | event_id 主键；run_id 外键 | 保存调用 ID、执行代次、顺序与原始事件对象 |

一个任务可以有多次运行，一次运行可以有多个事件。UUID 是运行、调用和事件的身份；sequence 是事件在一次运行内的顺序，两者用途不同。

约束分工：主键阻止身份重复；外键阻止不存在的关联；`UNIQUE(run_id, sequence)` 阻止同一运行出现重复序号；CHECK 限定正数序号/代次、终态和 JSON 对象。SQL 约束不检查序号连续性，连续性由归档加载器检查。JSONB 存完整快照，常用 ID 与状态独立成列，便于关联和约束。

release_id、grant_id、revision_id 暂时可空，因为被拒绝的草稿也需要留痕。现在没有发布/授权注册表，不能把这些字符串解释成已验证授权；完整生命周期仍在 P1-06、P1-08 和 P1-11。

迁移显式写出本版本的建表操作，不调用 `metadata.create_all()`，也不导入随未来代码改变的表定义。以后结构改变应添加新迁移，不改写已经应用的 0001。[Alembic 教程](https://alembic.sqlalchemy.org/en/latest/tutorial.html)。

```powershell
uv run --locked --offline python -m alembic upgrade head --sql
```

这只生成 PostgreSQL SQL，不连接数据库，不能证明数据库实际可执行。真实迁移另行验证。

## 小步 B：一次归档，共同提交

`database/archive.py` 先读取运行清单，核验引用工件的内容哈希、TaskSpec、任务/版本血缘、事件顺序和最终状态，再写数据库。

关键结构如下，完整代码还处理已存在任务和事件批量写入：

```python
with engine.begin() as connection:
    connection.execute(task_specs.insert().values(...))
    connection.execute(execution_runs.insert().values(...))
    connection.execute(runtime_events.insert(), event_rows)
```

正常退出上下文即提交；任意写入抛异常就回滚。`insert().values(...)` 让驱动绑定参数，数据内容不会拼接成 SQL。只有测试 schema 名称由程序生成，DDL 通过 SQLAlchemy 标识符机制构造。

同一 task_id 内容相同可复用；内容不同则拒绝。重复归档同一 run_id 会报错，并保留原记录。这是明确的“拒绝覆盖”策略，尚不是重复提交返回原结果的幂等接口。

文件存储与数据库之间仍无共同事务：归档失败时原运行文件保留，修复原因后可重新归档；数据库写入成功也不会移动或删除工件。JSONB 不是工件文件的备份。当前加载器核验本地一致性，不提供数字签名或对恶意篡改者的可信证明。

配置由 `RECALL_DATABASE_URL` 环境变量提供，限定 `postgresql+psycopg` 驱动。`.env.example` 只是说明，程序不会自动加载 `.env`；不要把真实凭证写入文档、命令输出或 Git。归档 CLI 失败只输出错误类型，数据库 SQL 参数不回显。

## 小步 C：真实数据库验证

本机原先没有 PostgreSQL 或 Docker，采用 [PostgreSQL Windows 下载页](https://www.postgresql.org/download/windows/)链接的 [EDB 二进制压缩包](https://www.enterprisedb.com/download-postgresql-binaries)。已解压服务端所需 bin、lib、share 到忽略目录 `.local/postgres/runtime/pgsql/`，未安装系统服务。

本次获取地址：`https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip`；大小 384620317 字节；SHA-256 `e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2`。该哈希是本地取得文件的指纹，没有声称取得独立的上游校验和。二进制、许可文件、下载包和数据库目录均未提交到 Git。

本机可复现：

```powershell
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin --archive .local/postgres/postgresql-18.6-5.zip
```

其他机器先从官方页面安装/解压 PostgreSQL，再将 `--bin-dir` 指向其 bin 目录；不要依赖本机 `.local/` 自动存在。脚本使用 `initdb` 创建全新目录，生成临时密码，要求 SCRAM-SHA-256，只监听 127.0.0.1 的临时端口，通过 `pg_ctl` 启停。密码初始化文件会删除，不把密码写入验证记录。结束后服务器已停止，忽略目录保留日志和已停止的数据目录；不会自动删除已有文件。

集成测试也可以连接你准备的专用测试数据库：

```powershell
$env:RECALL_TEST_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/recall_test'
uv run --locked --offline python -m pytest -q -m postgres --basetemp .local/pytest-postgres
```

上面的 USER/PASSWORD 是需替换的说明值。本轮没有保存可复用的个人数据库密码。每项集成测试生成独立随机 schema，仅在其中迁移，最后清理自己创建的 schema；不会降级或清空 public。测试用户需具备创建 schema 权限。清理后的测试数据不作为长期归档。

若使用已准备好的开发数据库长期保存归档：设置 `RECALL_DATABASE_URL`，先执行迁移，再运行示例和归档命令：

```powershell
uv run --locked python -m alembic upgrade head
uv run --locked python -m alembic current
$demoRun = uv run --locked python -m app run-demo examples/demo-task.json --with-model | ConvertFrom-Json
uv run --locked python -m database $demoRun.run_dir
```

这里需要已启动的数据库；临时验证脚本结束后其服务器已关闭。不要对有价值的数据直接尝试 `downgrade base`；本轮降级验证只发生在新建的测试 schema 中。[initdb](https://www.postgresql.org/docs/18/app-initdb.html)、[pg_ctl](https://www.postgresql.org/docs/18/app-pg-ctl.html)。

## 练习与参考答案

练习 1：给一条新事件填入不存在的 run_id，预测写入结果。参考答案：触发 `fk_event_run` 外键约束，抛出 IntegrityError。不是空结果，也不是工具执行失败。可直接运行已写好的范例：

```powershell
uv run --locked python -m pytest -q -m postgres tests/test_postgres.py -k missing-run --basetemp .local/practice-fk
```

此命令需要上文的测试数据库环境变量；它通过才表示“预期的拒绝已发生”，不是错误数据被成功写入。

练习 2：第一条运行归档成功；第二条使用新 task_id，但其最后一个 event_id 故意与第一条重复。如果第二条前面已经写了任务和运行，会留下半条记录吗？

参考答案：不会。测试 `test_late_insert_failure_rolls_back_task_run_and_events` 在真实数据库中制造主键冲突，断言最终仍只有第一条任务、第一条运行和它的 4 个事件。第二条运行的文件保留，但其数据库事务全部回滚。用 `-k late_insert_failure` 可运行该范例。

练习 3：重复执行建表迁移与重复归档，是同一件事吗？参考答案：不是。重复 `upgrade head` 读取 alembic_version 后无新迁移可执行；重复归档同一 run_id 触发主键冲突。集成测试分别验证两种行为。成功归档 CLI 输出 `status=archived`、模拟标识与事件数量；重复归档退出码 2，输出 `archive_failed / IntegrityError`。

## 小步 D：CI 与本地等价检查

`.github/workflows/ci.yml` 包含 Windows/Ubuntu 离线检查和单独的 PostgreSQL 18.6 服务检查，push、pull_request 或手动触发。只读仓库权限，不需要模型密钥，不下载 NHTSA 资料。安装 Python/依赖、获取 Action 和拉取数据库镜像仍需网络；“离线”指业务测试不访问外部数据或模型，不是整个 CI 无网络。

checkout 与 setup-uv 固定到已核对的提交，uv 固定 0.11.26、Python 固定 3.12.14。CI 数据库密码只用于一次性测试服务，不能复用于部署。PostgreSQL 镜像固定版本标签，尚未固定镜像 digest。参见 [GitHub PostgreSQL 服务教程](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)、[checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)、[setup-uv v10.3.2](https://github.com/astral-sh/setup-uv/releases/tag/v10.3.2)。

```powershell
uv sync --locked --python 3.12
uv run --locked --offline python -m scripts.verify_offline
```

依次运行 Ruff 格式与静态检查、mypy、离线 SQL 生成、pytest 和真实回环 HTTP。mypy 只检查 pyproject 中列出的 9 个源码文件，并检查其函数体；未声称全项目严格类型检查。既有源码统一格式与导入顺序，未有意改变上一小步行为；回归测试通过。

pytest 默认选择 `not postgres`；指定 `-m postgres` 才运行数据库组，缺测试数据库环境变量时明确报错，不静默跳过。Windows 沙箱的 asyncio 问题已在第三步记录，本轮采用正常权限运行同样的本地验证，不修改业务逻辑规避。

## 实际结果与模块验收

| 检查 | 本轮实际结果 | 可以证明的范围 |
| --- | --- | --- |
| Ruff 格式 / 静态检查 | 通过 | 当前配置下的代码一致性和静态问题 |
| mypy | 9 个源码文件通过 | 当前选择范围的类型检查 |
| 无数据库测试 | 47 通过，0 失败/错误/跳过；12 项数据库测试未选入 | 契约、运行边界、API、工件、归档加载 |
| PostgreSQL 测试 | 12 通过，0 失败/错误/跳过 | 实际迁移/升降级、结构对账、归档回读、约束、回滚、在线迁移与 CLI |
| 独立回环 HTTP | 7 类通过，服务已停止 | 真实本地 HTTP 与原始工件字节校验 |
| GitHub Actions | 工作流已编写，YAML 已本地解析；未在 GitHub 执行 | 不能声称远端 CI 已绿 |
| 真实模型 / 调查质量实验 | 0 次 | 不报告模型准确率、token 或费用 |

本地合计 59 项 pytest 测试通过，分无数据库组与 PostgreSQL 组执行，另有 7 类 HTTP 检查。记录：`docs/verification/2026-10-26-offline-ci-local.json`、`docs/verification/2026-10-26-postgres-step.json`、`docs/verification/2026-10-26-http-step4.json`。

对应实现提交：`a239439`。保留原始计划文件哈希，未删减后续模块。

P1-01 产物已具备：服务、命令入口、任务与模型/工具契约、开发配置与调用限制、工件和事件、初始迁移、开发夹具和离线 CI。非法输入拒绝、任务/调用关联、密钥外置、模拟模型显式区分均有验证。本模块只建立工程基础，不提前声称完成数据发布、正式授权、后台恢复、调查 Agent 或模型评测。

下一模块 P1-02 仍保留计划全部目标：真实数据获取、不可覆盖的原始快照、字段与时间语义、幂等导入及资料可用性矩阵。等待学习者确认本模块理解后开始。

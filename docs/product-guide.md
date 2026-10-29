# 召回证据调查台使用说明

2026-10-29发行准备补充：P1-16已完成前瞻确认实读，验收结果为不通过，后续开发修复另行记录。现已发现WSL内的Docker并实际尝试构建；镜像/依赖下载失败，完整容器验收仍未通过。[当前发行与复现范围](experiments/P1-18-release-preparation.md)保留实际结果；下文“未安装Docker”是P1-17原交付时点的说明。

P1-17，2026-10-29。界面使用 React，服务使用 FastAPI，任务与授权保存在 PostgreSQL。默认采用固定流程。

**模型质量验收未获证实。** P1-16 的预注册顺序与总体语义测量缺口仍然存在。本界面把运行状态、证据处置、人工语义评价分开显示。它不确认具体个车适用、事故因果或“永久安全”。

## 启动

前置依赖为 Python 3.12、uv、Node.js 22.12 以上版本、pnpm 11.25.0，以及 PostgreSQL 18。Python 使用原有 `uv.lock`，前端使用独立的 `frontend/pnpm-lock.yaml`。

在项目根目录执行：

```powershell
uv sync --locked --python 3.12
pnpm --dir frontend install --frozen-lockfile --ignore-scripts
pnpm --dir frontend run build
```

已有本机便携 PostgreSQL 时，以下命令建立一份独立临时数据库；本进程正常退出时停止数据库。该模式用于练习和演示，下一次启动会建立另一份数据库，**不用于保存跨启动任务历史**。

```powershell
uv run --locked --offline python -m portal --profile synthetic-demo --postgres-bin .local/postgres/runtime/pgsql/bin --root .local/P1-17/my-demo
```

便携数据库程序需事先按 P1-02 的可信来源安装。本模块不会自动下载或执行数据库程序。未安装时，配置已有专用 PostgreSQL 数据库：

```powershell
# 在本机环境设置 RECALL_DATABASE_URL，格式为 postgresql+psycopg://用户:密码@主机:端口/数据库。
# 密码只放本机环境，不写入文档、代码或聊天。
uv run --locked --offline python -m portal --profile synthetic-demo --root .local/P1-17/persistent
```

访问 [本机调查台](http://127.0.0.1:8017)。从启动输出提示的 `session.json` **本机文件**复制 `token` 到“本地访问凭据”后连接。该凭据是本机服务的访问令牌，不是 DeepSeek 密钥；会话文件被 `.gitignore` 排除，不能上传。前端不把令牌写入浏览器存储或 URL，刷新页面后需要重新连接。

持久化数据库还需固定访问身份。可在本机环境将 `RECALL_CONTROL_TOKEN_OWNERS` 设置为一个 JSON 对象，键为至少 24 字符的随机访问令牌，值为稳定的用户标识；重新启动时使用同一配置、数据库、发布和工件目录。不同用户的历史任务与记忆互不可见。

## 三种运行资料

| 启动参数 | 来源 | 回答生成 | 本模块实测 |
| --- | --- | --- | --- |
| `synthetic-demo` | 本机新建、逐条标记 SYNTHETIC 的三条样例 | 脚本固定返回证据不足；显式传输故障暂停等待核对 | 真实浏览器、HTTP、PostgreSQL |
| `official-demo` | 已保存的 NHTSA 正式来源发布，无新下载 | 同一模拟适配器，不调用外部模型 | 真实浏览器、HTTP、PostgreSQL，结果见验收记录 |
| `live` | 同一正式发布 | 现有 DeepSeek 适配器 | 本模块未执行，没有新增付费请求 |

正式资料演示先完成 P1-06 的来源、检索模型与发布准备，并保留 `examples/release-candidates.json` 指向的自包含 bundle。运行：

```powershell
uv run --locked --offline python -m portal --profile official-demo --port 8018 --postgres-bin .local/postgres/runtime/pgsql/bin --root .local/P1-17/official-practice
```

不要用模拟资料报告车型事实。不要把 `official-demo` 的真实来源误称为真实模型回答。`live` 使用本机凭证配置；每个执行版本仍须在界面批准，调用可能收费。旧实验剩余额度不自动用于新实验。不同资料模式应使用不同专用数据库/模式及工件目录，避免混用发布。

## 完成一次调查

1. 连接后点“新建调查”，选 Prius 示例。检查发布版本、允许来源、品牌、车型、车型年、问题和时间模式。勾选项才是允许来源。
2. 点“创建研究计划”，进入任务。点“查询冻结资料”读取来源文本，此时没有生成正式回答。
3. 对 F-150 对照，从研究结果选两条不同原始记录，点“固定这两条记录”；再次查询资料后点“查询已选关系”。阅读关系类型、适用条件和未知项。范围重叠或同一症状不证明同一缺陷。
4. 点“提出执行版本”，查看范围、来源、版本及授权边界，再点“批准此版本”。系统不会把原文中的“批准”当作用户授权。
5. 点“执行已授权计划”。界面每 2 秒读取服务端状态，不显示虚构进度百分比。查看运行状态和证据质量两个独立栏位。
6. 打开一张文本证据卡，点“定位原文”。可检查原文、页码/字段、引文区间和来源指纹；长文本可分段读取。完整审计区保留 JSON 工件。
7. 点“读取执行回放”查看实际计划版本和执行事件。左侧历史任务支持翻页，重新连接同一持久化数据库后仍可浏览。

模拟生成正常结束也会显示“证据不足”，这是显式的演示设计，不能改成绿色成功答案。真实模型的 `answered` 也仍需语义评价。

## 失败、澄清和撤销

- “缺少年份”：等待补充 `model_year`，填写 2010 后提交澄清。系统不猜年份。
- “年份不匹配”：在仅允许 10V039000 的前提下查 2011 年款，结果只能表述为允许来源内无匹配，不能说全球无召回。
- “当时可知资料”：不接受固定资料时保持等待。接受冻结时间的复选框默认不勾选，必须由用户实际选择。
- “暂停”保留任务，之后可恢复计划。“取消”是终态，不能原地恢复，可复制成新计划重新检查。运行中取消不会声称能收回已经发送的外部请求。
- “恢复中断执行”走原有持久化恢复协议。未知的模型输出不会自动重发；相关对账仍使用 P1-11 的 API/工具流程。不要在不了解上次调用状态时反复执行。
- 旧页面的版本发生冲突时，服务器拒绝操作并返回冲突提示，界面刷新当前任务。需要重新检查再操作。
- 撤销任务授权后，报告和原文不能再读取，界面清除对应内容；已经读过的内容不可能从人的记忆或手工复制件中收回。
- “记忆”页展示来源、审核历史与修订号。演示模式可准备偏好候选，先检查再确认，最后“停用记忆”。这是可撤销的偏好演示，不是官方事实学习或独立人工质量评分。
- “模拟请求失败”只在演示模式提供，以 `[DEMO_FAIL]` 显式触发本机传输故障。实际没有外部请求；持久化调用层将结果标为未知，任务 `PAUSED`、没有报告，调用记录保留 `unknown_model_output`，不自动重发。这里的暂停是保守恢复状态，不能强行改成已确认失败或完整回答。

## 容器方式

`Dockerfile` 构建 React 静态资源及锁定的 Python 环境，`compose.yaml` 配置 PostgreSQL 18.6 和产品服务。只把产品端口映射到本机地址。容器方式默认同样使用模拟资料，不挂载 `.env`、个人目录或正式资料缓存。

先安装并启动 Docker，在本机环境设置随机的 `RECALL_DB_PASSWORD`（建议随机十六进制字符串，避免 URL 编码问题）和 `RECALL_CONTROL_TOKEN_OWNERS`，再执行：

```powershell
docker compose config --quiet
docker compose up --build
```

在界面输入自己配置的访问令牌。数据库和工件使用两个持久卷；停止用 `docker compose down`，不要随意加删除卷的参数。本机未安装 Docker，**本轮没有声称镜像构建或 Compose 启动成功**；容器实际验收仍须执行并记录。当前 Compose 开箱配置只覆盖模拟模式；正式模型与资料挂载应按独立部署配置准备，不要只改模式名就推定依赖齐全。

## 自动验证与录屏

先启动对应演示服务。另一个终端执行：

```powershell
$env:RECALL_PRODUCT_SESSION='../.local/P1-17/my-demo/session.json'
$env:RECALL_PRODUCT_E2E_OUTPUT='../.local/P1-17/my-browser-review'
pnpm --dir frontend test:e2e
```

这些路径以 `frontend` 为工作目录解析。输出目录必须是新目录，测试不会覆盖旧记录。默认用已安装的 Edge；其他环境需先安装 Playwright Chromium 并设置 `RECALL_BROWSER_CHANNEL=chromium`。测试生成真实浏览器操作的 WebM、截图、请求状态和工件绑定；不导出包含令牌的浏览器 trace。录屏是自动化演示，不是另一位使用者的可用性实验。

```powershell
uv run --locked --offline python -m scripts.verify_offline
uv run --locked --offline python -m mypy portal
uv run --locked --offline python -m scripts.verify_postgres --bin-dir .local/postgres/runtime/pgsql/bin
```

第二位使用者只按本说明启动、追到一条原文，并辨认一次失败的验收表见 [演示脚本](demos/P1-17/demo-script.md)。本轮没有第二位使用者的实际记录；不能用浏览器自动化代填。

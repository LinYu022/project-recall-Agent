# 车辆召回调查 Agent

一个面向车辆召回资料的调查 Agent 项目。围绕用户问题检索资料、调用查询工具、整理支持与反对证据，并生成带原文引用的调查结论，提供中文网页界面。

## 项目亮点

- **多种调查方式**：支持固定流程、单 Agent、并行工具和多 Agent 协作，可在任务计划中选择 `fixed`、`single_agent`、`parallel_tools` 或 `multi_agent`。
- **可追溯的 RAG**：支持 BM25、向量检索、混合检索和重排；证据绑定资料版本，可查看引用摘录及原文位置。
- **计划与执行分离**：先确认调查范围和执行计划，再授权模型调用；支持条件澄清、反证检查与调用额度控制。
- **持久化任务与记忆**：保存任务状态和执行记录，支持暂停、恢复，以及语义、情景和程序三类记忆的复用与撤销。

## 使用方式

1. 获取代码：
   ```sh
   git clone https://github.com/LinYu022/project-recall.git
   cd project-recall
   ```
2. 将 `.env.example` 复制为 `.env`，填写仅含字母和数字的随机数据库密码，并将访问凭据填写为 `{"你的随机访问令牌":"user"}`。
3. 启动：
   ```sh
   docker compose up --build -d
   ```
4. 打开 <http://127.0.0.1:8017>，输入上一步设置的访问令牌。在页面中选择资料、填写问题、保存计划，确认后执行。
5. 使用完毕运行 `docker compose down`。任务数据会保留在本机 Docker 数据卷中。

默认使用合成示例资料和本地模拟模型，便于先体验完整流程。

## 调用真实 LLM

当前模型适配器接入 DeepSeek。在 `.env` 中填写：

```dotenv
RECALL_PRODUCT_PROFILE=llm-demo
RECALL_MODEL_API_KEY=你的DeepSeek密钥
RECALL_MODEL_BASE_URL=https://api.deepseek.com
RECALL_MODEL_ID=deepseek-flash
```

再次运行 `docker compose up --build -d`，在网页中创建任务、确认计划并批准执行，即可调用真实模型。密钥仅保存在本机 `.env`，调用费用由模型服务商计算。

`llm-demo` 使用项目内的合成示例资料和真实 LLM。已有真实资料版本时，可在原生 Python 环境中通过 `python -m portal --profile live --release-index 本地资料索引.json` 加载，数据库连接由 `RECALL_DATABASE_URL` 指定。

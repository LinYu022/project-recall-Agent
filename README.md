# 车辆召回资料助手

用于检索车辆召回资料、整理证据并查看引用原文。提供中文网页界面，支持创建调查计划、确认执行、查看结果和任务记录。

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

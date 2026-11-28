# ProjectPilot

ProjectPilot 是一个面向课程作业场景的 AI 项目调研助手：上传作业材料后，系统提取并确认需求，搜索 GitHub 候选仓库，采集 README 证据，进行检索与深度核查，最后输出带原文证据的项目评估和推荐报告。

## 功能

- 上传 PDF、DOCX 或纯文本作业材料
- 使用 DeepSeek 提取需求，并支持人工编辑、确认和版本管理
- 将确认后的需求用于 GitHub 仓库发现
- 采集 README 快照，保留可定位的原文证据
- 使用 BAAI/bge-m3 + Chroma 做混合检索
- 对候选仓库进行逐仓库深度核查
- 使用确定性 Python 逻辑生成评分、排序和推荐
- 前端展示需求、证据、核查结果和推荐理由

## 技术栈

- 后端：FastAPI、Pydantic、SQLAlchemy、Alembic、MySQL（仅搜索阶段）
- 前端：React、TypeScript、Vite
- 模型：DeepSeek Chat Completions
- Embedding：SiliconFlow / BAAI/bge-m3
- 向量检索：Chroma
- 对象存储：本地目录（开发）或 S3 兼容存储（生产）

## 目录结构

```text
backend/
  app/            后端应用代码
  migrations/     Alembic 迁移
  tests/          后端测试
frontend/
  src/            前端源码
  *.test.mjs      前端测试脚本
docs/             设计与验收文档
docs/archive/     历史过程文档
```

## 快速启动

### 1. 环境变量

在项目根目录复制 `.env.example`：

```text
复制 .env.example 为 .env
```

然后按需填写：

- `SILICONFLOW_API_KEY`
- GitHub Token（如使用搜索阶段）
- `MYSQL_*`（如使用搜索持久化）

DeepSeek Key 不在服务端默认配置中保存。用户必须在页面顶部的模型凭据卡片中填写自己的 Key；未填写时提取、搜索规划和深度核查会被拒绝。

`.env` 已被 Git 忽略，禁止提交真实密钥。

### 2. 后端

使用 Python 3.12：

```text
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[test]"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果使用项目根目录已有的虚拟环境，也可以直接：

```text
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

后端默认地址：

```text
http://127.0.0.1:8000
```

### 3. 前端

```text
cd frontend
npm install
npm run dev
```

前端默认地址：

```text
http://127.0.0.1:5173
```

Vite 已配置将 `/api` 代理到 `http://127.0.0.1:8000`。

## 测试

### 后端

```text
cd backend
python -m pytest
```

### 前端

```text
cd frontend
node --test *.test.mjs
npm run build
```

## 搜索数据库

搜索阶段使用 MySQL。建表脚本见：

```text
docs/ProjectPilot_MySQL_Create_Tables.sql
```

数据库设计见：

```text
docs/ProjectPilot_MySQL_Design.md
```

在线 Alembic 迁移默认关闭，需显式设置 `PROJECTPILOT_ALLOW_MIGRATIONS=1` 并确认目标数据库后再执行。

## 主要 API

```text
POST /api/v1/sessions
POST /api/v1/tasks
POST /api/v1/tasks/{task_id}/input
GET  /api/v1/tasks/{task_id}/input
POST /api/v1/documents/{document_id}/requirement-extractions
GET  /api/v1/documents/{document_id}/requirements
POST /api/v1/requirement-revisions/{revision_id}/confirm
POST /api/v1/tasks/{task_id}/search-runs
GET  /api/v1/tasks/{task_id}/search-runs/{run_id}
GET  /api/v1/tasks/{task_id}/search-runs/{run_id}/retrieval-evidence
POST /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
GET  /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
```

## 重要限制

- 本次提交为课程竞赛 MVP，不是生产级 SaaS。
- 需求提取、搜索、深度核查均可能产生第三方 API 费用。
- 不自动重试付费模型调用；失败结果需要人工确认后重新发起。
- README 证据表示“文档中提及”，不等同于功能已经实际运行验证。
- 本地 `data/`、Chroma 索引、日志和虚拟环境属于运行产物，不应提交。
- 前端生产构建产物为 `frontend/dist/`，仅用于静态部署，不进入源码提交包。

## 安全说明

- 不要提交 `.env`、API Key、数据库密码、会话 Token 或含隐私的作业原文。
- 生产环境使用 HTTPS，并将对象存储、MySQL 和 Chroma 保持为私有服务。
- 用户提供的模型 Key 仅在请求期间使用，不写入数据库或日志。

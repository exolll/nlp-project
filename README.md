# ProjectPilot 比赛版

ProjectPilot 帮助学生把课程作业要求转成可核对的需求，再搜索 GitHub，找出与需求匹配的项目。用户可以上传 PDF 或 DOCX，也可以直接输入文字。**确认需求后才开始搜索**。最终结果展示仓库排名、逐项匹配判断和对应证据。

本文对应比赛版 `slave` 分支提交 [`4e01aa5`](https://github.com/exolll/nlp-project/commit/4e01aa5ad01a0799aa30f394174052acef057b69)。

## ProjectPilot 做什么

**提交作业 → 预览原文 → 提取并确认需求 → 搜索 GitHub → 查找仓库证据 → 查看排名和引用**

| 你在页面上做什么 | 系统接着做什么 | 从哪个文件看起 |
| --- | --- | --- |
| 上传作业，或粘贴文字 | 接收 PDF、DOCX 或纯文字。上传文件时可以另填补充说明。解析正文并保留来源位置，供预览核对。 | [上传页面](frontend/src/main.tsx) · [文档解析](backend/app/services/parsing.py) |
| 提取需求，修改并确认 | 从原文提取主题、硬性功能、偏好和技术约束。保存修改后的版本，搜索绑定用户最终确认的需求。 | [需求确认面板](frontend/src/RequirementsPanel.tsx) · [需求服务](backend/app/services/requirements.py) · [需求格式](backend/app/schemas/requirements.py) |
| 开始搜索 | 把确认后的需求转成查询，调用 GitHub 获取候选仓库，并记录候选从哪条查询命中。 | [查询规划](backend/app/services/query_planner.py) · [搜索执行](backend/app/services/search_execution.py) · [GitHub 客户端](backend/app/services/github_client.py) |
| 查看候选的相关资料 | 保存候选仓库 README 快照，切成片段。用 BM25、关键词打分和向量检索找相关内容，并融合排序。 | [README 与索引](backend/app/services/chunk_index.py) · [证据检索](backend/app/services/retrieval.py) · [证据组装](backend/app/services/evidence_bundle.py) |
| 查看推荐排序，点开引用核对 | 逐项核查硬性条件和偏好，检查引用与快照是否对应，再汇总成排名和说明。 | [搜索与推荐面板](frontend/src/SearchPanel.tsx) · [深度核查流程](backend/app/services/deep_review_pipeline.py) · [引用校验与排序](backend/app/services/deep_review_logic.py) |

例如，作业写「必须能在网页上传讲义并基于它问答，能点开引文查看原文可加分」。确认需求时，上传和问答应是硬性条件，查看引文应是偏好。拿到推荐后，打开引文核对它是否证明的是同一仓库、同一版本下的实际操作流程。

前后端接口从 [FastAPI 入口](backend/app/main.py) 看起。搜索及深度核查接口分别在 [search_routes.py](backend/app/services/search_routes.py) 和 [deep_review_routes.py](backend/app/services/deep_review_routes.py)。

## Windows 本地启动

准备 Python 3.12 或更高版本，以及可运行 Vite 的 Node.js 和 npm。以下命令以 PowerShell 为例。

```powershell
git clone --branch slave --single-branch https://github.com/exolll/nlp-project.git
cd nlp-project
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
Copy-Item .env.example .env
```

已有 `.env` 时，保留原文件并对照模板补齐字段。运行范围决定需要配置哪些服务。

| 要测到哪一步 | 需要准备什么 |
| --- | --- |
| 上传、解析和预览 | 本地文件存储，可用下面的开发预览入口。 |
| 真实需求提取、编辑和确认 | 另配 `DEEPSEEK_API_KEY`，模型及地址参照 [.env.example](.env.example)。 |
| GitHub 搜索、证据检索和排名 | 另配 `GITHUB_TOKEN`、可访问的 MySQL 数据库和 `MYSQL_*` 参数。启用向量检索时，按模板配置 `EMBEDDING_*` 和 `SILICONFLOW_API_KEY`，并安装 ChromaDB。真实凭据只填本地 `.env`。 |

完整链路先创建与 `MYSQL_DATABASE` 一致的数据库，再从仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install chromadb
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
```

**终端一**从仓库根目录启动后端。开发预览使用本地文件存储。完整推荐功能是否可用取决于上表的服务配置。

```powershell
.\.venv\Scripts\python.exe backend/dev_preview.py --test-data-directory ./data/local-preview
```

**终端二**从仓库根目录进入前端后启动：

```powershell
cd frontend
npm ci
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。前端开发服务将 `/api` 请求转发到本地后端的 `8000` 端口。

## 用课程作业跑一次

1. 先上传一份文字可选中的 PDF，例如「课程讲义问答」。预览应保留作业正文和页码。
2. 点击提取需求。对照原文，核查「必须」「优先」「选做」有没有被混淆。必要时编辑，再确认版本。
3. 如果已配置完整链路，发起 GitHub 搜索，记录候选和最终排名。至少打开一条引用，验证它确实支持页面上的判断。
4. 再用「实验报告网页导出」做对照。这份作业要求从网页生成 PDF。只证明仓库能上传 PDF 的引用不足以支持导出能力。

真实 GitHub 候选会随搜索时间变化。验收时看确认后的条件、仓库是否符合以及引文是否有效。

## 发现问题时怎么定位

| 现象 | 先核对什么 |
| --- | --- |
| 页面打不开或接口失败 | 前端是否在 `5173`、后端是否在 `8000`，以及后端终端报错。 |
| 预览缺段或顺序异常 | 对照 PDF 原页和解析预览。全扫描件可能缺少可提取的文本。 |
| 把选做提成必须 | 保存作业原文和提取结果，看偏差是否发生在确认之前。 |
| 需求正确，候选仍跑偏 | 记录实际查询、限定词和候选仓库。先检查检索条件和 GitHub 请求状态。 |
| 有候选但无法进入深度核查 | 核对 MySQL 迁移、README 快照、嵌入配置和 Chroma 路径。 |
| 排名靠前的仓库缺少功能证据 | 核对逐项判断、引用归属和原文。只验证到部分必要条件时，应如实记录。 |

报告问题时保留提交号、输入样例、确认后的需求、实际查询、推荐与引用，以及最早出现偏差的步骤。

## 开发检查

从仓库根目录运行后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

在 `frontend` 目录运行 `npm run build` 检查前端构建。涉及真实搜索或模型的验证脚本位于 `backend/validate_*_live.py`，运行前应先准备对应服务凭据。

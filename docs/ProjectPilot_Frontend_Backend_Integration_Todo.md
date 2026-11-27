# ProjectPilot 前后端接口待对接清单

> **本文性质**：团队协商方案。文中所有「建议接口」**均未实现**，不代表现有后端具备该能力。
> **现有后端契约冻结**：当前 `backend/app` 共 19 个 HTTP 路由，本次前端改版不修改其中任何一个。
> 生成日期：2026-11-26 ｜ 依据：`slave` 分支 `4e01aa5` ｜ 最近更新：2026-11-27（界面 06 最终结果改为左右工作台，仅前端）

---

## 一、现有契约冻结清单（前端正在依赖，联调时不得破坏）

| # | 方法与路径 | 用途 | 关键约束 |
|---|---|---|---|
| 1 | `POST /api/v1/sessions` | 创建会话 | 返回 `access_token`，`owner = sha256(token)` |
| 2 | `GET /api/v1/config` | 上传限制 | 仅返回 6 个输入限制字段 |
| 3 | `GET /api/v1/tasks` | 历史任务列表 | **仅当前会话**，`created_at` 降序，无分页 |
| 4 | `POST /api/v1/tasks` | 新建任务 | 空请求体 `Contract`，上限 `MAX_SESSION_TASKS=100` |
| 5 | `POST /api/v1/tasks/{id}/input` | 上传材料 | multipart（仅 `file`/`text`），需 `Idempotency-Key`，202 |
| 6 | `GET /api/v1/tasks/{id}/input/status` | 解析状态 | — |
| 7 | `GET /api/v1/tasks/{id}/input` | 标准化文档 | 无文档时不可用 |
| 8 | `GET /api/v1/documents/{id}/requirements` | 需求提取与版本 | 返回 `extraction` / `revisions` / `latest_revision_id` |
| 9 | `POST /api/v1/documents/{id}/requirement-extractions` | 触发提取 | 需 `Idempotency-Key`，202，计费 |
| 10 | `POST /api/v1/requirement-revisions/{id}/revisions` | 保存新版本 | `STALE_REVISION` 409 只允许基于最新版 |
| 11 | `POST /api/v1/requirement-revisions/{id}/confirm` | 确认版本 | — |
| 12 | `POST /api/v1/tasks/{id}/search-runs` | 启动搜索 | 需 `Idempotency-Key`，`SearchBudget` 有区间校验，202 |
| 13 | `GET /api/v1/tasks/{id}/search-runs/{run_id}` | 运行状态 | 返回 `run_view`，含 `confirmed_revision_id` |
| 14 | `GET .../search-runs/{run_id}/evidence` | 字面证据 | — |
| 15 | `GET .../search-runs/{run_id}/retrieval-evidence` | 检索片段 | **唯一带查询参数**：`repository_id`、`requirement_item_id` |
| 16 | `GET`/`POST .../search-runs/{run_id}/deep-check` | 深度核对（旧版） | POST 需 `Idempotency-Key`，计费 |
| 17 | `GET`/`POST .../search-runs/{run_id}/deep-review` | 逐仓库深度核查 | POST 计费，产出 `ranking` |
| 18 | `POST .../search-runs/{run_id}/evidence` | 证据发布（前端暂未使用） | — |

**全局约束**：所有请求体模型 `extra='forbid'`；错误统一为 `ErrorResponse{error_code, message, retryable, details, request_id}`；鉴权为 `Authorization: Bearer <token>`；CORS 允许 `127.0.0.1:5173`。

---

## 二、待对接项（均为团队协商建议，未实现）

### 1. 任务标题持久化与重命名

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 自动生成标题后写回服务端；用户内联重命名（Enter 保存 / Esc 取消）；顶栏与历史列表同步 |
| 所需请求方式 | `PATCH /api/v1/tasks/{task_id}` |
| 建议请求参数 | `{ "title": "string, 1–80 字符" }` |
| 建议返回数据 | 完整 `TaskResponse`，并新增字段 `title: string \| null` |
| 当前后端支持情况 | ❌ `TaskResponse` 仅 4 字段（`schemas/documents.py:67-71`）；路由表中无 PATCH |
| 前端当前处理方式 | `capabilities.taskTitle = false`；默认标题为 `任务 {task_id 前 8 位}`；提取完成后若用户未手动改过标题，前端用最新版本的 `project_topic` 自动命名（仅存 localStorage，不写服务端）；用户手动编辑后自动命名永不覆盖 |
| 后续联调要求 | ① `TaskResponse` 增 `title` 后需同步出现在 `TaskList`；② 明确 `title` 空串是否合法（建议 `null` = 未设置、空串非法）；③ PATCH 返回完整响应以便就地更新列表；④ 标题由用户编辑后，自动生成逻辑不得覆盖 |

### 2. 删除任务

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 历史抽屉三点菜单 → 删除 → 前端二次确认 → 调用删除 → 列表移除 |
| 所需请求方式 | `DELETE /api/v1/tasks/{task_id}` |
| 建议请求参数 | 无请求体 |
| 建议返回数据 | `204 No Content`；失败时 `ErrorResponse` |
| 当前后端支持情况 | ❌ 19 个路由无任何 DELETE；`storage.delete` 仅为对象存储内部调用 |
| 前端当前处理方式 | 菜单项渲染但 `disabled`，Tooltip「待后端接口支持」——不做"本地隐藏"式假删除 |
| 后续联调要求 | ① 需明确级联范围：输入对象、`NormalizedDocument`、需求版本、search runs、manifest、MySQL 记录；② 是否允许删除 `processing` 中任务（建议 409）；③ 删除后 `GET /tasks` 立即不再包含；④ 二次确认由前端负责，后端必须幂等（重复删除返回 404 或 204 需约定） |

### 3. 搜索运行列表查询（阶段回看的前置条件）

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 打开历史任务时列出该任务全部 runs，恢复上次所在阶段 |
| 所需请求方式 | `GET /api/v1/tasks/{task_id}/search-runs` |
| 建议请求参数 | 可选 `limit`（建议默认 20）、`order`（建议默认 `created_at desc`） |
| 建议返回数据 | `{ "runs": [ { "run_id", "task_id", "confirmed_revision_id", "pipeline_status", "evidence_status", "created_at" } ] }` |
| 当前后端支持情况 | ❌ 无。每次 run 只能由客户端记住 `run_id` 后逐个寻址 |
| 前端当前处理方式 | `localStorage` 记录 `{task_id: {revision_id, run_id}}`；换浏览器或清缓存后无法回看已完成阶段 |
| 后续联调要求 | ① 至少返回 `confirmed_revision_id` 与 `created_at`，前端需据此计算「待更新」；② 该接口就绪后，localStorage 降级为纯缓存，不作为权威来源；③ 需支持空列表而非 404 |

### 4. 跨会话任务恢复

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 重新打开浏览器后，用上一次的任务标识恢复真实任务 |
| 所需请求方式 | 方案 A：`POST /api/v1/sessions/refresh`；方案 B：延长会话并允许携带已过期 ≤N 天的 token 恢复 |
| 建议请求参数 | 方案 A：请求头携带旧 token，或请求体 `{ "refresh_token": "..." }` |
| 建议返回数据 | 新 `SessionResponse`，且**沿用同一 `owner`** |
| 当前后端支持情况 | ❌ `owner = sha256(token)`（`main.py:97-101`），换 token 即换身份；会话默认 24 小时（`SESSION_HOURS`） |
| 前端当前处理方式 | token 存 sessionStorage；刷新页面可恢复，**重开浏览器必然新建会话、旧任务 401**，界面明确提示「上次任务属于已结束会话」并提供开始新任务入口 |
| 后续联调要求 | ① 需定义刷新失效错误码，前端据此显示重试入口；② 明确同一 `owner` 跨 token 复用的安全边界；③ 前端在能力开启前不得把 token 迁到 localStorage |

### 5. LLM 供应商配置读写

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 设置抽屉「API 配置」：读取当前配置、填写并保存供应商 / Base URL / 模型 / API Key |
| 所需请求方式 | `GET /api/v1/settings/llm`、`PUT /api/v1/settings/llm` |
| 建议请求参数 | PUT：`{ "provider": "deepseek \| siliconflow", "base_url": "https://...", "model": "string", "api_key": "string" }` |
| 建议返回数据 | `{ "provider", "base_url", "model", "api_key_configured": true, "api_key_masked": "sk-****abcd" }`——**任何情况下不回传明文 key** |
| 当前后端支持情况 | ❌ 无接口。`settings.py:58-75` 中 `deepseek_base_url` 与 `deepseek_model`、`embedding_model` 为 `Literal` 单值类型，仅 API Key 可变；所有请求体 `extra='forbid'`，前端发送 `api_key` 会得到 422 `INVALID_REQUEST` |
| 前端当前处理方式 | 仅只读展示真实字段 + 明显标注「待后端接口支持」；输入框存在但不会发送，也不声称已生效 |
| 后续联调要求 | ① `Literal` 需放开为 `str` + 白名单校验；② 需确认落盘方式（settings 为每请求惰性重建，改 `.env` 即下一请求生效，见 `requirements.py:254`）；③ 必须提供 `api_key_configured`，前端不得缓存明文；④ 需说明多供应商并存时各阶段分别使用哪个 provider |

### 6. LLM 连接测试

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 设置抽屉「连接测试」按钮 → 显示成功/失败与耗时 |
| 所需请求方式 | `POST /api/v1/settings/llm/test` |
| 建议请求参数 | 可选 `{ "provider": "..." }`；无参数时测试当前生效配置 |
| 建议返回数据 | `{ "ok": true, "latency_ms": 320, "model": "deepseek-flash" }`；失败时 `ErrorResponse` |
| 当前后端支持情况 | ❌ 无接口 |
| 前端当前处理方式 | 按钮只探测后端可达性（调用无副作用的 `GET /config`），文案明确「后端可达 ≠ API Key 有效」 |
| 后续联调要求 | ① 必须做最小真实调用并限流，避免被当作免费探测工具；② 错误码区分「网络不可达」「鉴权失败」「模型不存在」；③ 不得在响应中回显 key |

### 7. 当前生效配置只读查询

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 设置抽屉「模型参数」展示当前实际生效的模型与维度 |
| 所需请求方式 | 扩展现有 `GET /api/v1/config`（推荐，避免新增路径） |
| 建议请求参数 | 无 |
| 建议返回数据 | 在现有 6 字段基础上增加：`llm_provider`、`llm_model`、`embedding_model`、`embedding_dimension`、`embedding_enabled`、`search_enabled` |
| 当前后端支持情况 | ⚠️ `GET /config` 只返回 6 个输入限制字段（`schemas/documents.py:90-96`），不含任何模型信息 |
| 前端当前处理方式 | 只使用真实调用响应里的模型信息：`ExtractionJob.model/prompt_version/usage`、`RetrievalBundle.model/dimension`、`DeepReviewOperation.model/prompt_version/deadline`；尚无调用记录时显示「尚未有真实调用记录」，不猜测 |
| 后续联调要求 | ① 新增字段必须是**可选**扩展，不得破坏既有 6 字段；② 前端 `Config` 类型按可选字段处理以兼容旧后端 |

### 8.（非阻塞）任务列表分页

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 历史任务超过 100 条时继续浏览 |
| 所需请求方式 | `GET /api/v1/tasks?offset=&limit=` |
| 建议请求参数 | `offset`（默认 0）、`limit`（默认 50，上限 100） |
| 建议返回数据 | `{ "tasks": [...], "total": 123 }` |
| 当前后端支持情况 | ❌ 无分页参数；受 `MAX_SESSION_TASKS=100` 限制 |
| 前端当前处理方式 | 一次全量拉取 + 前端时间分组；任务量在 100 以内无影响 |
| 后续联调要求 | 仅当会话上限提高时才有必要实现；需保持 `tasks` 字段名不变以兼容 |

---

## 三、前端本地状态与自动流程（无需后端改动）

以下行为全部基于现有 19 条路由，不新增请求、不改请求体，仅说明联调时需要知道的前端约定：

1. **输入草稿**：`localStorage['projectpilot.input-draft.v1']`，只存文本与文件提示 `{ name, size }`，**不存文件内容、不存任何密钥**；约 1s 防抖写入，提交成功后清除。界面明确标注「仅保存在本浏览器 · 不代表服务端成功」。
2. **标准化预览开合偏好**：`localStorage['projectpilot.preview.v1']`，默认隐藏；开合只影响布局，不会重新请求或清空已解析文档与来源片段。
3. **标题**：`localStorage['projectpilot.ui.v1']` 保存 `title`、`autoTitle`、`titleEdited`；提取完成读取 `project_topic` 自动命名，手动编辑优先。后端接入 `title` 字段后应优先使用服务端值，本地字段降级为 UI 缓存。
4. **自动链**：点击「提交并开始分析」后前端依次执行 `POST /tasks` → `POST /tasks/{id}/input` → 轮询 `GET .../input/status` → `GET .../input`，成功后**仅自动发送一次** `POST /documents/{id}/requirement-extractions`，随后进入需求确认阶段。刷新、恢复任务、切换阶段子界面、开合预览均不会再次触发模型调用。
5. **失败处理**：上传网络失败/5xx 保留同一任务与 `Idempotency-Key` 供显式重试；解析失败（`status = failed`）或 422 时恢复已填材料，不自动重试；提取失败需用户显式点击「重新提取（新请求）」并生成新幂等键。
6. **阶段子界面**：顶部进度按钮切换 6 个独立子界面（输入 / 需求拆解 / 需求确认 / GitHub 初筛 / 深度筛选 / 最终结果）；各面板保持挂载以避免丢失编辑与轮询，但同一时刻只显示当前子界面，不使用锚点长页面滚动。
7. **界面 02 工作台布局（2026-11-27）**：左侧为固定概览栏（标题、版本状态、需求完整度、分组导航、缺失信息、来源证据/版本记录入口），右侧为独立滚动的编辑区，底部固定「保存为新版本 / 确认此版本」；不新增任何请求，保存与确认继续调用清单 #10/#11 的既有接口，确认本身仍不触发搜索。
8. **需求完整度**：仅前端按 9 个需求字段计算展示（`[]` 视为已明确为空、`null` 视为未提供），不参与后端校验，也不改变保存/确认判定；后端无需提供该字段。
9. **折叠面板**：条目约束与来源、语言 ALL / ANY、来源证据、版本变更记录默认折叠，展开后保留 UUID、证据关联、原始码点与全部版本操作；折叠只影响展示，不删除或重写任何服务端数据。
10. **提取与核对入口**：未提取、提取失败或幂等键未核实时默认展开；成功后收起为「提取与核对 · 状态」摘要，避免挤占首屏，仍保留显式重新提取与刷新核对。
11. **界面 03 工作台布局（2026-11-27）**：仅改阶段 4「GitHub 初筛」子界面——左侧固定运行概览栏（绑定版本 UUID、流水线/证据状态、固定预算、分区导航、授权与运行入口），右侧独立滚动「运行与候选 / 逐需求证据 / 检索片段」三个分区；深度筛选与最终结果界面未改动。不新增任何请求：显式授权仍为一次 `POST /search-runs`（沿用 sessionStorage 中的幂等键），刷新按钮仍只执行 `GET`（运行/证据/检索片段），轮询逻辑、证据引文与字节坐标、检索「相关性尚未核查」提示全部保留；左侧状态折叠只在绑定了运行 ID 后收起，不改变任何服务端数据。
12. **界面 04 工作台布局（2026-11-27）**：仅改阶段 5「深度筛选」子界面——左侧固定概览栏（运行/版本 UUID、深度核对与逐仓库核查状态、检索 Bundle 与排名状态、固定预算、三分区导航、两个独立授权区），右侧独立滚动「深度核对与中文推荐 / 逐仓库深度核查（MVP） / 授权与报告追溯」三个分区；最终结果界面未改动。不新增任何请求：深度核对仍为一次 `POST .../deep-check`（1 次模型调用），逐仓库核查仍为一次 `POST .../deep-review`（每仓库最多 1 次、总计最多 5 次模型调用），两者沿用原有幂等键与 `sessionStorage` 预留标记，重复进入或刷新只执行 `GET`（深度核对、深度核查、检索），轮询与停止条件、报告元数据（模型 / Prompt 版本 / Token 用量 / 时间）、完整性哈希与确定性排名全部保留；左侧授权折叠按状态自动收起，不改变任何服务端数据。
13. **界面 06 工作台布局（2026-11-27）**：仅改阶段 6「最终结果」子界面——左侧固定概览栏（运行/版本 UUID、逐仓库核查状态、排名规则与参与/未参与数量、报告生成时间、两分区导航），右侧独立滚动「推荐排名 / 报告与授权溯源」两个分区；不新增任何请求，排名继续只读取已发布的深度核查报告，确定性排名、推荐理由与引文坐标、限制说明、排除仓库说明、报告版本/时间与授权哈希全部保留；切换子界面或刷新只执行 `GET`，阶段 4/5 与顶部进度行为未改动。

### 仅开发环境：Mock 失败场景（默认关闭）

`frontend/mock/server.mjs` 支持 `MOCK_SCENARIOS=1`（或 `upload503,parse_fail,extract_fail` 组合）启用一次性演示失败：

| 场景 | 触发位置 | 错误形态 | 恢复方式 |
|---|---|---|---|
| `upload503` | 首次 `POST .../input` | `503 UPLOAD_UNAVAILABLE`，`retryable: true`，不落文档 | 用同一 `Idempotency-Key` 重试即成功 |
| `parse_fail` | 每个会话的第一份文档 | `status=failed` + `INPUT_PARSE_FAILED`；`GET .../input` 返回 422 | 新建任务后重新提交 |
| `extract_fail` | 每个会话的第一次提取 | `extraction.status=failed` + `EXTRACTION_FAILED`，`retryable: true`，不生成版本 | 显式「重新提取（新请求）」 |

场景仅在本地 Mock 生效，响应仍带 `X-ProjectPilot-Mock: 1`；生产/真实后端不受影响。

---

## 四、通用联调约定（新增接口时请一并遵守）

1. **请求体**：沿用 `Contract` 风格（`extra='forbid'`），空体用 `{}`。
2. **错误结构**：统一 `ErrorResponse{error_code, message, retryable, details, request_id}`，前端据此决定是否显示重试入口。
3. **计费类操作**：必须继续要求 `Idempotency-Key`，且同一 key 重复提交返回既有结果而非新建。
4. **兼容性**：只增字段、不改字段名与类型；新增字段一律可选。
5. **鉴权**：全部沿用 `Authorization: Bearer`，不要新增 cookie 机制。
6. **前端适配层**：上述能力全部由 `frontend/src/capabilities.ts` 开关控制；后端就绪后仅需翻开关并替换 `frontend/src/services/` 实现，页面组件无需改动。
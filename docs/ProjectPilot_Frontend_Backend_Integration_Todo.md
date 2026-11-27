# ProjectPilot 前后端接口待对接清单

> **本文性质**：团队协商方案。文中所有「建议接口」**均未实现**，不代表现有后端具备该能力。
> **现有后端契约冻结**：当前 `backend/app` 共 19 个 HTTP 路由，本次前端改版不修改其中任何一个。
> 生成日期：2026-11-26 ｜ 依据：`slave` 分支 `4e01aa5` ｜ 最近更新：2026-11-27（界面 03 右侧候选工作台、界面 04/05/06 展示优化与项目详情页；仅前端，无新增接口）

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

### 9.（非阻塞）历史需求版本恢复为最新版本

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 希望采用某个历史版本内容时，直接基于该历史版本创建新的最新版本 |
| 当前后端行为 | 编辑与确认接口只接受最新版本；对历史版本返回 `409 STALE_REVISION`（「版本已更新，请刷新后基于最新版本编辑」/「只能确认最新需求版本」） |
| 前端当前处理方式 | 历史版本只读；确认按钮标注「仅最新版本可确认」，保存按钮标注「历史版本不可保存」，并提供「回到最新版本」入口；不伪造历史版本确认 |
| 若产品需要 | 需后端新增「将历史版本复制为最新草稿/版本」接口，并明确证据、歧义引用与版本号的迁移规则；前端再接入，当前不阻塞主流程 |

### 10.（非阻塞）阶段 4 候选评分、排序与项目选择

| 项 | 内容 |
|---|---|
| 前端需要的操作 | 候选仓库展示综合匹配度分数与排序；标记「初筛通过 / 推荐深筛」；勾选部分项目后进入深度筛选 |
| 当前后端支持情况 | ❌ 搜索运行、证据与检索 Bundle 均无逐仓库匹配度 / 排名 / 初筛结论字段；`DeepCheckRequest` 只有 `revision_id` 与 `consent`，`deep-review` 无项目选择参数；现有 RRF 分数只描述片段相关性，不能当作仓库匹配度 |
| 前端当前处理方式 | 不渲染匹配度、通过率或勾选框，也不把 RRF 换算成百分制；「筛选结果」按 `retrievalChunkCount`（同分按提及数、仓库编号）确定性排序，卡片/表格切换仅本地视图状态；底部操作栏写明「候选 N 个」与阶段 5 单独授权，进入深度筛选只做阶段导航，不发起 POST |
| 若产品需要 | 需后端提供：① 逐仓库匹配度与排序规则版本（含分数构成，缺证据时的空值语义）；② 初筛通过 / 推荐深筛的确定性判定或模型判定及其原始状态；③ 深度筛选请求接受 `repository_ids` 选择，并明确未选仓库是否跳过、选择上限、费用影响与幂等键复用规则；④ 排名/初筛结果的持久化与回看接口 |
| 后续联调要求 | 不使用无证据的分数冒充匹配度；前端接入前保留当前「按片段数排序 + 不预选」的降级方案，接口就绪后再替换适配层 |

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
11. **界面 03 工作台布局（2026-11-27）**：阶段 4「GitHub 初筛」子界面——左侧固定运行概览栏（绑定版本状态、流水线/证据状态、固定预算、分区导航、授权与运行入口），右侧独立滚动「GitHub 候选项目 / 筛选结果 / 检索与追溯」三个分区；后续展示优化把 UUID 与原始枚举移入默认折叠的技术审计，普通视图只保留中文状态。不新增任何请求：显式授权仍为一次 `POST /search-runs`（沿用 sessionStorage 中的幂等键），刷新按钮仍只执行 `GET`（运行/证据/检索片段），轮询逻辑、证据引文与字节坐标、检索「相关性尚未核查」提示全部保留；左侧状态折叠只在绑定了运行 ID 后收起，不改变任何服务端数据。
12. **界面 04 工作台布局（2026-11-27）**：阶段 5「深度筛选」子界面——左侧固定概览栏（运行/版本状态、深度核对与逐仓库核查状态、检索 Bundle 与排名状态、固定预算、三分区导航、两个独立授权区），右侧独立滚动「深度筛选结论 / 仓库结论与需求匹配 / 授权与报告追溯」三个分区；后续展示优化把 UUID、哈希与原始枚举移入默认折叠的技术审计，普通视图只保留中文结论。不新增任何请求：深度核对仍为一次 `POST .../deep-check`（1 次模型调用），逐仓库核查仍为一次 `POST .../deep-review`（每仓库最多 1 次、总计最多 5 次模型调用），两者沿用原有幂等键与 `sessionStorage` 预留标记，重复进入或刷新只执行 `GET`（深度核对、深度核查、检索），轮询与停止条件、报告元数据（模型 / Prompt 版本 / Token 用量 / 时间）、完整性哈希与确定性排名全部保留；左侧授权折叠按状态自动收起，不改变任何服务端数据。
13. **界面 04/05 展示优化（阶段 5 深度筛选 / 阶段 6 最终结果，2026-11-27）**：两个子界面改为三层展示——普通视图只呈现中文结论、状态、优势/缺口与下一步；证据按需展开（README 引文、行号、字节区间、README 快照路径）；Run/Operation/版本 ID、哈希、原始枚举、完整时间戳全部保留在默认折叠的「技术审计」中，可复制且不在普通视图重复。阶段 6 左侧固定概览栏（推荐结论、参与/推荐/待核查/不推荐/未参与数量、友好规则名、报告时间、两分区导航），右侧独立滚动「推荐结论与依据 / 报告与授权溯源」；不新增任何请求，排名继续只读取已发布的深度核查报告，确定性排名、推荐理由与引文坐标、限制说明、排除仓库、报告版本/时间与授权哈希全部保留；切换子界面或刷新只执行 `GET`。
14. **界面 06 项目详情（阶段 6 内页，2026-11-27）**：在阶段 6 内部打开单仓库详情工作台，五个页签为「综合分析 / 需求匹配 / 证据核查 / 技术分析 / 技术审计」；**不发起任何新请求**（打开/关闭、切换页签、需求→证据跳转均复用已加载的 deep-review 报告与初筛检索数据），刷新时只执行既有 `GET`。详情只展示已发布报告与已保存 README 证据，不重新访问 GitHub、不重新调用模型；缺失字段显示「未提供」或明确口径说明，不以推算值替代。
15. **需求确认后进入阶段 4（2026-11-27）**：点击「确认此版本」并收到 `POST /requirement-revisions/{id}/confirm` 成功后，前端先刷新阶段信号，再用 `history.replaceState` 切到阶段 4「GitHub 初筛」并记录阶段；不重新打开任务、不新增请求、不自动搜索。已确认版本在底部操作栏保留「进入 GitHub 初筛 →」入口；搜索仍须在阶段 4 显式勾选授权后发起。后端接口与响应结构无需改动。
16. **界面 03 右侧候选工作台（2026-11-27）**：右侧顶部改为「GitHub 候选项目」总览（检索仓库 / 有检索片段 / 发现相关提及三块真实计数，证据未发布时显示 `—`），「筛选结果」支持卡片/表格本地切换与「检索片段数 ↓」确定性排序；候选卡片保留仓库元数据与逐需求引文，表格行的「查看初筛依据」就地展开同一卡片。底部操作栏「进入深度筛选 →」只做阶段导航：先刷新只读阶段信号，再切到阶段 5，不发起任何 `POST`、不调用模型、不经 GitHub；初筛证据未发布时按钮禁用。不新增接口，不改变现有请求与响应；评分与项目勾选等待后端支持（见第二节第 10 项）。
17. **深度筛选完成后的最终结果导航（2026-11-27）**：本窗口内刚授权且已完成的逐仓库深度核查，在报告 `ranking` 首次出现时自动切到阶段 6「最终结果」，每个 Operation 只自动跳转一次；导航前只刷新既有阶段信号（`GET` 任务/接收/搜索运行/深度核查），不新增 `POST`、不重复模型调用、不访问 GitHub。已发布报告后再次进入阶段 5 不会自动跳走，阶段 5 底部保留「查看最终结果 →」手动入口；报告已发布但 `ranking` 缺失时按钮禁用并说明「最终结果需要确定性排名」。自动跳转依赖 `rankingAvailable` 这一既有前端派生信号，无新增后端字段或接口。

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

---

## 五、展示层只读字段依赖（本轮无新增接口，联调时不得删改）

展示优化（界面 03/04/05 + 界面 06 项目详情）**没有提出新的 HTTP 路由、请求参数或响应结构**，全部读取既有 GET 响应。以下字段被用户视图、证据视图或技术审计直接展示，联调时请保持字段名、类型与语义不变；只增不改。

### 1. 逐仓库深度核查 `GET .../deep-review`

| 位置 | 展示用途 | 缺失/异常时的前端行为 |
|---|---|---|
| `repositories[].trace.{commit_sha, blob_sha, content_hash, readme_path, collected_at}` | 技术审计中的快照指纹与 README 路径 | 原样显示空值或「未提供」，不伪造 |
| `repositories[].input.{chunk_ids, chunk_count, input_bytes_upper_bound, budget}` | 技术审计中的输入范围与固定预算 | 缺字段按「未提供」 |
| `repositories[].usage` / `response_hash` | Token 用量与响应指纹 | `usage.known=false` 显示「Provider 未返回用量（未知）」 |
| `repositories[].hard_constraint_summary` / `logic_group_results` | 必需/禁止/偏好统计、逻辑组结果 | 缺数组按空处理，不推断 |
| `repositories[].requirements[]` 的 `verdict / reason_code / hard_constraint_check / citations / rejected_citations / model_omitted` | 需求匹配、证据核查、优势/缺口、下一步建议 | 未知枚举统一「未知状态」；无引文显示「信息不足」口径，不反转为「不满足」 |
| `requirements[].citations[].{chunk_id, quote, line_start, line_end, byte_start, byte_end}` | 证据原文、行号、字节区间、跳转与审计 | 字段缺失则引文不完整，仍保留可读提示 |
| `ranking.{ranking_config_version, scored_at}` | 推荐规则友好名与报告评分时间 | `scored_at` 缺失时回退报告 `created_at`；规则未知显示「未知状态」 |
| `ranking.repositories[]` 的 `repository_id / full_name / html_url / rank / hard_constraint_tier / hard_constraint_units / preferred / recommendation` | 排名卡、推荐结论、硬约束检查、偏好统计、审计 | `full_name` 缺失时回退初筛检索中的仓库名；`html_url` 仅接受 http(s)，否则不渲染链接 |
| `recommendation.reasons[]` 的 `requirement_item_id / text / citations` 与 `limitations` | 推荐依据、证据跳转、限制说明 | 无理由时明确「报告没有给出可展示的推荐理由」，不推断 |
| 可选字段 `total_score / evidence_coverage / score_breakdown / ranking_explanation`（v2 排名） | 综合得分、覆盖率、评分明细 | v1 报告无这些字段时显示「不适用 / 当前规则不产生百分制」，不视为数据缺失 |
| `excluded_repositories[]` 的 `repository_id / status / error_code / reason_code` | 「未参与排名」语义与审计 | 原样保留；UI 明确「未参与 ≠ 不符合需求」 |

### 2. 字面证据 `GET .../evidence`

| 位置 | 展示用途 | 约定 |
|---|---|---|
| `requirement_items[]` 顺序与 `requirement_item_id` | 生成「需求 1/2/3…」友好编号并建立跳转锚点 | **同一 run 内顺序必须稳定**；顺序变化只会导致编号重排，不丢数据 |
| `candidates[].availability / source` | 证据可用性与快照指纹 | — |
| `candidates[].items[].status` | 「发现相关提及 / 尚未核实」 | 只用于提示，不得解释为满足或不符合 |
| `items[].quotes[]` 的 `{text, line_start, line_end, byte_start, byte_end}` | 证据原文与坐标 | 缺失时该需求视为「尚未核实」 |

### 3. 检索片段 `GET .../retrieval-evidence`

| 位置 | 展示用途 | 约定 |
|---|---|---|
| `requirements[]` | 检索需求的友好字段名与关联描述 | — |
| `candidates[].repository.{full_name, html_url, stars, description, language}` | 初筛结论与项目详情左栏的仓库元数据 | 历史 run 无该元数据时安全降级，不额外请求 GitHub |
| `items[].chunks[]` 的 `chunk_id / text / line_start / line_end / byte_start / byte_end / methods / dense_distance / rrf_score` | 检索片段展示与审计 | `methods` 为空时显示「未记录检索方式」 |

### 4. 非阻塞的后续改进建议（当前前端已安全降级，不阻塞联调）

1. **详情页自包含（待后端接口支持）**：`deep-review` 报告目前不含语言 / Stars / 描述，项目详情需复用同 run 的初筛检索数据；若希望刷新后仅凭 deep-review 即可展示仓库元数据，可在报告 `repositories[]` 中新增可选的 `repository_metadata` 字段。前端已按「缺失即隐藏」处理，无需后端立即修改。
2. **排名报告补全时间**：建议 `ranking.scored_at` 恒定返回；当前缺失时前端回退 `report.created_at`，不会报错。
3. **引文坐标完整性**：建议所有 `citations` 恒含 `line_start/line_end/byte_start/byte_end`；缺失时前端无法提供可核对的行号，只能展示文本。
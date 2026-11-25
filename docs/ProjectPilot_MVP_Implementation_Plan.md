# ProjectPilot MVP 实施方案（MVP-FROZEN）

- 状态：`MVP-FROZEN`
- 冻结日期：2026-11-24
- 项目定位：课程竞赛项目、可实际演示的原型
- 本轮范围：只修改本方案文档；不修改代码、测试、数据库或现有业务逻辑
- 后续启动条件：用户明确批准后，从阶段一开始编码

## 0. 冻结目标

ProjectPilot 只保留一条完整业务闭环：

```text
PDF / Word / Text 上传
→ AI 提取需求
→ 需求确认
→ GitHub 搜索
→ README 采集
→ BGE-M3 + ChromaDB + BM25 混合检索
→ DeepSeek 按仓库深度核查
→ Python 校验引用真实性
→ 结构化项目评估报告
→ 前端展示结论和 README 原文证据
```

评委最终应能直接看到：

1. 项目是否符合需求；
2. 哪些需求得到 README 支持；
3. 哪些需求与 README 明确矛盾；
4. 哪些需求证据不足；
5. 每个判断对应的 README 原文、路径、行号和快照标识。

本方案不再扩展生产级基础设施，不再提出新的架构版本。后续只允许按三阶段实现和修复已批准范围内的缺陷。

## 1. 必须保留的已有成果

### 1.1 3a 主链路

以下功能已经完成，不推翻、不重写、不改变核心接口：

| 能力 | 现有实现 |
|---|---|
| PDF、Word、Text 需求提取 | `extraction.py`、`document.py` |
| 需求编辑、确认和修订 | `requirements.py`、`input_state.py` |
| SearchPlan | `search_plan.py`、`planner.py` |
| GitHub 搜索 | `search_execution.py`、`github_client.py` |
| README 快照采集 | `readme_persistence.py`、`search_persistence.py` |
| README 切片 | `chunking.py` |
| SiliconFlow BGE-M3 | `embedding_service.py`、`siliconflow_client.py` |
| ChromaDB | `chroma_store.py`、`chunk_index.py` |
| Dense + BM25 + Keyword 混合检索 | `retrieval.py` |
| EvidenceBundle v2 | `evidence_bundle.py` |
| 检索证据前端展示 | `frontend/src/SearchPanel.tsx` |
| 对象存储不可变写入和 SHA-256 校验 | `storage.py`、`search_persistence.py` |

### 1.2 第一阶段代码和测试

| 文件 | 处理 |
|---|---|
| `backend/app/services/requirement_quality.py` | 保留，继续做需求质量检查 |
| `backend/tests/test_requirement_quality.py` | 保留 |
| `backend/app/services/deep_review_logic.py` | 保留引用、归属、字节、行号、降级、prohibited 和 ALL/ANY 校验 |
| `backend/tests/test_deep_review_logic.py` | 保留，只随必要字段调整更新断言 |
| `backend/app/services/deep_review_models.py` | 保留严格 Pydantic 模型，只做实际接入所需的最小调整 |
| `backend/app/services/provider_execution.py` | 保留现状，不接入、不扩展 |
| `backend/tests/test_provider_execution.py` | 保留，不为简化而删除有效测试 |

其他已经存在的 3a、3b 测试和验证脚本全部保留。本 MVP 不安排大规模清理，不删除 `.qoder-credits`、旧 deep-check 脚本或其他未跟踪文件。

### 1.3 Provider 隐藏重试核查

已核查 `backend/app/services/extraction_provider.py`：

- `call_provider` 只发送一次 POST；
- 默认使用 `httpx.AsyncHTTPTransport(retries=0)`；
- 使用 `asyncio.timeout` 做整体超时；
- 没有 retry 循环；
- `ProviderError.public()` 明确返回 `retryable: false`。

因此深度核查可以直接复用 `call_provider`，不需要新增 Provider 执行框架，也不会产生隐藏重试。

信息保留核查结论：

- 原始 HTTP 状态码不单独保存，但已稳定归一化为 `ProviderError.code`：401、402、429、5xx 分别映射为 `PROVIDER_AUTH`、`PROVIDER_BALANCE`、`PROVIDER_RATE_LIMIT`、`PROVIDER_UNAVAILABLE`；
- 超时映射为 `PROVIDER_TIMEOUT`，连接失败映射为 `PROVIDER_NETWORK`；
- `ProviderError.usage` 会保留 200 响应中实际返回的 usage，包括输出截断等已取得 usage 的错误；
- HTTP 错误响应和超时没有可用 usage 时保存 `None`，新管线将其转换为 `null`；
- 深度核查直接读取 `exc.code` 和 `exc.usage`，不依赖 `public()` 的展示字段。

现有信息已经满足 MVP 的最小错误分类，本轮不修改 `extraction_provider.py`，也不新增 Provider 框架。

## 2. 最终技术架构

### 2.1 组件关系

```text
现有 SearchRun + EvidenceBundle v2
                ↓
        deep_review_pipeline.py
                ↓
 extraction_provider.call_provider
                ↓
        deep_review_logic.py
                ↓
         DeepReviewReport
                ↓
 deep_review_routes.py + Run Manifest
                ↓
        现有对象存储
                ↓
         前端报告展示
```

### 2.2 唯一 Operation 数据来源

正式深度核查的 Operation 只保存在 Run Manifest 中：

```json
{
  "deep_review_operation": {
    "operation_id": "uuid",
    "status": "running",
    "revision_id": "uuid",
    "revision_hash": "...",
    "bundle_hash": "...",
    "model": "deepseek-chat",
    "prompt_version": "deep-review-v1",
    "consent": true,
    "authorized_at": "...",
    "deadline": "...",
    "report_key": null,
    "report_hash": null,
    "error_code": null
  }
}
```

约束：

- 一个 SearchRun 只有一个 `deep_review_operation`；
- 不同时在 SearchRun/session 中保存第二份 Operation；
- 不增加多 Operation 历史映射；
- 不增加双重指纹；
- 不增加独立幂等索引；
- 不增加分布式锁、active lock 或多实例协调；
- 只使用现有 Manifest CAS 完成“创建一次”和“终态更新”；
- `deadline` 取授权时间加 `5 × extraction_timeout_seconds + 120 秒`，用于只读识别中断；
- 不扩展成通用状态管理框架。

### 2.3 重复点击与授权

- 用户必须在请求中明确传入 `consent: true`；
- Operation 创建成功即表示授权已记录；
- 重复点击直接读取 Manifest 中已有 Operation 并返回当前状态；
- 同一 SearchRun 不允许启动第二次正式深度核查；
- 如果重复请求携带不同修订，返回 409，不覆盖原 Operation；
- 失败、部分完成或未知后不自动重试；
- 需要重新核查时，创建新的 SearchRun 并重新授权。

MVP 不要求 `Idempotency-Key`，不保存幂等键哈希。并发重复点击由 Manifest 的单字段 CAS 自然收敛为同一个 Operation。

### 2.4 任务状态与需求判断分离

任务状态只使用：

| 状态 | 含义 |
|---|---|
| `running` | 后台执行中 |
| `completed` | 所有选中仓库均完成核查 |
| `partial` | 至少一个仓库完成，且至少一个仓库失败或跳过 |
| `failed` | 没有可用仓库结果，也不是未知结果 |
| `unknown` | 无法确认模型调用结果 |

顶层状态按以下优先级聚合：

1. 任意仓库为 `unknown`：顶层直接为 `unknown`，最终报告仍保留所有已完成仓库的完整结果；
2. 没有 `unknown` 且所有选中仓库为 `completed`：顶层为 `completed`；
3. 没有 `unknown`、至少一个仓库 `completed`，且存在 `failed` 或 `skipped`：顶层为 `partial`；
4. 没有 `unknown` 且没有任何 `completed`：顶层为 `failed`。

没有可用仓库在 POST 前置检查阶段拒绝，不进入状态聚合。

需求判断仍只使用：

- `supports`
- `contradicts`
- `insufficient`

重要规则：

- 需求结论为 `insufficient` 不会自动导致任务 `partial`；
- `partial` 只表示部分仓库没有完成，不表示需求部分满足；
- 仓库状态简化为 `completed | failed | unknown | skipped`；
- 不再使用 `ready`、`dispatched`、`recorded`、`outcome_unknown` 等旧状态；
- 服务中断后，GET 可把超过 deadline 的持久化 `running` 只读展示为 `unknown`，不回写 Manifest。

### 2.5 报告持久化

只保存一份最终报告：

```text
模型分析
→ Python 引用校验
→ 汇总所有仓库结果
→ 生成一份 DeepReviewReport JSON
→ 写入现有对象存储
→ 将 report_key、report_hash 和终态写入 Manifest
```

对象 key：

```text
deep-reviews/{operation_id}/report.json
```

复用现有 `_put_immutable` 和 `read_version`：

- 报告内容不可变；
- 保存 SHA-256；
- GET 读取时校验 hash；
- 不保存每个仓库的独立结果对象；
- 不保存 attempt 历史；
- 不做迟到响应审计；
- 不扫描对象存储；
- 不实现报告恢复或重放。

执行过程中仓库结果只保存在内存中。如果进程在最终报告写入前退出，该任务展示为 `unknown`，不自动恢复，也不自动重新调用模型。

### 2.6 后台执行

- 使用 FastAPI `BackgroundTasks`；
- 按 3a 检索顺序处理最多 5 个候选仓库；
- 每个仓库最多调用一次 DeepSeek；
- 不实现持久化任务队列；
- 不实现启动扫描、周期 Recovery Worker、多实例恢复或任务认领；
- 遇到 `unknown` 停止后续模型调用，剩余仓库标记 `skipped`；
- 401、402 等明确配置错误立即停止；
- 单个非法 JSON 可标记该仓库 `failed` 后继续，但仍不重试。

## 3. DeepSeek 调用与预算

### 3.1 固定预算

| 项目 | 上限 |
|---|---:|
| 候选仓库 | 5 |
| 每仓库模型调用 | 1 |
| 每仓库候选 Chunk | 16 |
| 输入 UTF-8 字节保守上界 | 24000 |
| 输出 Token | 2000 |
| 总模型调用 | 5 |

说明：

- 24000 是完整 Prompt 的 UTF-8 字节保守上界，不是真实 tokenizer 计数；
- 不得把字节上界显示为真实 Token 使用量；
- 实际 Token 使用量以模型响应 `usage` 为准；
- 超出预算时明确跳过并记录原因，不静默截断 Chunk 或原文；
- 不自动增加 GitHub、Embedding 或模型调用。

阶段一统一使用以下字节字段命名，不再使用 `MAX_INPUT_TOKENS`、`max_input_tokens`、`input_tokens_estimate` 或 `estimate_token_upper_bound`：

- `MAX_INPUT_BYTES = 24000`
- `BudgetLimits.max_input_bytes`
- `RepositoryInput.input_bytes_upper_bound`
- `estimate_input_bytes_upper_bound(...)`

输出预算仍使用 Token 语义：`MAX_OUTPUT_TOKENS = 2000`。真实 Token 只来自模型响应 `usage`。现有相关测试保留，只更新字段名和断言。

### 3.2 Provider 最小错误分类

| 情况 | 仓库状态 | 后续行为 |
|---|---|---|
| 401、402 | `failed` | 停止后续调用 |
| 429、5xx | `unknown` | 停止后续调用，不重试 |
| 超时、断网 | `unknown` | 停止后续调用，不重试 |
| 非法 JSON、结构不合法、输出截断 | `failed` | 不重试，可继续下一仓库 |
| 未知 usage | 不改变执行状态；请求结果未知时仍为 `unknown` | Token 字段保存 `null` |

`unknown` 表示无法确认模型是否完成，不能伪造成功或失败。汇总 Token 只累加已知 usage，未知调用数单独记录，不能把未知 Token 当作 0。

### 3.3 费用记录

- 记录真实调用次数；
- 记录每次调用的 `prompt_tokens`、`completion_tokens`、`total_tokens`；
- 未知值保存 `null`；
- 有用户批准的计价来源时，记录估算费用和价格来源；
- 没有计价来源时记录 `not_calculated`，不编造金额；
- 真实验收报告额外记录实际账单或用户确认的费用口径。

## 4. 模型输入、输出与 Python 校验

### 4.1 模型输入

每个仓库输入：

- 已确认需求项：ID、字段、原文、情态、ALL/ANY 和逻辑组；
- 仓库基础信息；
- EvidenceBundle v2 中当前仓库的去重候选 Chunk；
- Chunk ID、文本、行号和字节范围；
- 严格指令：不得编造需求 ID、Chunk ID、引用或逻辑组。

### 4.2 模型输出

模型只输出草稿：

```json
{
  "assessments": [
    {
      "requirement_item_id": "req-1",
      "verdict": "supports",
      "reason_code": "supported_by_citation",
      "citations": [
        {"chunk_id": "chunk-1", "line_start": 12, "line_end": 18}
      ]
    }
  ]
}
```

示例中的 `supported_by_citation` 属于现有 `REASON_CODES` 枚举，与 `RequirementReview.reason_code` 的 Pydantic `Literal` 一致。阶段一让 `DraftAssessment.reason_code` 复用同一枚举，不新增原因体系。模型不输出：

- 最终 quote；
- byte 坐标；
- 总体推荐分；
- 仓库排名分；
- 源码审计或运行验证结论。

### 4.3 Python 校验

继续使用 `deep_review_logic.py`：

1. 需求 ID 必须属于已确认修订；
2. Chunk 必须属于当前仓库和当前 Snapshot；
3. 行号必须在 Chunk 和完整 README 范围内；
4. UTF-8 字节范围必须合法；
5. quote 必须从规范化 README 原始字节重新切片；
6. `supports/contradicts` 必须至少有一个有效引用；
7. 引用全部无效时降级为 `insufficient + citation_rejected`；
8. prohibited 需求未在有效证据中提到时必须为 `insufficient`；
9. 模型遗漏需求由 Python 补为 `insufficient`；
10. 未知需求、重复 Chunk 和非法输出不能进入正式结论；
11. ALL/ANY 结果由 Python 真值表计算；
12. 字节预算只用于调用前保守检查，不冒充真实 Token。

## 5. API 设计

### 5.1 POST 启动深度核查

```http
POST /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
```

请求：

```json
{
  "revision_id": "uuid",
  "consent": true
}
```

行为：

- 首次请求：创建 Manifest Operation，注册 BackgroundTasks，返回 202；
- 重复点击：返回已有 Operation 当前状态，不再次注册后台任务；
- 修订不一致：返回 409；
- 未授权：返回 422；
- Bundle 未就绪或没有可用仓库：返回 409 或 422。

### 5.2 GET 读取深度核查

```http
GET /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
```

返回：

```json
{
  "operation_id": "uuid",
  "status": "running",
  "persisted_status": "running",
  "model": "deepseek-chat",
  "revision_hash": "...",
  "bundle_hash": "...",
  "report": null,
  "error_code": null
}
```

规则：

- GET 永远只读；
- 不触发模型调用；
- 不写 Manifest；
- 不恢复任务；
- 持久化 `running` 超过 deadline 时返回 `status: unknown` 和 `persisted_status: running`；
- 已有最终报告时返回完整报告；
- 不提供重试、重放、取消或恢复接口。

## 6. 报告与前端展示

### 6.1 报告内容

最终报告包含：

- Task、SearchRun、Operation、需求修订和 Bundle hash；
- 模型名和 Prompt 版本；
- 每个仓库的状态和错误；
- 每条需求的 `supports / contradicts / insufficient`；
- 判断原因；
- README 原文引用；
- README 路径、行号、字节范围；
- Snapshot ID、commit SHA、blob SHA、content hash；
- ALL/ANY 逻辑组结果；
- 模型调用次数和 Token usage；
- 简单费用字段：`cost_status`、`estimated_cost`、`currency`、`pricing_source`；
- 未配置计价来源时 `cost_status: not_calculated`，金额保存 `null`；
- README-only 声明。

### 6.2 前端必须展示

授权勾选文案固定为：

> 我已知悉：本次深度核查仅向 DeepSeek 发送已确认的需求条目、候选仓库基本信息、已保存的 README 检索片段及必要引用坐标；不会直接上传原始 PDF/Word 文件。系统将按仓库最多发起 5 次模型调用，用于判断需求是否获得 README 文档支持、存在矛盾或证据不足，并可能产生 API 费用。

页面必须支持：

1. 明确的深度核查授权；
2. 正在核查的状态；
3. 按仓库展示；
4. 按 `supports / contradicts / insufficient` 分类；
5. 每条需求的判断原因；
6. 点击展开 README 原文；
7. README 路径、行号和快照标识；
8. `failed / partial / unknown` 状态；
9. 模型调用次数和 Token 使用量。

固定声明：

> 本报告仅基于已保存的 README 文档证据，不代表源码审计、代码运行验证或部署可用性。

不增加复杂图表、排行系统或可视化大屏。证据展示优先。

## 7. 三阶段开发计划

### 阶段一：深度核查核心功能

#### 输入

- 已确认需求修订；
- EvidenceBundle v2；
- 当前仓库的原始 README 快照；
- 现有 `call_provider`；
- 现有 `deep_review_logic.py` 和报告模型。

#### 工作内容

1. 新增 `deep_review_pipeline.py`；
2. 从 Bundle 提取并去重当前仓库 Chunk；
3. 构造 DeepSeek Prompt；
4. 每仓库最多调用一次；
5. 解析模型草稿；
6. 调用 Python 引用校验；
7. 生成 `DeepReviewReport`；
8. 按第 3.1 节统一输入预算的字节字段命名，并同步更新现有测试断言；
9. 让 `DraftAssessment.reason_code` 复用现有 `REASON_CODES`；
10. 对任务状态和简单费用字段做最小必要调整；
11. 完成 MockTransport 测试。

#### 修改文件

- 新增：`backend/app/services/deep_review_pipeline.py`
- 新增：`backend/tests/test_deep_review_pipeline.py`
- 修改：`backend/app/services/deep_review_logic.py`
- 修改：`backend/app/services/deep_review_models.py`
- 修改：`backend/tests/test_deep_review_logic.py`
- 复用但不修改：`extraction_provider.py`、`evidence_bundle.py`、`search_persistence.py`

现有测试只调整受字段变更影响的断言，不删除有效测试。

#### 输出

- 可离线运行的深审核心服务；
- 严格结构化报告；
- Mock 测试结果。

#### 工作量

2～3 人日。

#### 验收标准

- 最多处理 5 个仓库；
- 每仓库最多一次 Provider 调用；
- 429、5xx、超时、断网不触发第二次调用；
- 未知 Token 为 `null`；
- 无效引用降级为 `insufficient`；
- prohibited 和 ALL/ANY 校验通过；
- 输入超预算时明确跳过，不截断；
- 输入预算字段全部使用字节命名，不显示为真实 Token；
- 模型和 Pydantic 的 `reason_code` 使用同一现有枚举；
- 不调用真实 API；
- 现有 3a 和第一阶段测试全部通过。

### 阶段二：前后端接入

#### 输入

- 阶段一核心服务；
- 现有 Run Manifest CAS；
- 现有对象存储；
- 现有前端搜索页面。

#### 工作内容

1. 新增 POST/GET 路由；
2. 在 Manifest 中创建 singular `deep_review_operation`；
3. 记录授权、修订、Bundle、模型和 deadline；
4. 重复点击返回已有任务；
5. 使用 BackgroundTasks 执行；
6. 汇总并保存唯一最终报告；
7. 保存报告 SHA-256；
8. 前端增加授权按钮和状态展示；
9. 保留现有检索证据展示，只增加逐仓库、逐需求的深审报告区域；
10. 完成前后端联调。

#### 修改文件

- 新增：`backend/app/services/deep_review_routes.py`
- 新增：`backend/tests/test_deep_review_routes.py`
- 修改：`backend/app/persistence/run_manifest.py`（只增加 singular 字段及校验，不改现有 3a 状态）
- 修改：`backend/app/main.py`（只注册新路由）
- 修改：`frontend/src/search.ts`
- 修改：`frontend/src/SearchPanel.tsx`
- 修改：`frontend/search.test.mjs`
- 必要时修改：`frontend/browser-e2e-screenshot.mjs`

不修改 `search_routes.py` 的现有 3a 路由，不删除旧 deep-check 代码，不新增数据库表。

#### 输出

- 可操作的 POST/GET API；
- Manifest 中的唯一 Operation；
- 对象存储中的唯一最终报告；
- 可演示的前端报告页面。

#### 工作量

2～3 人日。

#### 验收标准

- 首次 POST 返回 202，重复 POST 返回已有状态；
- 同一 SearchRun 不能创建第二个 Operation；
- Operation 不同时存在于 session 和 Manifest；
- 报告不可变且 SHA-256 校验通过；
- GET 不触发模型调用；
- 过期 `running` 展示为 `unknown`；
- 任意仓库 `unknown` 时顶层为 `unknown`，且已完成仓库结果保留在报告中；
- 前端授权文案明确数据范围、调用目的、最多 5 次调用和可能费用，并说明不直接上传原始 PDF/Word；
- 前端能展示支持、矛盾、证据不足、原文、路径和行号；
- 前端显示调用次数和 Token；
- `failed / partial / unknown` 提示明确；
- 后端测试和前端构建通过；
- 不调用真实 API。

### 阶段三：真实验收和比赛演示

#### 输入

- 阶段二完成的系统；
- 已有稳定 SearchRun 和 EvidenceBundle v2；
- 用户单独授权。

#### 工作内容

1. 新增 `backend/validate_deep_review_live.py`；
2. 明确记录授权范围、预算和调用上限；
3. 优先复用已有 SearchRun 和 Bundle；
4. 执行真实 DeepSeek 调用；
5. 核对真实引用可以从原文复现；
6. 记录真实 Token 和费用状态；
7. 完成完整浏览器演示；
8. 如实区分 Mock 测试和真实验收。

#### 修改文件

- 新增：`backend/validate_deep_review_live.py`
- 除非发现缺陷，否则不修改业务代码
- 不新增数据库表
- 不扩展架构

#### 输出

- 一份真实 DeepReviewReport；
- 真实调用和费用记录；
- 完整浏览器演示结果；
- 验收结论和限制。

#### 工作量

1～1.5 人日，另加真实 API 等待时间。

#### 验收标准

- 至少一个真实报告成功生成；
- DeepSeek 调用次数不超过 5；
- 每条正式支持/矛盾都有可复现的 README 引用；
- Token 来自真实响应，未知值保持 `null`；
- 记录实际调用次数和费用状态；
- 刷新、重复点击和服务重启不会自动重复调用；
- 上传到报告的完整流程可演示；
- 不用 Mock 结果冒充真实验收。

## 8. 测试与验收边界

### 8.1 离线阶段

阶段一、二只允许 MockTransport 和本地测试：

```bash
cd backend
pytest
```

```bash
cd frontend
npm run build
```

必须覆盖：

- 3a 回归；
- 引用真实性；
- Chunk/Snapshot 归属；
- UTF-8 字节和行号；
- 无有效引用降级；
- prohibited；
- ALL/ANY；
- 每仓库一次调用；
- 无隐藏重试；
- 重复点击；
- 未知 usage；
- 最终报告不可变；
- GET 只读。

### 8.2 真实阶段

阶段三必须单独授权，并记录：

- 授权时间和范围；
- 模型名；
- 实际调用次数；
- Token usage；
- 费用状态和计价来源；
- 是否复用已有 Bundle；
- 真实验收结论。

未获得单独授权，不调用真实 DeepSeek、GitHub 或 Embedding API，不产生真实费用。

## 9. 明确不做的内容

- 不实现持久化任务队列；
- 不实现启动扫描或周期 Recovery Worker；
- 不实现多实例任务恢复；
- 不实现分布式锁；
- 不实现多 Operation 历史；
- 不实现双重指纹或独立幂等索引；
- 不实现每仓库独立结果对象；
- 不实现迟到响应审计；
- 不实现对象存储全量扫描；
- 不实现报告重放；
- 不实现仓库级重试资格；
- 不接入 `provider_execution.py`；
- 不删除现有有效测试；
- 不修改六张核心数据库表；
- 不重构 3a；
- 不新增无业务价值的服务层。

## 10. 技术风险

| 风险 | 处理 |
|---|---|
| DeepSeek 返回非法 JSON 或截断 | 标记仓库 `failed`，不重试，不使用不完整结果 |
| 超时或断网导致调用结果未知 | 标记 `unknown`，停止后续调用，不自动重放 |
| 模型编造引用 | Python 从原始 README 字节重新切片校验 |
| 24000 字节上界误显示为 Token | 明确命名为字节上界，真实 Token 只来自 usage |
| 进程在最终报告前退出 | 任务展示 `unknown`，不恢复，不重复调用 |
| Manifest 终态 CAS 失败 | 任务保持 `running`，超时后只读展示 `unknown`，不自动修复 |
| 候选仓库质量影响演示 | 阶段三优先选择已有稳定 SearchRun/Bundle |
| Token 或费用信息缺失 | 未知值保存 `null`，费用标记 `not_calculated` |
| 旧 deep-check 与新路径并存 | 新前端只使用新路径，本期不清理旧代码 |
| 误把 README 结论当作源码结论 | 页面和报告固定 README-only 声明 |

## 11. Git 与工作区安全

- 当前工作区已有 3a 的未提交修改和未跟踪文件，全部保留；
- 本方案只修改 `docs/ProjectPilot_MVP_Implementation_Plan.md`；
- `docs/ProjectPilot_Deep_Review_Implementation_Plan.md` 保持 v5.2.2 历史版本，SHA-256 为 `3943c363aa52ed76c2ddc537c1aa041854ca8723a59ce517109d5da7aa934dc1`，不再扩展；
- 不使用 `git reset --hard`、`git clean`、`git checkout --`；
- 不覆盖、不回退、不清理已有未提交改动；
- 每个编码阶段开始前重新检查 `git status --short`；
- 提交和推送仍需用户单独授权。

## 12. 冻结声明

本方案标记为 `MVP-FROZEN`。审核通过后只按三个阶段实施：

1. 深度核查核心功能；
2. 前后端接入；
3. 真实验收和比赛演示。

不再增加架构升级、恢复系统、分布式机制、复杂状态机或额外业务功能。用户明确批准前，不修改代码，不进入阶段一。

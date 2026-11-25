# ProjectPilot SiliconFlow Embedding + ChromaDB 实施方案

- 状态：已确认（2026-11-23 用户审核通过；本轮开始按阶段实施）
- 上位依据：`ProjectPilot_项目任务书.md` 第 12 章、`docs/ProjectPilot_Implementation_Plan.md` §5.2–§5.7
- 本文件为 3a 阶段的唯一实施依据；3b（把深度核对改为按块输入）不在本阶段

---

## 0. 本轮修订清单（相对上一版，共 11 项）

| # | 修订 | 结论 |
|---|---|---|
| 1 | API 域名 | 默认 `https://api.siliconflow.cn/v1`；`cloud.siliconflow.cn` 只是平台页面；保留 `SILICONFLOW_BASE_URL` 覆盖 |
| 2 | 模型预检 | 继续 `BAAI/bge-m3`、1024 维；**不传 `dimensions`**（仅 Qwen3 支持）；预检须列模型、真实调用一次、验维度/数值/用量 |
| 3 | Chroma 生产形态 | 开发 `PersistentClient`；生产独立 Chroma Server + 持久卷 + 内网 + token 认证；平台不支持时停止并提出托管向量库备选 |
| 4 | 同意文案 | 采用用户给定文案，并入现有“搜索授权”一处，不新增第二个同意入口 |
| 5 | 3b | 本阶段不做；保留 `deep-check-v1` 与历史报告 |
| 6 | 检索配置 | Dense Top-10、BM25 Top-10、RRF k=60、最终 Top-5；Cosine 0.5 不作硬过滤；记录的是 distance 不是百分比 |
| 7 | Chroma 元数据 | 删除 `run_id`；保留 `repository_id/snapshot_id/chunk_id/commit_sha/readme_path/chunk_index` 等 |
| 8 | 失败恢复 | 崩溃保持 `embedding` 续跑；重试耗尽 → 当前 operation `failed` 终态；再次发起新建 `operation_id`，复用缓存与已写向量 |
| 9 | Top-K | 明确 Top-10 是单路召回数，Top-5 是融合后最终证据数 |
| 10 | 重试成本 | 每次请求尝试计入预算；有限重试；缓存成功向量；不承诺 exactly-once；Chroma 用 `chunk_id` 幂等 |
| 11 | Token 预算 | 统一口径：batch 16、最多 32 批、最多 512 块/次、262,144 token；无效上限见 §6.4，超限显式失败 |

---

## 1. 结论与边界

1. 选型：SiliconFlow 托管 API + `BAAI/bge-m3` + 1024 维；向量库 ChromaDB；LLM 仍为 DeepSeek Flash。
2. Embedding 只负责“找相关片段”：不判断需求是否满足、不生成推荐解释、不进入推荐总分。
3. 不新增 MySQL 表，不改六张表结构，不新增 `usage_type`；索引状态写 Run Manifest，块正文留私有对象存储，向量进 Chroma。
4. 现有搜索、证据、深度核对逻辑与 Schema 全部保持不变。

---

## 2. 配置与启动校验

### 2.1 `SiliconFlowSettings`（`backend/app/core/settings.py`）

| 字段 | 类型/默认 | 环境变量 |
|---|---|---|
| `siliconflow_api_key` | `SecretStr`，必填 | `SILICONFLOW_API_KEY` |
| `siliconflow_base_url` | `str`，默认 `https://api.siliconflow.cn/v1` | `SILICONFLOW_BASE_URL` |
| `embedding_provider` | `Literal['siliconflow']` | `EMBEDDING_PROVIDER` |
| `embedding_model` | `Literal['BAAI/bge-m3']` | `EMBEDDING_MODEL` |
| `embedding_dimension` | `Literal[1024]` | `EMBEDDING_DIMENSION` |
| `embedding_batch_size` | `int`，默认 16，范围 1–32 | `EMBEDDING_BATCH_SIZE` |
| `embedding_max_batches` | `int`，默认 32 | `EMBEDDING_MAX_BATCHES` |
| `embedding_max_chunks_per_run` | `int`，默认 512 | `EMBEDDING_MAX_CHUNKS_PER_RUN` |
| `embedding_max_tokens_per_run` | `int`，默认 262144 | `EMBEDDING_MAX_TOKENS_PER_RUN` |
| `embedding_timeout_seconds` | `float`，默认 30 | `EMBEDDING_TIMEOUT_SECONDS` |
| `embedding_max_retries` | `int`，默认 1（0–2） | `EMBEDDING_MAX_RETRIES` |
| `embedding_enabled` | `bool`，默认 `true` | `EMBEDDING_ENABLED` |

- 与现有 `ExtractionSettings` 同风格：`env_file=PROJECT_ROOT/.env`、`extra='ignore'`、`hide_input_in_errors=True`。
- `.env.example` 下一轮补模板，只写变量名和空值；真实密钥只在 `.env`（已被 `.gitignore` 忽略）。
- 启动校验：`create_app()` 调用 `validate_embedding_config()`；`embedding_enabled=true` 且密钥为空 → 抛明确错误，只点名变量，不回显值。离线测试在 `conftest` 设 `EMBEDDING_ENABLED=false` 并注入假密钥，不降低生产约束。
- 代码中只在拼 `Authorization` 头时取一次密钥；日志只记 `model/dimension/batch/status/usage`，不记请求头、密钥、上游完整响应。

### 2.2 同意文案（并入现有搜索授权）

搜索面板现有唯一搜索同意处追加：

> 为完成项目需求与 GitHub 项目的语义匹配，系统将把必要的需求文本及候选项目 README 文本片段发送至 SiliconFlow Embedding API 进行向量化处理。系统不会将原始 PDF 或 Word 文件直接发送给 Embedding 服务。请确认同意后继续。

深度核对保留其独立同意，不改文案；Embedding 不新增第三个勾选。

---

## 3. SiliconFlow API Client

依据官方文档（`docs.siliconflow.com`，域名差异见 §0-1）：

- `POST {base_url}/embeddings`，`Authorization: Bearer <key>`，`Content-Type: application/json`。
- 请求：`model`、`input`（字符串或数组）、`encoding_format: "float"`；**不发 `dimensions`**。
- 批量：数组最多 32 条；`BAAI/bge-m3` 单条上限 8192 token；本项目块控制在 512 token 以内（策略值，不是 API 硬限）。
- 响应：`object="list"`、`model`、`data[{object:"embedding", embedding:[...], index}]`、`usage{prompt_tokens, completion_tokens, total_tokens}`。
- 限流：账户级；向量模型 RPM 2000–10000、TPM 50 万–1000 万；429 消息标明 RPM/TPM 等类别；无 `Retry-After` 文档。
- 错误：400/401/403/429/503/504；401/403 与参数错误不重试。

`backend/app/services/siliconflow_client.py`：

```python
class EmbeddingClientError(Exception):
    code: str            # EMBEDDING_AUTH / EMBEDDING_RATE_LIMIT / EMBEDDING_UNAVAILABLE /
                         # EMBEDDING_TIMEOUT / EMBEDDING_HTTP_ERROR / EMBEDDING_INVALID_RESPONSE /
                         # EMBEDDING_RESPONSE_LIMIT
    retryable: bool
    usage: dict | None
    attempts: int

async def embed(texts: list[str]) -> tuple[list[list[float]], dict, int]
```

- 传输：`httpx.AsyncClient(timeout=..., follow_redirects=False, trust_env=False, retries=0)`；响应读取上限 8 MiB；`stream` 防超限。
- 校验：状态码 → 严格 JSON（拒绝重复键/NaN）→ `object=="list"` → 条数相等 → `index` 是 0..n-1 排列 → 每条 1024 维、全部有限、非全零；空向量/维度错误一律 `EMBEDDING_INVALID_RESPONSE`，不落库。
- 重试：仅 429/503/504/超时/网络错误；最多 `embedding_max_retries` 次；退避 0.5s（有 `Retry-After` 取 `min(值,5s)`）；**每次尝试都计入预算**；401/400 不重试。
- 不承诺 exactly-once：超时可能已在服务端完成；客户端只复用“已拿到响应”的结果，缓存与 `chunk_id` 保证重复写入幂等。
- 密钥/上游消息只写短摘要，不进错误响应、不进日志。

---

## 4. EmbeddingService

`backend/app/services/embedding_service.py`：

```python
@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str; dimension: int; version: str
    usage: dict; cache_hits: int; attempts: int
```

- 输入：去重后的文本列表；输出：与输入等长、顺序一致、1024 维。
- 批内去重，跨运行缓存：键 `sha256(model|version|preprocess_version|text)`，值存 `embedding-cache/{model_slug}/{hash}.json`（不可变）；命中不调用 API、不计 token；缓存损坏视为未命中重算。
- 预算预扣与结算：调用前按“批数 × 16 + token 估算”检查；结束按响应 `usage` 结算；超限抛 `EMBEDDING_BUDGET`，**不跳过任何块**。
- 任一批重试后仍失败 → 整个索引操作 `failed`；已成功批次的向量与缓存保留。
- 同步入口 `embed_sync()` 供现有同步流水线/脚本使用（内部 `anyio.run`）。

---

## 5. ChromaDB

`backend/app/services/chroma_store.py`：

- 客户端：开发 `chromadb.PersistentClient(path=data/chroma, settings=Settings(anonymized_telemetry=False))`；生产 `HttpClient`（内网地址、`ssl`、token 头）。
- 集合：`pp_{model_slug}_{dim}_{preprocess_version}`（如 `pp_bge_m3_1024_v1`），`metadata={"hnsw:space":"cosine"}`；显式传向量，禁止默认 Embedding。
- 记录：`id=chunk_id`；`metadatas=[{repository_id, snapshot_id, chunk_id, commit_sha, readme_path, chunk_index, heading_path, line_start, line_end, byte_start, byte_end, content_hash, model_version}]`；**无 `run_id`**；`documents=None`。
- 查询：`query(query_embeddings=[...], n_results=10, where={"$and":[{"snapshot_id":sid},{"repository_id":rid}]}, include=["metadatas","distances","ids"])`。
- **返回的是 distance（余弦距离，越小越近），不是相似度百分比**；`score` 字段记为 `cosine_distance`，方向 `lower_is_closer`；不做 0.5 硬过滤。
- 写入校验：按 `snapshot_id` 统计 count，抽样按 id 回读 `content_hash`；不一致即 `failed`，不标 ready。
- 重复 upsert 幂等；换模型/维度/预处理即换集合，旧集合不删。

### 5.1 生产认证与部署

- Server 端：
  - `CHROMA_SERVER_AUTHN_PROVIDER=chromadb.auth.token_authn.TokenAuthenticationServerProvider`
  - `CHROMA_SERVER_AUTHN_CREDENTIALS=<随机长 token>`（只放服务器环境变量/Secret）
- Client 端：`chromadb.HttpClient(host=..., port=..., ssl=..., headers={"Authorization": f"Bearer {token}"})`；或 `Settings(chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider", chroma_client_auth_credentials=token)`。
- 网络：Chroma 只监听内网/私网，不发布公网端口；FastAPI 与 Chroma 同 VPC/容器网络。
- 持久化：独立卷挂载 `/data`（`chroma run --path /data/chroma`），备份与对象存储同一策略。
- **平台确认**：当前尚未确定后端部署平台；实施到部署步骤时必须确认其支持“持久卷 + 私有网络 + Secret 注入”。若不支持，**停止并提交备选方案**（托管向量库，如 Zilliz Cloud/阿里云 DashVector/腾讯云 VectorDB），不静默降级为公网暴露或嵌入式单机。

---

## 6. 分块、索引与恢复

### 6.1 ReadmeChunk

```json
{
  "chunk_id": "<snapshot_id>:<ordinal>:<sha256(text)[:16]>",
  "snapshot_id": "...", "repository_id": "123",
  "ordinal": 7, "heading_path": ["## Usage", "### Export"],
  "text": "...", "content_hash": "sha256(text)",
  "byte_start": 1234, "byte_end": 5678,
  "line_start": 40, "line_end": 61,
  "token_estimate": 480, "split": "block|lines|chars"
}
```

- 基于 LF 归一化文本；入库前断言 `normalized[byte_start:byte_end].decode() == text`。
- 512 token / overlap 64；代码块、表格、列表不硬切；超长块按行再切，超长行按字符切并标记。
- 每仓库 100 块；每次运行结构上限 1000 块（任务书口径），但 **Embedding 有效容量见 §6.4**；超出容量明确失败，不静默跳过。
- 空块/纯空白/超长单行跳过并计数。

### 6.2 索引状态（Run Manifest + 独立进度对象）

Manifest 中新增 `chunk_index_operation`（只记引用与状态，保持 Manifest 体积可控）：

```
operation_id / version='chunk-index-v1'
index_key                     # uuid5(run_id, index_config_hash)，跨重试稳定
model / dimension / preprocess_version / config_hash
status: pending | embedding | ready | failed
chunk_manifest_key / chunk_manifest_hash
vectors_key                   # 可选：向量快照（可选实现）
usage: {snapshot_id: bool}
failed_operation_ids: [...]   # 历史失败尝试
error_code
created_at / updated_at
```

独立进度对象 `chunk-index/{index_key}/progress.json`（CAS 写，内容不可变式追加）：

```
completed_batch_ids: [...]
batch_status: {batch_id: {input_hash, state: done|failed, attempts, usage}}
usage: {batch_id: {...}}
```

- 崩溃：`status=embedding` 保留；恢复读进度，只跑未完成批次；已写向量按 `chunk_id` upsert 幂等。
- 重试耗尽：当前 operation `failed`（终态，记录 error_code）；再次发起生成**新** `operation_id`，同一 `index_key`，复用缓存/已写向量/已完成批次；旧 operation 记入 `failed_operation_ids`。
- 不变量：`pending→embedding→ready|failed` 单调；`ready` 不可重开；`ready` 必须有块清单哈希、count 校验与每快照 `content_hash` 一致；CAS 回调内不做外部调用。
- 触发：搜索流水线 README 全部终态后（本轮先由显式脚本执行，HTTP 触发与 UI 留到 3a-UI）。

### 6.3 批处理

- 每批最多 16 条；按 token 估算再切分；批 ID = `sha256(batch_texts 顺序哈希)[:16]`；`batch_input_hash` 用于恢复校验。
- 单批超时 30s；失败重试 1 次；429 退避。

### 6.4 预算统一口径（有效上限）

| 限制 | 值 | 含义 |
|---|---|---|
| `embedding_batch_size` | 16 | 每批条数 |
| `embedding_max_batches` | 32 | 每次尝试最多批数 |
| 每仓库块数 | 100 | 结构限制 |
| 每次运行块数（结构） | 1000 | 任务书上限 |
| **每次运行块数（Embedding 有效）** | **min(512, 16×32=512, floor(262144/平均估算 tokens))** | 实际能处理的上限 |
| `embedding_max_tokens_per_run` | 262144 | 硬预算；按响应 usage 结算 |

- 无缓存时最多 **512 块**；5 个仓库 × 100 = 500，已知场景可完整覆盖。
- 若块数或 token 超预算：**显式失败**（`EMBEDDING_CAPACITY` / `EMBEDDING_BUDGET`），不生成 partial ready、不静默丢块。
- 40 批可处理 640 块的旧说法作废：本次固定 32 批 / 512 块。

---

## 7. 检索：Dense + BM25 + RRF

`backend/app/services/retrieval.py`：

1. **Dense**：需求项文本 → Embedding → Chroma 查询（`snapshot_id+repository_id` 过滤）→ **Top-10**；记录 `cosine_distance`。
2. **BM25**：同一快照块清单纯 Python BM25（`k1=1.2,b=0.75`）；分词 = ASCII 单词 + CJK 二元组；→ **Top-10**。
3. **关键词**：现有 `_mentions` 行级命中作为第三路（保留可解释性）。
4. **RRF**：`score = Σ 1/(60+rank)`，排名从 1 开始；不混不同量纲。
5. **最终**：融合后取 **Top-5** 组成 `EvidenceBundle`；每条含 `requirement_item_id/chunk_id/snapshot_id/repository_id/source_location/quote/retrieval_method/score/score_direction`；无召回记录范围与原因。
6. 约束：不跨仓库/快照/任务；相似度只表示相关，不是满足证据，不进总分。

仓库级“语义匹配”只展示“有相关块覆盖的硬性需求数 / 总数”，阈值配置化，不叫满足率。

---

## 8. 与现有系统关系

- 不新增 MySQL 表；`search_snapshots.usage_type` 不变；`repository_snapshots` 仍是快照身份唯一来源。
- `run_manifest.py` 增加 `chunk_index_operation` 校验与不变量，等级与 `deep_check_operation` 相同；旧 Manifest（无该字段）继续合法。
- 3a 只新增索引/检索能力与只读输出；不改 `deep-check-v1`、不改推荐标签规则、不改 Requirement JSON Schema。
- 3b（按检索块做深度核对）单独立项，等 3a 验收后再定。

---

## 9. 验收标准

离线：

1. 分块：标题/段落/代码块/表格边界、否定词保留、偏移与行号回查、100/512/1000 上限与 truncated 标记。
2. Client：批量切分、index 乱序回填、1024/有限/非零校验、429/5xx/超时重试次数、401/400 不重试、响应上限、密钥不落日志。
3. Service：去重、缓存、预算预扣/结算、失败不产生 ready。
4. Chroma：集合隔离、where 过滤、跨快照不串、upsert 幂等、distance 语义、count/哈希校验。
5. 索引：状态单调、崩溃续跑、失败后新 operation 复用缓存、Manifest 不变量、GET 只读。
6. 检索：Top-10/Top-5 定义、RRF 不混量纲、无召回有原因、证据可回查。
7. 回归：现有 869 项测试全绿；前端构建与浏览器检查不退化。

真实（最小成本）：

8. 预检：`GET {base}/models` 确认模型可见；`embed(["你好"])` 确认 1024 维、有限、非零；记录 usage；不传 `dimensions`。
9. 对现有 5 份 README 建索引并检索 5 条需求：确认能命中已知证据（gemini `.txt` 下载行、rescript per-word timestamps），记录 distance 与块数。

---

## 10. 实施顺序

1. 配置与分块 → 2. SiliconFlow Client → 3. EmbeddingService → 4. ChromaDB → 5. Chunk Index → 6. Dense+BM25+RRF → 7. EvidenceBundle → 8. 最小真实预检 → 9. 真实索引/检索验收。

每步先写离线测试，测试通过才进入下一步；未验证不标记完成。不执行数据库迁移，不修改六张 MySQL 表。

---

## 11. 3a-UI 实施记录（2026-11-23）

### 11.1 流水线接入

- `search_routes.pipeline`：README 全部终态并发布字面证据后，依次执行 `ChunkIndexer.index` → `EvidenceBundleService.publish`；两步均单独 try/except，失败只写入该阶段状态，**不回滚**已发布的搜索、字面证据与深度核对，也不重调 GitHub/LLM。
- `BackendFactory.open` 在搜索运行（budget 非空）时构造 EmbeddingService / ChromaStore / ChunkIndexer / EvidenceBundleService；`EMBEDDING_ENABLED=false` 时跳过检索阶段。
- 已有快照直接复用；向量缓存命中不发请求；索引 ready 时 `indexer.index` 仅校验并返回。

### 11.2 EvidenceBundle 持久化

对象：`retrieval-bundles/<operation_id>.json`（不可变）。Manifest 新增：

```
evidence_bundle_operation = {
  operation_id, version: 'evidence-bundle-v1', revision_hash, plan_hash,
  chunk_index_key, model, dimension, config_hash,
  storage_key, bundle_hash, status: ready|partial|failed, error_code,
  created_at, updated_at
}
```

Bundle 结构（前端展示与 API 共用）：

```
version, owner, task_id, run_id, confirmed_revision_id, revision_hash, plan_hash,
model, dimension, config {dense_top,bm25_top,final_top,rrf_k}, warning, notice,
created_at, status, empty_reason, query_usage {tokens,attempts},
requirements [ {requirement_item_id, field, text, modality, operator, logic_group_id} ],
candidates [ {
  repository_id, snapshot_id, readme_path, coverage,
  repository {full_name, html_url, stars, description, language},
  status: ok|partial|failed, error_code,
  items [ {requirement_item_id, text, status: found|empty, empty_reason,
           chunks [ {chunk_id, text, line_start, line_end, byte_start, byte_end,
                     methods, ranks, dense_distance, rrf_score, score_direction} ]} ] } ]
```

- 每条证据绑定 requirement_item_id / repository_id / snapshot_id / chunk_id，正文来自已保存 README。
- 无召回写 `empty_reason`；Dense 失败降级为 BM25/关键词并标 `partial`；全部无召回标 `failed`。
- 不新增 MySQL 表；检索状态只进入 Run Manifest。

### 11.3 只读 API

`GET /api/v1/tasks/{task_id}/search-runs/{run_id}/retrieval-evidence`

- 可选查询参数：`repository_id`、`requirement_item_id`（可同时使用），返回同一响应结构并附 `filters`。
- 纯读：校验归属/绑定/Manifest/对象哈希，**不调用 SiliconFlow、不执行 Chroma 查询、不重新生成 Bundle**。
- 状态：不存在运行 404；未生成/未就绪 409 `RETRIEVAL_NOT_READY`；生成失败 409 `RETRIEVAL_FAILED`；过滤项不存在 404 `RETRIEVAL_FILTER_NOT_FOUND`。
- `run_view` 增加 `retrieval_status` / `retrieval_error_code`。

### 11.4 前端

- `SearchPanel` 新增「05 / 检索证据（README 片段）」：仓库名称/链接/Stars/简介；按需求字段分组；展开显示 README 路径、行号、字节区间、检索方式、余弦距离（越小越近）与 RRF 分数；明确标注“相关不代表满足”，不显示任何百分比。
- 状态：找到相关片段 / 尚未找到 / 部分完成 / 检索失败 / 覆盖不完整 / 未生成；失败不遮挡其他结果。
- 仅当 `retrieval_status` 为 `ready` 或 `partial` 时发起一次 GET；页面刷新后从已保存 Bundle 重新读取。

### 11.5 验收结果

- 后端 **928 项测试通过**（新增 6 项：partial/failed 状态、Bundle 过滤视图、Manifest 不变量）。
- 真实发布：已有运行（5 份 README、72 块）发布 Bundle `ready`，5 个仓库全部 `ok`，查询 embedding **0 新增 token**（全部命中缓存），存储回读一致。
- 前端：`npm run build` 通过；`search.test.mjs` 13 项、`requirements.test.mjs` 13 项通过；隔离浏览器新增 17 项通过（原文/行号/方式/距离/无百分比/部分与失败状态），原有 37 项通过，外部请求 0。

### 11.6 已知问题

1. 现有那次真实 Bundle 发布于 `readme_path` 字段加入之前，页面回退显示 `README.md`；新运行会带真实路径。
2. GET 路由本身未做真实 HTTP 调用（旧会话令牌不在服务端），已用服务层 + 视图函数 + 离线测试覆盖；下一次真实搜索会自然走完整路由。
3. 流水线自动检索尚未在真实搜索中重跑（需模型/GitHub 授权）；已完成服务级真实发布。
4. 生产 Chroma 认证/持久卷仍未部署验证。

### 11.7 下一阶段建议

- 3b（深度核对改用检索块）单独确认；或先做一次真实搜索运行，验证流水线自动生成 Bundle 与页面展示。

---

### 11.8 最终真实端到端验收（2026-11-23）

真实账号、真实 DeepSeek / GitHub / SiliconFlow，上传 PDF 后完整走通：

| 项目 | 结果 |
|---|---|
| run_id | `d198dbe9-583f-4764-9d80-d02cc9fdfcaa` |
| task / revision / document | `da3a2961…` / `195f4d2b…` / `05d4a5c5…` |
| PDF | 1 页、56 字符、无警告 |
| 需求提取 | 3 条，usage 3,387 tokens（prompt 2,814，其中缓存 2,560；completion 573） |
| 确认 | 无不确定信息，一次确认成功 |
| 搜索 | 5 个候选、5 份 README 全部完成；`search_status=partial`（PAGE_BUDGET）；GitHub 请求 12 次 |
| 规划 | usage 2,786 tokens |
| Chunk | **31** 块（7/3/6/10/5），全部 `full` |
| Embedding | 2 批，**3,228 tokens**；查询 21 tokens |
| Bundle | `ready`，5 个仓库，**69** 条证据坐标全部按原始快照复核通过 |
| 路径 | 5 份快照均保存真实 `readme_path` |
| Manifest | chunk `ready`、bundle `ready`，bundle 文件哈希与记录一致 |

HTTP 权限（真实请求，A/B 两会话）：A 读自己的运行与 Bundle 200；B 读 A 的运行/Bundle 均 404；B 在自建任务下复用 A 的 run_id 404；错误 run_id 404；匿名 401；`repository_id` / `requirement_item_id` 过滤均只返回本运行数据，未知过滤值 404。11 项全部通过。

页面：真实浏览器打开该运行、展开证据后截图（`data/acceptance/3a-ui/retrieval-evidence-full.png`、`retrieval-evidence-section.png`），显示仓库名/Stars/语言、README 路径与行号、检索方式、余弦距离（越小越近）、RRF；无满足百分比。历史 Bundle 缺少路径时显示“历史记录未保存文件路径”，不再伪造 `README.md`。

异常场景（离线故障注入，全部通过）：README 404 不造假快照；GitHub 限流/认证失败停止派发；Embedding 429/超时有限重试；Dense 失败降级为 BM25/关键词并标 `partial`；全部无召回标 `failed`；无召回写 `empty_reason`；索引崩溃后批次进度恢复；页面刷新从已存 Bundle 重读。

规模分析：本次候选 5、README 5、Chunk 31，远低于 512 上限；当前 `readme_candidates=5` 时理论最坏 500 块，仍不触顶。若把候选扩到 10，最坏 1,000 块会触发 `EMBEDDING_CAPACITY`；届时应先做规则评分 Top-N（如 6–8）再向量化，或经批准提高预算。本阶段未修改任何候选筛选规则。

---

### 11.9 3a 审核修正记录（2026-11-23）

**证据语义**：检索条目状态只保留 `retrieved`（已召回候选片段）/ `empty`（未召回），新增 `relevance: unverified`；前端改为“已召回候选片段（相关性待核查）”，禁止项追加“未提及不代表符合禁止条件”。不新增 LLM 调用。

**Bundle v2**：新写入 `evidence-bundle-v2`、`schema_version: 2`，候选级增加 `commit_sha / blob_sha / content_hash / collected_at / readme_path`。字段由 `snapshot_traces()` 从 Manifest README 操作 + MySQL `repository_snapshots` 双源读取并逐项核对，任一不一致即 `SearchIntegrityError`。Manifest 同时接受 v1/v2；历史 v1 Bundle 不重写，API 返回 `schema_version: 1`，页面显示“历史记录未保存完整快照标识/文件路径”。

**唯一 Chunk 展示**：前端按 `chunk_id` 合并重复片段，展示关联的全部需求；每条需求仍保留独立召回状态与片段数；底层 69 条关联数据不变。

**查询去重**：执行层按“最终 q + per_page”等有效参数判重，不再只比较 query_text；每个原始 group_id/query_id 作为 alias 保留，命中记录为每个 alias 各写一条。Manifest 仍保留别名集合。

**锁核查结论**：LocalStorage 为跨进程 `FileLock` + 原子替换 CAS；Manifest 为 64 次 CAS 重试；Bundle/索引以不可变对象 + 条件 Manifest 更新，冲突显式失败；`search_persistence._lock` 仅进程内串行化，跨进程可能重复一次 Embedding 调用（成本风险，无数据覆盖）。多进程并发索引与重复计费规避列为生产部署前专项验收。

**测试**：后端 941 项全过（新增 v1/v2 Bundle、追溯字段、查询去重、状态语义）；前端构建通过；单测 13+13；浏览器 37+27 项全过，外部请求 0。

**真实数据**：v1 Bundle 经 API 回读正常（`schema_version:1`，缺失字段按历史处理）；v2 追溯提取对真实运行 5 个仓库逐一验证，commit/blob/content_hash/collected_at/readme_path 与 Manifest 及 MySQL 快照完全一致。v2 真实 Bundle 将在下一次真实搜索运行时写入。

---

### 11.10 收尾验收（2026-11-23）

- **Bundle v2 离线端到端 Fixture**：`backend/tests/test_bundle_v2_fixture.py` 使用真实快照元数据（scorelab/Tracker 的 commit/blob/content_hash/readme_path/collected_at）与本地真实 README 字节，在临时存储中构造明确标记的测试运行；不调用 GitHub/DeepSeek/SiliconFlow，不写历史对象。覆盖 v2 序列化、不可变持久化、Manifest 发布、`EvidenceBundleStore` 回读、真实 ASGI GET（含 `repository_id` 过滤）以及五项追溯字段一致性。
- **查询去重指纹**：`search_fingerprint(q, sort, order, per_page, **extra)` 覆盖所有有效参数；当前 GitHub 客户端实际只发送 `q/page/per_page`（`sort/order` 固定未使用，`page` 在每轮分页中统一推进），指纹不含 `page` 并在代码注释说明。测试覆盖“同 q 不同 sort/order 不合并、完全相同只执行一次”。
- **前端文案**：顶部与条目状态统一为“召回候选片段（相关性待核查）”，不再使用“相关片段/找到相关片段”。
- **需求质量待办（进入 3b 前）**：本次真实提取把同一句 “Build a project tracker.” 同时写入 project_topic 与 required_features，需改进“项目目标 vs 具体功能”的区分与重复需求识别；同时补充模糊需求提示。不得自行补充原文没有的功能要求，本轮未修改提取模块。
- **测试**：后端 943 项全过；前端构建通过；单测 13+13；浏览器 37+27 项全过，外部请求 0。

---

## 附：官方文档依据

- Embeddings：`https://docs.siliconflow.com/en/api-reference/embeddings/create-embeddings`
- 限流：`https://docs.siliconflow.com/rate-limits/rate-limit-and-upgradation`
- 错误：`https://docs.siliconflow.com/en/faqs/error-code.md`
- 模型列表：`https://docs.siliconflow.com/en/api-reference/models/get-model-list.md`
- 中文站点 `docs.siliconflow.cn` 同名路径返回 404；API 域名按用户确认使用 `api.siliconflow.cn`。

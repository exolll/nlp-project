# ProjectPilot 3b 深度核查实施方案（修订版 v5.2.2，待批准编码）

- 状态：待批准编码（本轮只修改方案，不编码、不调用外部服务、不改数据库、不改历史 Bundle）
- 日期：2026-11-23（v5.2.2 最终一致性修订）
- 依赖：3a 已通过的 EvidenceBundle v2、Chunk Index、Run Manifest、对象存储 CAS
- 本文件替代上一版 3b 方案；上一版未编码

---

## 0. 本轮修订清单（问题 → 修改 → 结构 → 对 3a 影响 → 测试）

| # | 原方案问题 | 修改 | 数据结构变化 | 对 3a 影响 | 新增测试 |
|---|---|---|---|---|---|
| 1 | 只区分 failed，无法表达“请求结果未知”；失败重试可能重复计费 | 新增 `outcome_unknown`；调用前 CAS 锁定，调用结果先落盘；`retry_from_operation_id` 取代布尔重试；成功仓库复用，不重放未知 | Manifest 操作/仓库状态增加 `dispatching/recorded/outcome_unknown`、`attempt_id/input_hash`、`retry_from_operation_id` | 无 | 超时不自动重放；未知不重扣；仅失败/未知仓库重试；成功仓库零调用 |
| 2 | 坐标只写了规则，未核对实现 | 以代码为准：Chunk 偏移是 **LF 归一化后整篇 README 的全局字节偏移**，不是 Chunk 局部 | 报告引用沿用该坐标系并新增复现说明 | 无 | 真实快照归一化后逐字节复核；CRLF 映射；行号/半开区间 |
| 3 | 缺阶段 4 可消费的仓库级汇总 | 每仓库新增 `hard_constraint_summary` 与 `logic_group_results`；不生成总分/满足率 | RepositoryReview 增加两组统计 | 无 | required/prohibited/preferred 计数；ALL/ANY 组结果；unknown 不通过 |
| 4 | 模型漏项与“没有证据”混同 | 新增 `reason_code=model_output_incomplete`；Python 检查覆盖/重复/未知/跨版本；仓库降为 partial | RequirementReview 增加 `model_omitted`；RepositoryReview status 允许 partial | 无 | 漏项、重复项、未知项、跨 revision 项；仓库不得标 ok |
| 5 | consent 只是布尔值 | 升级为带版本、文本哈希、时间戳、绑定信息的记录；服务端固定文本 | 操作新增 `consent` 对象 | 无 | 缺失/版本错/文本哈希错拒绝；绑定记录落盘 |
| 6 | quality_warnings 只存内存/未绑定版本 | 持久化到会话，绑定 `revision_hash`，编辑后重算；API 只读返回 | 会话新增 `quality_warnings` 映射；DocumentRequirements 加只读字段 | 加性，不影响检索/证据 | 编辑后重算；旧 hash 不返回；确认页可见 |
| 7 | GET 多操作歧义 | GET 支持 `operation_id`；未指定且多个操作 → 409 并列出可选 ID | 查询参数与错误 | 无 | 单操作默认；多操作 409；指定 ID 200 |
| 8 | 客户端可提交预算 | 服务端固定预算，客户端不得覆盖 | POST body 移除 budget；多余字段 422 | 无 | 伪造高预算被拒；缺省按服务端配置 |
| 9 | v1 兼容缺少索引校验 | v1 除 trace 外还验证 Chunk Index `ready` 及其 snapshot/repo 绑定 | 兼容检查扩展 | 无 | 索引非 ready/错绑定时拒绝 |

### 0.1 v3 增补

| # | 增补 | 结论 |
|---|---|---|
| 10 | 字节切片易误用 | 明确**只用 UTF-8 bytes 切片**；引用必须同时满足整篇范围、Chunk 全局字节区间、Chunk 行号区间；补中文/Emoji/CRLF/CR/UTF-8 边界测试 |
| 11 | 崩溃恢复不完整 | 新增两种恢复：dispatching 崩溃 → 超时 outcome_unknown、不重放；响应已写但 Manifest 未更新 → 按 attempt_id + 哈希恢复，不重调模型 |
| 12 | Idempotency-Key 未明确 | 同 key 同请求返回原 operation；同 key 不同请求 409；并发只产生一个 operation |
| 13 | unknown usage 记账 | outcome_unknown 的 usage 为“未知”，不得写 0；统计只累计已知调用并单列 unknown_calls |
| 14 | 硬约束映射不完整 | 明确 required/prohibited 的 verdict→hard check 映射；`logic_group_results` 只保留真实 `logic_group_id` 组，不再列 null 组 |
| 15 | 模型重复输出处理 | 同 ID 多次 → 全部不可信，Python 生成唯一 insufficient + duplicate_item 警告，仓库 partial；给模型输出 JSON Schema，全部转换/分组/统计由 Python 完成 |
| 16 | 授权/数据保存说明 | 补充项目自身保存的响应/报告/usage 和保留说明；每个新 operation（含重试）都有授权；unknown 重试需显式再次确认 |
| 17 | 两个补充项 | quality_warnings 持久化位置/CAS 一致性/hash 绑定；v1 逐项核验五项 trace（含 blob_sha、collected_at）；明确 GET 对唯一 running operation 的响应 |

### 0.2 v4 最终修订

| # | 位置 | 修改 | 原因 |
|---|---|---|---|
| 18 | §6 | 删除遗留 `normalized[byte_start:byte_end]` 字符串切片表达，统一为 UTF-8 bytes；新增行号→字节映射生成、换行符、空行、多行、Chunk 行中边界规则 | 与 §6.1 bytes 规则冲突 |
| 19 | §5.3/§5.4 | 新增迟到响应、已启动重试后旧响应到达、不同 Key 并发；Manifest 更新必须校验 attempt_id/预期状态；新增每 Run 单活动 operation 锁 | 防过期执行者覆盖与重复计费 |
| 20 | §14 | POST body 明确 `acknowledge_unknown` 条件字段，纳入幂等规范化请求 | 未知重试需显式确认 |
| 21 | §8 | supports/contradicts 必须至少一条有效引用；无有效引用强制 insufficient；部分无效保留有效项并告警 | 防止无引用直接 pass/fail |
| 22 | §5.1 | 增加 Operation 最终状态确定性聚合规则 | 避免多仓库混合状态歧义 |
| 23 | §9 | 增加 ALL/ANY 组内 operator/modality 一致性校验 | 防非法分组 |
| 24 | §3 | 核实 `required_features=null` 符合现有 Schema（`list[Nonempty] \| None`），无需改 Schema | 避免假设 |
| 25 | §12 | 输出截断/JSON 不完整/超 token 的失败处理 | 不留半份结果 |
| 26 | §10 | 保留“对象存储无自动 TTL”，真实部署前单独制定保留/删除策略 | 不虚构自动清理 |

### 0.3 v5 最终修订

| # | 位置 | 修改 | 原因 |
|---|---|---|---|
| 27 | §5.1/§5.1.1 | 只有全部仓库终态才聚合 Operation；非终态一律 running；统一 partial 与 outcome_unknown 优先级（unknown 优先） | 消除两节冲突 |
| 28 | §12/§11 | 每个 Operation 最多 5 个仓库，超限调用前拒绝；服务端固定，不加批 | 控制单操作规模 |
| 29 | §5.4/§11 | 幂等指纹拆分为客户端请求指纹 + 服务端运行指纹；先查 Key 再查活动锁 | 防跨 Bundle/配置复用 |
| 30 | §5.6 | 新增 error_code → retryable 表；不可恢复错误禁止重试 | 防反复无效调用 |
| 31 | §5.4.1 | 明确进程重启恢复扫描、超时、响应核验、终态与锁释放原子性 | 锁恢复闭环 |
| 32 | §5.4.2 | 活动锁作用域明确为“每个 Run 同时一个” | 保留单指针 |
| 33 | §6.4/§6.5 | 明确 Chunk `byte_end` 不含末尾换行；补 Chunk 字节区间与行号区间一致性校验 | 边界确定 |
| 34 | §16 | 测试矩阵扩到 51 项 | 覆盖新增场景 |

### 0.4 v5.1 修订

| # | 位置 | 修改 | 原因 |
|---|---|---|---|
| 35 | §5.1 | 操作表与 §5.1.1 完全一致：partial 不含 outcome_unknown；unknown 优先；非终态保持 running；failed 与 unknown 分述 | 消除旧规则 |
| 36 | §5.4.1/§5.4.2/§11.1 | 活动锁统一为“每个 Run 一个”；删除 owner/task/revision/bundle/model/consent 作为锁定目标的旧表述 | 统一作用域 |
| 37 | §5.6.1 | retryable 明确为仓库级；定义 ok/partial/retryable failed/non-retryable failed/unknown/skipped 的复用与调用规则；混合场景允许新 Operation | 完善重试 |
| 38 | §5.6/§19.5 | `OUTPUT_TRUNCATED` 改为 non-retryable；调整输出策略后使用新配置与新 Operation | 预算/Prompt 不变时重试不可靠 |
| 39 | §14/§19.1 | 补全错误码：IDEMPOTENCY_RUN_CONFLICT、NOT_RETRYABLE、REPOSITORY_BUDGET、NO_ELIGIBLE_REPOSITORIES；仓库 >5/为空不建 Operation、不调模型 | 统一响应 |
| 40 | §5.4.2 | （v5.1 记录，已被 v5.2 #44 取代）恢复触发与周期任务；未超时 dispatching 后续仍可恢复 | 防锁永久占用 |
| 41 | §11.2/§12 | 每 Operation 1–5 个可核查仓库；summary 增加 `repositories_skipped` | 空集合与统计完整 |
| 42 | §16 | 测试矩阵扩到 56 项 | 覆盖新增场景 |

### 0.5 v5.2 修订

| # | 位置 | 问题 | 修改 |
|---|---|---|---|
| 43 | §5.6/§5.6.1 | 仓库级与 Operation 级重试规则冲突 | 统一以仓库级 retryable 为准；明确初次/重试的 0、>5、无符合条件仓库错误码；删除 Operation 级旧表述 |
| 44 | §5.4.2/§14/§19.2 | GET 触发 reconcile，与只读冲突 | GET 纯只读；恢复仅在启动、周期 Worker、POST 创建前执行；GET 返回持久化状态 |
| 45 | §16 #53 | 把幂等重放与业务重试混为一谈 | 拆分为场景 A（同 Key 同指纹返回原 Operation）与场景 B（新 Key 业务重试 409） |
| 46 | §5.6/§19.7 | 以 HTTP 状态码直接判定失败 | 按“请求是否已发送/是否确认执行”分类；5xx/读超时保守判 outcome_unknown；新增执行确定性适配要求 |
| 47 | §19.6 | 未定义 202 后 Worker 生命周期 | 复用现有 BackgroundTasks + 持久化 Manifest；启动恢复 + 周期 Recovery Worker；明确重启发现 |
| 48 | §11.1 | 超时只用 Operation 时间 | 每仓库 attempt 记录 `issued_at/timeout_at`，按仓库独立判断 |
| 49 | §14/§19.2 | outcome_unknown 的 GET 语义不清 | 已生成报告时返回 200 + `status: outcome_unknown`，保留成功/失败/未知仓库与 unknown usage；无报告才 409 |
| 50 | §5.6.2 | partial 处理未闭环 | 普通重试复用 partial 不调用；修复需新配置、新指纹、新 Operation，不静默改旧报告 |
| 51 | §16/§20/§21 | 矩阵与关闭状态需同步 | 修订 #53/#54，新增 9 项（57–65），矩阵共 **65 项**；更新关闭表与风险 |

### 0.6 v5.2.1 修订摘要（历史）

v5.2.1 已完成：多 Operation 历史与 Key 索引、仓库级重试统一、failed/unknown 按执行确定性、GET 纯只读、Worker/Recovery 契约、Provider 细分分类、GET 契约、测试矩阵 82 项。

### 0.7 v5.2.2 修订摘要（本轮）

| # | 问题 | 解决 | 章节 |
|---|---|---|---|
| 61 | 版本/历史记录缺失 | 标题、状态、§1.1 升级 v5.2.2；保留 §0.1–0.6 历史；新增本节 | 全篇 |
| 62 | 重试场景未区分 | 明确场景 A（同指纹普通重试）与场景 B（新配置全量重跑）；全文统一引用 | §4、§5.2、§5.6.1、§5.6.2、§19.5、§19.7 |
| 63 | 错误码多套定义 | §5.6 建立唯一 Provider 错误码字典（状态/执行确定性/retryable/ack/usage），其他章节只引用 | §5.6、§19.7 |
| 64 | retryable 数据模型含糊 | 服务端布尔字段 + `requires_acknowledgement`；pending 等为 false；客户端不可传入 | §5.6、§11.1、§19.5 |
| 65 | GET 契约冲突 | 以 §19.2.1 唯一标准；failed 一律 409；删除 pending 表述；GET 纯只读 | §14、§19.2、§19.2.1 |
| 66 | Recovery 枚举不完整 | 补 Local 递归分页、S3 ListObjectsV2 ContinuationToken、续扫、新增 Run、多实例 CAS | §19.6 |
| 67 | 旧结构与格式残留 | 全文统一多 Operation Manifest；修复 §5.1 表格；§11.1 标记结构示例 | §5.1、§11.1、全篇 |
| 68 | 测试矩阵需修正 | 修正 #67/#70/#79；新增 83–86（错误码唯一性、retryable、分页续扫、多实例） | §16 |
| 69 | 代码核查依据不足 | §1.1 增补文件/类/函数/能力状态与 3b 新增项 | §1.1 |
| 70 | 关闭状态/风险 | 只有全文一致才关闭；开发任务仍标未实现 | §20、§21 |

---

## 1. 现有实现核对结论（以代码为准）

| 对象 | 实际结构 | 3b 使用方式 |
|---|---|---|
| RequirementJSON | 12 个业务字段，`extra=forbid` | 不改 |
| RequirementItem | `requirement_item_id/field/value_path/text/modality/source_kind/evidence_ids/logic_group_id/operator/supersedes_item_id` | 直接使用 |
| Modality / Operator | `required/preferred/prohibited/informational`；`ALL/ANY/not_applicable` | 已有，无需兼容补丁 |
| RequirementRevision | `revision_id/task_id/.../status/requirements/requirement_items/change_log/created_at/confirmed_at` | 只接受 `confirmed` |
| source_evidence | `evidence_id/field/original_text/source_kind/source_location/requirement_item_id/segment_id/start_offset/end_offset` | 引用与警告保留 |
| EvidenceBundle v2 | 候选含 `commit_sha/blob_sha/content_hash/readme_path/collected_at/repository/status/items` | 3b 只读消费 |
| Bundle item | `status(retrieved/empty)`、`relevance(unverified)`、`chunks` | retrieved ≠ relevant |
| Bundle chunk | `chunk_id/text/line_start/line_end/byte_start/byte_end/methods/ranks/dense_distance/rrf_score` | 引用回查 |
| Chunk manifest | `chunk-index/<index_key>/manifest.json`；块含全局 byte/line 偏移、content_hash、commit/path | 引用坐标来源 |
| Run Manifest | 已有 planner/screening/evidence/deep_check/chunk_index/evidence_bundle 操作 | 新增 `deep_review_operations` + `deep_review_idempotency_keys` + `active_deep_review` |
| DeepSeek 封装 | `call_provider` 单次、`strict_json`、usage 原样返回；无自动重试 | 3b 复用风格 |
| 存储 | Local FileLock + 原子替换；S3 ETag 条件写；不可变对象 | 报告不可变 |

### 1.1 现有代码能力核对（v5.2.2，基于实际代码）

| 能力 | 现状 | 3b 方案要求 |
|---|---|---|
| 后台执行 | `POST` 使用 FastAPI `BackgroundTasks`（request 结束后在当前进程内执行）；**没有**持久化队列/独立 Worker | 复用 BackgroundTasks 启动执行 + Manifest 持久化；新增启动恢复扫描与周期 Recovery Worker（开发任务，不引入消息队列） |
| 重启恢复 | 搜索有按 Manifest 重放的 `recover`；**没有全局启动扫描/周期任务** | 3b 新增启动扫描 + 周期 Recovery（开发任务）；POST 创建前执行恢复检查 |
| Provider 错误分类 | `call_provider` 只区分 401/402/429/5xx/超时/网络；超时与网络统一“结果未知”文案；**无法区分 DNS/连接失败与已发送后的读超时** | 3b 新增执行确定性适配（开发任务）：明确“发送前失败”vs“已发送但无响应”；无法确认时保守 `outcome_unknown` |
| GET 语义 | 现有 GET 只读 Manifest/对象，不触发后台任务 | 3b GET 保持纯只读，不做 reconcile |

> 上表“新增/开发任务”均未实现；方案不把它们写成现有能力。

**代码依据（v5.2.2 核对）**

| 能力 | 文件 / 类 / 函数 | 当前实现 | 3b 处理 |
|---|---|---|---|
| 本地 CAS | `backend/app/services/storage.py` / `LocalStorage.read_version/compare_and_swap/put` | FileLock + 临时文件 + `os.replace` + fsync | 直接复用 |
| S3 条件写 | 同文件 / `S3Storage._write/read_version` | `IfMatch/IfNoneMatch`，ETag 作版本 | 直接复用；生产需真实多实例验收 |
| Manifest CAS | `backend/app/persistence/run_manifest.py` / `RunManifestStore.update` | 64 次 CAS 重试 + `_validate/_invariants` | 扩展多 operation/Key/锁结构 |
| 不可变写 | `backend/app/services/search_persistence.py` / `SearchPersistence._put_immutable` | 先读后写、哈希回读 | 报告/响应复用 |
| 后台任务 | `backend/app/main.py` / `submit`；`services/search_routes.py` / `pipeline/deep_pipeline` | FastAPI `BackgroundTasks`，无队列、无 lifespan | 复用机制；新增 Worker/Recovery |
| Provider 调用 | `backend/app/services/extraction_provider.py` / `call_provider` | 只区分 401/402/429/5xx/超时/网络；无 DNS/Connect/Write/Read 细分 | **新增执行确定性适配器** |
| 存储枚举 | `StorageService` Protocol（get/put/delete/read_version/compare_and_swap） | **无 list/scan** | 新增只读 `list_keys` 或登记对象 |
| 深度核查 | — | 未实现（`deep-check-v1` 是整篇 README 的另一路径） | 全新开发，不覆盖旧数据 |

---

## 2. Query Fingerprint / Request Fingerprint（确认，不改）

| 概念 | 组成 | 用途 |
|---|---|---|
| Query Fingerprint | `q + sort + order + per_page + extra`（当前 sort/order 固定未用） | 合并相同搜索条件 |
| Request Fingerprint | `q + page + per_page + sort + order` | 实际 HTTP 请求；不同 page 必为不同请求 |

不同 page 不会被视为同一请求：每页独立 `request_id`、独立响应对象，`hit` 记录 page/position/request_id。3b 不触发检索，只消费 Bundle。

---

## 3. 需求提取质量优化方案（仅方案）

### 3.1 职责

| 层 | 负责 | 不负责 |
|---|---|---|
| Prompt v4 | 区分总体目标与可验收功能；禁止同句跨字段复用；禁止常识补充；要求逐字原文 | 不保证绝对遵守 |
| Python 校验 | 重复检测、原文包含校验、生成并持久化 warnings、拒绝扩写 | 不改写用户文本、不自动删需求 |
| 前端 | 展示警告、缺失项、补充/删除入口；确认页风险提示 | 不自动合并或删除 |

### 3.2 规则

1. `project_topic` 只放总体目标/产品类别；`required_features` 只放可验收能力；
2. 同一句不得同时作为 topic 与 feature；只有目标时 `required_features=null`，`missing_information` 写“未提供具体功能要求”；
3. 每条 item 文本必须是其 evidence `original_text` 的规范化逐字子串；
4. 禁止同义扩写与补充原文不存在的功能；
5. 歧义写入 `uncertain_information`，不猜测。

**Schema 核实（v4）**：现有 Pydantic `RequirementJSON.required_features` 类型为 `list[Nonempty] | None`，因此“仅项目主题、无具体功能”时置 `null` 是合法的，无需修改 Schema。其他列表字段同理。

### 3.3 警告持久化（新增修订）

- 存储：会话 `task.requirements.quality_warnings[revision_id] = {revision_hash, computed_at, warnings:[...]}`；
- 警告结构：`{code: DUPLICATE_PROJECT_GOAL|DUPLICATE_REQUIREMENT|EVIDENCE_NOT_VERBATIM, field, requirement_item_id, evidence_ids, message}`；
- **绑定 revision_hash**：GET/编辑/确认时重新计算当前草稿哈希；哈希不一致的旧警告不返回；编辑保存后重算；
- 不进入 RequirementJSON/RequirementRevision；API 以只读加性字段 `quality_warnings` 返回（含 revision_hash）；
- 不自动删除重复需求；允许确认“只有主题、没有功能”的版本；
- `EVIDENCE_NOT_VERBATIM` 在提取阶段直接失败（不落草稿）；`DUPLICATE_*` 仅警告。

**持久化与一致性（v3 补充）**

- 实际位置：会话 JSON `task.requirements.quality_warnings[revision_id] = {revision_hash, computed_at, warnings}`，与需求版本同一聚合对象；写入走现有 `InputState` CAS，跨进程一致；
- 每次提取、编辑保存、确认前**重算**；若 `revision_hash` 与当前版本不一致，视为过期，不返回也不沿用；
- 并发编辑：CAS 冲突按现有规则重读重算，不合并旧警告；
- 提取 job 的 `validation_diagnostic` 只用于失败诊断，不作为警告通道。

---

## 4. 3b 业务流程图（含未知/重试）

```
Revision(confirmed) + EvidenceBundle v2
        ↓
绑定校验：task/run/revision_hash/plan_hash/bundle_hash + 每仓库 trace
        ↓ 失败 → 拒绝，不调用
收集候选片段：Bundle 各 item 的 chunks，按 chunk_id 去重（每仓库）
        ↓
预算检查：唯一 chunk ≤16；完整请求 token ≤24k
        ↓ 超限 → 仓库 failed（调用前，不截断）
CAS 锁定：仓库 pending → dispatching（记录 attempt_id + input_hash）
        ↓
DeepSeek 调用（每仓库 1 次，无自动重试）
        ↓
结果落盘：response 对象（哈希）→ 仓库 recorded → 引用校验 → 生成报告
        ↓
  超时/网络中断且无响应 → outcome_unknown（不得自动重放）
        ↓
Manifest 汇总：ready / partial / failed / outcome_unknown
        ↓
GET（可指定 operation_id）→ 前端展示
        ↓
需重试（场景 A）：新 POST + 新 Key + retry_from_operation_id，仅符合条件仓库重新调用；
        已成功（ok）与 partial 仓库复用，零重复计费
换配置（场景 B）：新 POST + 新 Key，不带 retry_from；全部仓库用新配置重跑
```

---

## 5. 失败、未知与重试（P0-1）

### 5.1 状态

| 层 | 状态 | 含义 |
|---|---|---|
| 仓库 | pending | 尚未调用 |
| 仓库 | dispatching | 已 CAS 锁定并准备/正在调用 |
| 仓库 | recorded | 已收到响应并落盘 |
| 仓库 | ok | 引用校验与报告生成成功且覆盖完整 |
| 仓库 | partial | 报告已生成但存在漏项/重复/未知项告警 |
| 仓库 | failed | 已明确知道本次调用失败，且**能够确认请求没有被服务商执行**，或已得到明确的失败结果（如 401/402、确认未执行的 429、完整响应但 JSON 非法、输出截断） |
| 仓库 | outcome_unknown | 请求**可能已被服务商接收或执行**，但客户端无法确认最终执行结果（读超时、发送状态不确定、无法确认的 5xx/429） |
| 仓库 | skipped | v1 无法核验或预算前拒绝 |
| 操作 | running | 创建后立即为 running；只要存在非终态仓库（pending/dispatching/recorded）就保持 running |
| 操作 | ready | 全部仓库 ok |
| 操作 | partial | 至少一个 ok/partial，且**不存在** outcome_unknown；其余为 failed/skipped |
| 操作 | failed | 无 ok/partial、无 outcome_unknown，全部 failed/skipped |
| 操作 | outcome_unknown | 存在至少一个 outcome_unknown（优先于 partial 与 failed；逐仓库明细保留） |

### 5.1.1 最终状态确定性聚合（v5）

**聚合前置条件**：所有仓库必须进入终态 `{ok, partial, failed, outcome_unknown, skipped}`。只要存在 `pending / dispatching / recorded`，Operation 状态必须保持 **`running`**，不得提前聚合。

全部终态后按以下**固定优先级**计算，不依赖遍历顺序：

1. 全部仓库 `ok` → `ready`；
2. 存在 `outcome_unknown` → `outcome_unknown`（**优先级高于 partial**；即使有其他 `ok/partial`，未知计费必须显式暴露，逐仓库结果仍保留）；
3. 存在 `partial` 或（至少一个 `ok/partial` 且其余为 `failed/skipped`）→ `partial`；
4. 无 `ok/partial`、无 `outcome_unknown` → `failed`（含全部 skipped）。

§5.1 的状态表与本节的优先级一致：`outcome_unknown` 永远优先于 `partial`；`running` 只表示尚未全部终态。报告与 GET 同时返回逐仓库状态，聚合不丢失明细。

### 5.2 规则

1. **调用前 CAS**：仓库 `pending → dispatching` 与 `attempt_id/input_hash` 一次原子写入；并发第二个执行者会失败，不重复调用。
2. **结果先落盘**：响应对象写入私有存储并按哈希登记，再 `dispatching → recorded`；报告只从已登记响应编译。
3. **未知不重放**：超时/中断且无响应 → `outcome_unknown`；同一操作内**不自动重放**。外部 API 无幂等键，无法证明“未执行”，自动重放可能重复计费。
4. **场景 A（同一运行指纹普通重试）**：新 POST + 新 Idempotency-Key + `retry_from_operation_id`；服务端校验同 revision/plan/bundle/model/prompt/config；ok/partial 复用；仅符合条件 failed 与已确认 outcome_unknown 重新调用；历史 Operation/报告不修改。
5. **场景 B（新配置重新核查）**：新 POST + 新 Key + **不携带 `retry_from_operation_id`**；产生新 run 指纹（prompt/config 变化）；创建全新独立 Operation；全部目标仓库重跑；**不复用**旧配置的 ok/partial/failed；新旧报告/usage/授权分别保存。
6. **计费记录**：每次调用记录 usage + attempt_id；复用仓库 usage 记录为 0 或标注 `reused=true`，不计费。
7. 客户端不能通过 POST 强制包含/覆盖预算、模型、超时或 retryable；服务端固定/计算。

### 5.3 崩溃恢复（v3）

| 场景 | 恢复方式 |
|---|---|
| dispatching 后进程崩溃 | 记录 `dispatching + attempt_id + issued_at`；恢复时已超时且无响应哈希 → 仓库 `outcome_unknown`，**不自动重放**；usage 标记未知 |
| 响应文件已写入、Manifest 尚未更新 | 通过 `attempt_id` 定位 `response_key`，校验文件哈希；匹配则补写 `recorded` 并继续编译，**不重新调用模型** |
| 响应哈希不匹配/文件缺失 | `outcome_unknown` 或 `failed`（按可确认性），不重放 |
| 报告已写、Manifest 未提交 | 按报告哈希可恢复引用；报告对象不可变，重复写入相同字节幂等 |

### 5.4 Idempotency-Key（v3）

- Key 作用域：owner + task + run + 操作类型；
- **相同 Key + 相同规范化请求** → 返回已存在 operation，不新建、不调用；
- **相同 Key + 不同请求**（revision/bundle/consent/retry 来源不同）→ `409 IDEMPOTENCY_CONFLICT`；
- 并发重复提交：Claim 通过 Manifest CAS 只允许一个 operation 写入；其余读取同一 operation；
- 重试是新 operation，必须使用**新 Key**；旧 Key 重放只会返回旧 operation。

### 5.4.0 两级幂等指纹（v5）

| 指纹 | 组成 | 用途 |
|---|---|---|
| Client Request Fingerprint | canonical JSON：`{revision_id, consent.version, consent.accepted, retry_from_operation_id, acknowledge_unknown}` | 判断“同 Key 是否同一用户请求” |
| Server Run Fingerprint | `{revision_hash, plan_hash, bundle_hash, model, prompt_version, config_hash}` | 防止跨 Bundle/配置复用旧结果 |

处理顺序：

1. **先按 Idempotency-Key 查已有记录**：
   - Client 指纹不同 → `409 IDEMPOTENCY_CONFLICT`；
   - Client 相同但 Server Run 指纹不同（Bundle 已变、模型/提示/配置变更）→ `409 IDEMPOTENCY_RUN_CONFLICT`；
   - 两者都相同 → 直接返回原 operation，不检查锁、不调用模型。
2. 没有该 Key：再检查 `active_deep_review` 活动锁；已有 → `409 DEEP_REVIEW_ACTIVE_OPERATION`。
3. 通过后 CAS 创建 operation + 写 Key 记录 + 活动锁；三者一次 Manifest 更新完成。
4. 重试使用新 Key，Server Run 指纹必须与 `retry_from_operation_id` 相同，否则拒绝。

### 5.4.1 迟到响应与并发保护（v4）

1. **状态更新必须带条件**：所有仓库级更新都校验 `operation_id + attempt_id + expected_status`；不匹配的更新一律拒绝（`SearchStateError`），过期执行者不能覆盖新状态。
2. **迟到响应**：若仓库已 `outcome_unknown` 或 operation 已终态，迟到的模型响应仍写入 `deep-review-late-responses/<attempt_id>.json` 并记录 `late_response_hash/at` 审计字段；**不修改**仓库/操作状态、报告或新 operation。
3. **重试后旧响应到达**：旧 operation 保持原状态并保留迟到审计；新 operation 读取自己的 attempt/response，不受影响。
4. **不同 Idempotency-Key 并发**：**每个 Run 同一时刻只允许一个活动 Deep Review Operation**。Manifest 增加 `active_deep_review` 单指针，Claim 时 CAS 写入；已有活动 operation → `409 DEEP_REVIEW_ACTIVE_OPERATION`（返回活动 operation_id）；终态（ready/partial/failed/outcome_unknown）后释放，才允许新 operation/重试。该锁防止不同 Key 造成重复计费。
5. 进程重启后：`active_deep_review` 指向的 operation 若超时仍无响应，按 §5.3 转 `outcome_unknown` 并释放锁；不自动重放。

### 5.4.2 活动锁作用域与恢复（v5）

- 作用域：**每个 Run 同一时刻只允许一个活动 Deep Review Operation**；Manifest 单指针 `active_deep_review`，不引入多锁。
- 启动恢复扫描：遍历存在 `active_deep_review` 的 Run：
  1. 读取 operation 与每仓库 `attempt_id/response_key`；
  2. 若响应文件存在：校验哈希 → 补写 `recorded` → 继续编译（不调模型）；
  3. 若不存在且已超时：仓库 → `outcome_unknown`（usage known=false）；
  4. 所有仓库终态后，在**同一次 CAS 更新**中写入 Operation 终态并置 `active_deep_review=null`；不允许分两步导致锁悬挂。
- 未超时的 dispatching 保留原状态，等待正常执行者；其他实例只读。
- 终态与释放锁的原子性：一次 Manifest update 同时完成聚合、report 引用与锁释放；CAS 冲突重读重试，冲突超过预算则保留 running，不误释放。
- **恢复触发（v5.2）**：仅三类位置执行恢复：①服务启动扫描；②后台周期性 Recovery Worker；③POST 创建新 Operation 之前做必要的过期锁检查。**GET 不触发任何恢复**（见 §14/§19.2）。
- **GET 的只读语义**：GET 只读取 Manifest 与已持久化报告；不修改状态、不执行 CAS、不释放锁、不恢复报告、不调模型、不触发后台任务。若读到尚未恢复的过期 Operation，按当前持久化状态返回，不自行修复。
- **未超时的 dispatching**：启动恢复不处理；后续由周期 Worker 或下一次 POST 在其 `timeout_at` 之后恢复，不会永久占用活动锁。

### 5.5 usage 的未知语义（v3）

- `outcome_unknown` 仓库的 usage 不得写 0：`{known:false, prompt_tokens:null, completion_tokens:null, total_tokens:null}`；
- 操作级统计：`usage.calls` 只计已确认调用，`usage.unknown_calls` 单列；
- 报告不得把未知调用按 0 计入成本或覆盖率。

### 5.6 仓库级重试资格与唯一错误码字典（v5.2.2）

**retryable 是仓库级服务端布尔字段**，由“仓库状态 + error_code + 执行确定性”计算；`requires_acknowledgement` 是独立布尔字段。客户端传入这两个字段一律 422。

| 仓库状态 | retryable |
|---|---|
| pending / dispatching / recorded | false |
| ok / partial / skipped | false |
| failed | 按下方唯一错误码字典 |
| outcome_unknown | true + requires_acknowledgement=true |

#### 唯一错误码字典（其他章节只引用，不重新定义）

| error_code | 实际情况 | 请求已发送 | 服务商已执行 | 仓库状态 | retryable | requires_ack | usage |
|---|---|---|---|---|---|---|---|
| `DEEP_REVIEW_PROVIDER_UNAVAILABLE` | DNS 失败 / 连接建立前失败（确认未发送） | 否 | 否 | failed | true | false | 0（确认未调用） |
| `DEEP_REVIEW_PROVIDER_AUTH` | HTTP 401 | 是 | 否 | failed | false | false | 0 |
| `DEEP_REVIEW_PROVIDER_BALANCE` | HTTP 402 | 是 | 否 | failed | false | false | 0 |
| `DEEP_REVIEW_PROVIDER_REJECTED` | HTTP 429，协议确认未执行 | 是 | 否 | failed | true | false | 0 |
| `DEEP_REVIEW_OUTCOME_UNKNOWN` | HTTP 429，无法确认是否执行 | 是 | 不确定 | outcome_unknown | true | **true** | known=false, token=null |
| `DEEP_REVIEW_OUTCOME_UNKNOWN` | WriteTimeout / 发送状态不确定 | 不确定 | 不确定 | outcome_unknown | true | **true** | known=false, token=null |
| `DEEP_REVIEW_OUTCOME_UNKNOWN` | ReadTimeout / 已发送无响应 | 是 | 不确定 | outcome_unknown | true | **true** | known=false, token=null |
| `DEEP_REVIEW_OUTCOME_UNKNOWN` | HTTP 5xx 执行不确定 / 响应中断 | 是 | 不确定 | outcome_unknown | true | **true** | known=false, token=null |
| `DEEP_REVIEW_INVALID_OUTPUT` | 完整响应但 JSON 非法 | 是 | 是 | failed | false | false | 有 usage 则记录 |
| `DEEP_REVIEW_OUTPUT_TRUNCATED` | finish_reason=length / 输出截断 | 是 | 是 | failed | false | false | 记录响应 usage |
| `DEEP_REVIEW_CHUNK_BUDGET` | 唯一 chunk >16 | 否 | 否 | failed/skipped | false | false | 0 |
| `DEEP_REVIEW_TOKEN_BUDGET` | 请求估算 >24k | 否 | 否 | failed/skipped | false | false | 0 |
| `DEEP_REVIEW_REPOSITORY_BUDGET` | 可核查仓库 >5 | 否 | 否 | skipped（Operation 拒绝） | false | false | 0 |
| `DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES` | 初次提交 0 个可核查仓库 | 否 | 否 | skipped（Operation 拒绝） | false | false | 0 |
| `DEEP_REVIEW_BUNDLE_BINDING` / `INDEX_UNVERIFIABLE` / `BUNDLE_UNVERIFIABLE` | 快照/索引不可核验 | 否 | 否 | skipped | false | false | 0 |
| `DEEP_REVIEW_CONSENT_*` / 参数错误 | 授权/输入问题 | 否 | 否 | skipped（Operation 拒绝） | false | false | 0 |

规则：

- **幂等重放 ≠ 业务重试**：同 Key 同 Client/Server 指纹的重放直接返回原 Operation（即使原结果是 failed/truncated），不新建、不调用；
- 业务重试必须新 Key + retry_from；outcome_unknown 必须 `acknowledge_unknown=true`；
- 任何“明确 4xx 一律可重试”的表述作废，以上表为唯一标准。

### 5.6.1 仓库级重试与混合场景（v5.1 保留）

`retryable` 是**仓库级属性**，由该仓库的 error_code 推导：

| 仓库状态 | 新 Operation 中处理 | 是否调用模型 |
|---|---|---|
| ok | 复用已验证结果 | 否 |
| partial（报告已生成、存在漏项/重复告警） | **复用**，不重新调用（当前建议，保持成本可控） | 否 |
| failed + retryable=true | 重新调用 | 是 |
| failed + retryable=false | 保留原结果/错误，不重新调用 | 否 |
| outcome_unknown | 仅 `acknowledge_unknown=true` 时重新调用 | 条件性 |
| skipped（v1 不可核验/预算拒绝） | 保留 skipped，不调用 | 否 |

混合场景：

- 只要**至少一个**仓库符合重试条件（retryable failed 或已确认的 outcome_unknown），就允许创建新 Operation；
- 新 Operation 中：符合条件的仓库重新调用；ok/partial/non-retryable failed/skipped 全部原样复用并标注 `reused=true`；
- 若没有任何符合条件的仓库 → 409 `DEEP_REVIEW_NOT_RETRYABLE`，不创建 Operation、不调用模型；
- 新 Operation 仍受“每 Run 单活动锁”和 1–5 仓库限制。

### 5.6.2 partial 产品处理（v5.2.1，方案 A）

1. 同一运行指纹下的普通重试：partial 仓库复用旧结果，不调用模型；
2. 如需修复 partial，必须修改 Prompt 或运行配置；
3. 新配置产生新的 Server Run Fingerprint；
4. 使用新的 Idempotency-Key；
5. 创建全新的独立 Operation，**不携带 `retry_from_operation_id`**；
6. 所有目标仓库都使用新配置重新核查；
7. **不复用**旧配置下的 ok/partial 结果；
8. 原 Operation 与报告保持不可变；新旧 Operation 分别保存授权、指纹、报告和 usage；
9. 本期不提供局部修复/混合复用功能，不允许出现“新报告标记新 Prompt 但部分仓库实际使用旧 Prompt”。

### 5.6.3 提交与重试的统一错误场景（v5.2.1，唯一标准）

| 场景 | HTTP | 错误码 | 是否创建 Operation/调用模型 |
|---|---|---|---|
| 初次提交，0 个可核查仓库 | 422 | `DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES` | 否 |
| 初次提交，>5 个可核查仓库 | 422 | `DEEP_REVIEW_REPOSITORY_BUDGET` | 否 |
| 重试，至少 1 个符合条件仓库 | 202 | 无 | 是（仅调用符合条件仓库） |
| 重试，无任何符合条件仓库 | 409 | `DEEP_REVIEW_NOT_RETRYABLE` | 否 |
| 存在 unknown 但未确认 | 422 | `ACKNOWLEDGE_REQUIRED` | 否 |

不因不可重试仓库而阻止其他可重试仓库；相反也不得把单个仓库的 retryable 当作 Operation 整体属性。

---

## 6. 引用坐标（P0-2，按现有实现）

现有代码（`chunking.normalize_readme` + `_Builder`）定义：

1. **原始快照**：对象存储中的 README 原始字节；`content_hash = sha256(raw bytes)`。
2. **归一化**：UTF-8 严格解码 → `\r\n`、`\r` 全部替换为 `\n`，得到整篇 LF 文本。
3. **字节偏移**：`byte_start/byte_end` 是**整篇归一化 README 的全局 UTF-8 字节偏移**，不是 Chunk 局部；左闭右开 `[start, end)`。
4. **行号**：基于归一化文本按 `\n` 切分，**从 1 开始，闭区间**。
5. **Chunk 文本**：`chunk.text == normalized_bytes[byte_start:byte_end].decode('utf-8')`（UTF-8 bytes 切片，不使用字符串索引）；分块时已有断言。
6. **可复现**：读取原始快照 → 校验 `sha256(raw)==snapshot.content_hash` → 同样规则归一化 → 切 `[start,end)`；不得直接使用原始 CRLF 字节偏移。
7. 报告引用只存 `chunk_id + line/byte`；`quote` 由 Python 从块清单还原，不信任模型复写。
8. 语义提示：引用真实存在 ≠ 语义支持；GPS tracking 原文不能作为 PDF parsing 的支持证据。

### 6.1 字节切片规则（v3，必须按此实现）

模型只返回 `chunk_id + line_start + line_end`，**不返回字节偏移**；字节由 Python 计算：

1. 只有 `bytes` 可以按偏移切片：`normalized_bytes = normalized.encode('utf-8')`；
2. 行号必须满足 `chunk.line_start ≤ line_start ≤ line_end ≤ chunk.line_end`；
3. 由 Chunk 行表得到该行范围的全局 `byte_start/byte_end`，且必须落在 Chunk 的 `[chunk.byte_start, chunk.byte_end)` 内；
4. 用 `normalized_bytes[byte_start:byte_end]` 解码（严格 UTF-8）；解码失败＝切断多字节字符 → 拒绝；
5. `quote` 为该解码结果；Python 不使用字符串索引进字节偏移；
6. 校验链：chunk 存在 → 仓库/快照归属 → 行号在 Chunk 内 → 字节在整篇且在 Chunk 内 → quote 与块文本一致。

### 6.2 编码测试（v3 新增）

- 中文 3 字节字符：行内切片的起止都落在字符边界；
- Emoji（4 字节，含代理对语义）：quote 精确、不得截断；
- CRLF 原文：按归一化后偏移引用；原始偏移不直接使用；
- 单独 CR：同上；
- 原始快照 `content_hash` 校验失败 → 拒绝整份报告；
- 行号/字节越界、跨 Chunk、跨快照 → `citation_rejected`。

### 6.3 行号→字节映射（v4）

- 当前 Chunk manifest **不保存逐行偏移表**；只在块级别保存 `byte_start/byte_end/line_start/line_end`；
- 校验时从原始快照确定性生成：`raw → 校验 content_hash → normalize → normalized_bytes.split(b'\n')` 逐行累计 `len(line_bytes)+1`（最后一行无换行则不加），得到全局 `line→[start,end)`；
- 该表只用于校验，不落盘、不写入 MySQL；可由原始快照与 content_hash 完全重建；
- 生成后必须校验：Chunk 的 `byte_start/byte_end` 与 `line_start/line_end` 在该表中一致，否则拒绝该仓库。

### 6.4 引用区间边界（v4）

| 场景 | 规则 |
|---|---|
| 行末换行符 | **不纳入**引用：quote 为选中行内容以 `\n` 连接，不带末尾换行；`byte_end` 指向最后一行内容的末尾 |
| 多行引用 | 包含中间行的全部字节与行间 `\n`；`line_start..line_end` 连续 |
| 空行 | 允许出现在引用中间；若整个引用仅空白 → `citation_rejected` |
| Chunk 从行中间开始 | 仅可能出现在超长行 `split='chars'` 块；**这类块不进入模型输入**（显式警告 `chunk_split_chars_skipped`），其内容仍在 Bundle 中，不由 3b 引用 |
| 引用跨 Chunk | 拒绝（必须完整落在单个 Chunk 的 `[byte_start,byte_end)` 内） |
| 引用跨行但行范围超出 Chunk | 拒绝 |
| 全局/Chunk 双重范围 | 必须同时满足整篇归一化 README 与对应 Chunk 的有效范围 |

**实现契约**：后续代码的引用解析函数必须只接受 bytes 表、只返回 bytes 区间；测试必须覆盖上述所有边界，禁止在实现中退回字符串索引。

### 6.5 Chunk 字节/行号一致性（v5）

- 现有分块规则：`chunk.byte_end = offsets[line_end-1] + len(last_line_bytes)`，**不包含该行末尾的 `\n`**；`chunk.text` 为行内容以 `\n` 连接、无末尾换行；
- 校验时必须同时满足：
  1. `chunk.byte_start == global_line_start_byte(chunk.line_start)`；
  2. `chunk.byte_end == global_line_end_byte_excluding_newline(chunk.line_end)`；
  3. `chunk.text == bytes[chunk.byte_start:chunk.byte_end].decode('utf-8')`；
  4. 引用区间 `[cite_start,cite_end)` ⊆ `[chunk.byte_start,chunk.byte_end)`；
  5. 引用行号 ⊆ `[chunk.line_start, chunk.line_end]`；
- 任一不满足 → 该 Chunk 不进入模型输入或引用被拒绝（`citation_rejected`）；
- `split='chars'` 的超长行块不进入模型输入（§6.4）。

---

## 7. 报告与阶段 4 结构化输出（P0-3）

每仓库新增：

```json
"hard_constraint_summary": {
  "required":   {"pass": 2, "fail": 0, "unknown": 1, "total": 3},
  "prohibited": {"pass": 0, "fail": 0, "unknown": 1, "total": 1},
  "preferred":  {"evidence_found": 1, "unknown": 2, "total": 3}
},
"logic_group_results": [
  {"logic_group_id": "lg-1", "operator": "ALL", "member_item_ids": ["a","b"], "result": "pass|fail|unknown"}
]
```

- 统计只表达“文档证据状态”，**不生成总分、不生成满足率**；unknown 不得视为通过；
- preferred 的 `evidence_found` 只作后续加分依据，不是硬约束 pass；
- 组结果由 Python 按第 9 节真值表计算，模型只给单项判定；
- `logic_group_results` **只保留真实 `logic_group_id` 的组**；无分组的单项不生成 null 组，直接看单项 `hard_constraint_check`。

### 7.1 verdict → hard_constraint_check 映射（v3）

| modality | verdict | hard_constraint_check | 说明 |
|---|---|---|---|
| required | supports | pass | 有效引用支持 |
| required | contradicts | fail | 有效引用反证 |
| required | insufficient | unknown | 未找到、引用无效或模型漏项 |
| prohibited | supports | pass | 原文**明确说明未使用/合规** |
| prohibited | contradicts | fail | 原文**明确使用禁用项** |
| prohibited | insufficient | unknown | 未提及 = 未知，绝不是 pass |
| preferred | 任意 | not_applicable | 只记录 evidence_found/unknown，供评分加分 |
| informational | 任意 | not_applicable | 背景信息 |

该映射由 Python 纯函数执行；模型不输出 hard check。

---

## 8. 模型漏项与输出校验（P0-4）

Python 必须检查每个仓库：

1. **覆盖**：revision 中每个 `requirement_item_id` 是否恰好出现一次；
2. **重复**：同一 ID 出现多次 → **所有该 ID 的结果全部判为不可信**，不选第一条也不选最后一条；Python 生成**唯一**一条 `insufficient` + `reason_code=model_output_incomplete` + `model_omitted=false` + `warning=duplicate_item`；
3. **未知**：ID 不在本 revision → 丢弃并 warning `unknown_item`，不进入统计；
4. **跨 revision**：ID 属于其他版本 → 同上，拒绝；
5. **漏项**：缺失 → `verdict=insufficient`、`reason_code=model_output_incomplete`、`model_omitted=true`；
6. **仓库状态**：出现漏项/重复/未知/跨版本 → 仓库 `partial`，**不得标 ok**；
7. 报告 `summary` 记录 omissions/duplicates/unknowns 数量。

### 8.1 模型输出 JSON Schema（v3）

模型每次只处理一个仓库，只允许输出 verdict 与行号；**不输出字节偏移、hard check、分组或统计**：

```json
{
  "assessments": [
    {
      "requirement_item_id": "uuid",
      "verdict": "supports|contradicts|insufficient",
      "citations": [
        {"chunk_id": "uuid:0:hash", "line_start": 1, "line_end": 3}
      ]
    }
  ]
}
```

- 严格 Pydantic：`extra=forbid`、`strict=True`；未知字段整体判输出无效（`model_output_incomplete`）；
- citations 最多 2 条；`line_start/line_end` 为整数、1 起；
- 引用所在的 `chunk_id` 必须来自本次输入集合；
- 全部分组计算、hard check、统计、中文说明由 Python 完成。

`reason_code` 枚举（最终）：

| code | 含义 |
|---|---|
| `supported_by_citation` | 有效引用支持 |
| `conflicted_by_citation` | 有效引用反证 |
| `not_found_in_provided_evidence` | 候选片段中没有找到 |
| `citation_rejected` | 引用未通过 Python 复核 |
| `model_output_incomplete` | 模型漏项/重复/输出不可用 |

“模型没给该项”“模型重复给出”“候选里没有证据”必须分开统计。

### 8.2 引用与 verdict 强制关联（v4）

| 模型输出 | Python 处理 |
|---|---|
| supports 且至少 1 条有效引用 | 保留 supports；hard check 按映射 |
| contradicts 且至少 1 条有效引用 | 保留 contradicts；hard check 按映射 |
| supports/contradicts 但**没有任何有效引用** | 强制降级 `insufficient` + `reason_code=citation_rejected`；不得 pass/fail |
| 部分引用有效 | 保留有效引用；无效项写入 `rejected_citations`；warning `partial_citations_rejected`；verdict 可保留（≥1 有效） |
| 全部引用无效 | `insufficient` + `citation_rejected` |
| verdict=insufficient 却带引用 | 忽略引用并 warning `citations_for_insufficient` |
| 非 insufficient 且 citations 为空 | `citation_rejected` |

模型**不能**通过无引用判断生成硬约束 pass/fail；hard check 只在引用校验完成后计算。

---

## 9. ALL / ANY 真值表

（Schema 已含 `operator` 与 `logic_group_id`，不改 Schema。）

**ALL**：全 pass → pass；任一 fail → fail；无 fail 但有 unknown → unknown。

**ANY**：任一 pass → pass；全 fail → fail；无 pass 但有 unknown → unknown。

### 9.1 分组一致性校验（v4）

- 同组所有成员必须具有相同 `logic_group_id`、相同 `operator`（ALL 或 ANY），且 `operator != not_applicable`；
- 组内 `modality` 必须一致（required 组或 prohibited 组）；混合 modality → 该组**不参与硬约束计算**，记 warning `logic_group_inconsistent`，成员按单项判定；
- `preferred/informational` 不进入组计算；
- 只有 revision 中已确认的分组才有效；模型不得新建或修改分组；
- 单成员 group 按该成员单项结果处理，不额外放大。

其余规则：

- 只有原文/确认版本明确表达时才能使用；模型不得创建 `logic_group_id`；
- prohibited 未提及 = unknown，绝不 pass。

---

## 10. 数据授权记录（P0-5）

服务端固定授权文本（版本 `consent-v1`，哈希由服务端计算）。POST 只需：

```json
{"revision_id": "...", "consent": {"version": "consent-v1", "accepted": true}}
```

服务端记录：

```json
"consent": {
  "version": "consent-v1",
  "text_hash": "hex64",
  "accepted_at": "ISO8601",
  "revision_id": "uuid",
  "confirmed_revision_id": "uuid",
  "bundle_hash": "hex64",
  "model": "deepseek-flash",
  "purpose": "deep_review"
}
```

- 绑定 revision、bundle、模型与操作类型；版本或哈希不匹配 → 422；
- 授权文案明确：发送已确认需求文本 + 本次运行已保存的 README 候选片段；不上传原始 PDF/Word；不访问 GitHub；
- 数据保留边界：只陈述“项目不额外保存服务商侧数据；服务商处理与保留政策以其官方条款为准”，**不作未经证实的隐私承诺**。

### 10.1 项目自身保存的数据（v3）

| 数据 | 位置 | 保留说明 |
|---|---|---|
| 模型原始响应（每仓库） | 私有对象存储 `deep-review-responses/<attempt_id>.json` | 用于审计、引用复核与崩溃恢复；不对外公开 |
| 核查报告 | `deep-reviews/<operation_id>.json` | 不可变；随运行保留，除非用户/运维按明确策略删除 |
| 调用与用量记录 | Manifest `deep_review_operations[<id>].repositories[].usage` + 报告 | 记录 attempt/usage/未知状态，供计费核对 |
| 授权记录 | Manifest `deep_review_operations[<id>].consent` | 每次新建 operation 都写入；含版本、文本哈希、时间与绑定 |

- 当前对象存储**没有自动 TTL**；保留期由部署侧策略决定，方案不声称已实现自动过期；
- 新增 operation（包括失败/未知重试）都必须重新提交 consent 并写入新的授权记录；
- 若上一操作存在 `outcome_unknown` 仓库，重试必须在 POST 中显式携带 `acknowledge_unknown: true`，否则 422；提示将可能再次计费且无法确认上次是否已执行。

---

## 11. 数据模型

### 11.1 Manifest 操作（v5.2.2 多历史结构）

以下是**结构示意（伪 JSON，键名为占位符）**，不是可直接解析的 JSON：

```json
{
  "deep_review_operations": {
    "<operation_id>": {
      "operation_id": "uuid",
      "version": "deep-review-v1",
      "status": "running|ready|partial|failed|outcome_unknown",
      "revision_hash": "hex64",
      "plan_hash": "hex64",
      "bundle_hash": "hex64",
      "model": "deepseek-flash",
      "prompt_version": "deep-review-v1",
      "config_hash": "hex64",
      "client_fingerprint": "hex64",
      "run_fingerprint": "hex64",
      "consent": { "...": "见第 10 节" },
      "retry_from_operation_id": "uuid|null",
      "repositories": {
        "842846538": {
          "status": "pending|dispatching|recorded|ok|partial|failed|outcome_unknown|skipped",
          "attempt_id": "uuid|null",
          "input_hash": "hex64|null",
          "issued_at": "ISO8601|null",
          "timeout_at": "ISO8601|null",
          "response_key": "deep-review-responses/<attempt>.json|null",
          "response_hash": "hex64|null",
          "usage": {"known": true, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reused": false},
          "error_code": "string|null",
          "retryable": false,
          "requires_acknowledgement": false,
          "late_response_key": "deep-review-late-responses/<attempt_id>.json|null",
          "late_response_hash": "hex64|null",
          "late_response_at": "ISO8601|null",
          "updated_at": "ISO8601"
        }
      },
      "report_key": "deep-reviews/<operation_id>.json|null",
      "report_hash": "hex64|null",
      "usage": {"calls": 0, "unknown_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
      "error_code": "string|null",
      "created_at": "ISO8601",
      "updated_at": "ISO8601"
    }
  },
  "deep_review_idempotency_keys": {
    "<key_sha256>": {
      "operation_id": "uuid",
      "client_fingerprint": "hex64",
      "run_fingerprint": "hex64",
      "created_at": "ISO8601"
    }
  },
  "active_deep_review": {"operation_id": "uuid", "claimed_at": "ISO8601"} | null
}
```

- **Operation 历史**：一个 Run 可保存多个 operation；GET 用 `operation_id` 查询；旧操作/报告不可变；
- **Idempotency-Key 索引**：只保存 `sha256(key)`，不存原始 Key；每个 Key 唯一绑定一个 operation；
- **原子创建**：一次 Manifest CAS 同时写入 `deep_review_operations[id]`、`deep_review_idempotency_keys[key_hash]`、`active_deep_review`；三者任一已存在导致条件失败，不允许“Operation 已建但 Key 未登记”或反向状态；
- **CAS 规则**：`update()` 的闭包内检查 Key/active/operation 全部条件，再一次性写回；冲突重读重试，最多 64 次；
- **保留/清理**：本阶段不自动删除历史 operation 或 Key；Key 索引保留到 Run 删除（防止 Key 重用绕过幂等）；未来清理必须保留 Key 墓碑，且不得删除仍被报告/活动锁引用的 operation；
- `retryable` 初始为 `false`，`requires_acknowledgement` 初始为 `false`；进入 failed/outcome_unknown 时按 §5.6 唯一字典计算，客户端不可传入；
- `outcome_unknown` 仓库 usage 必须 `known:false`、token 为 null；
- 操作级 usage 只累计已确认调用，`unknown_calls` 单列；
- 超时判断以仓库 `issued_at/timeout_at` 为准，Operation claimed_at 仅用于锁。

### 11.2 DeepReviewReport（不可变对象）

```json
{
  "schema_version": 1,
  "version": "deep-review-v1",
  "owner": "sha256(owner)",
  "task_id": "uuid",
  "run_id": "uuid",
  "confirmed_revision_id": "uuid",
  "operation_id": "uuid",
  "revision_hash": "hex64",
  "plan_hash": "hex64",
  "bundle_hash": "hex64",
  "model": "deepseek-flash",
  "prompt_version": "deep-review-v1",
  "scope": "provided_candidate_chunks",
  "notice": "仅基于 README 文档证据分析，未运行项目或审查源代码；候选片段可能未覆盖完整 README；未提及不代表项目不存在该功能。",
  "created_at": "ISO8601",
  "status": "ready|partial|failed|outcome_unknown",
  "repositories": [
    {
      "repository_id": "842846538",
      "snapshot_id": "uuid",
      "trace": {"commit_sha": "40hex|64hex", "blob_sha": "40hex", "content_hash": "64hex",
                 "readme_path": "README.md", "collected_at": "ISO8601"},
      "coverage": "full|truncated|non_utf8|empty",
      "status": "ok|partial|failed|outcome_unknown|skipped",
      "error_code": null,
      "input": {"chunk_ids": ["uuid:0:hash"], "chunk_count": 6,
                 "input_tokens_estimate": 8123,
                 "budget": {"max_chunks": 16, "max_input_tokens": 24000, "max_output_tokens": 2000}},
      "requirements": [
        {
          "requirement_item_id": "uuid",
          "field": "required_features",
          "text": "Build a project tracker.",
          "modality": "required",
          "operator": "not_applicable",
          "logic_group_id": null,
          "verdict": "supports|contradicts|insufficient",
          "reason_code": "supported_by_citation|conflicted_by_citation|not_found_in_provided_evidence|citation_rejected|model_output_incomplete",
          "hard_constraint_check": "pass|fail|unknown|not_applicable",
          "citations": [{"chunk_id": "uuid:0:hash", "line_start": 1, "line_end": 46,
                          "byte_start": 0, "byte_end": 1062, "quote": "..."}],
          "rejected_citations": [{"chunk_id": "uuid:1:hash", "reason": "out_of_range"}],
          "model_omitted": false
        }
      ],
      "hard_constraint_summary": {
        "required": {"pass": 2, "fail": 0, "unknown": 1, "total": 3},
        "prohibited": {"pass": 0, "fail": 0, "unknown": 1, "total": 1},
        "preferred": {"evidence_found": 1, "unknown": 2, "total": 3}
      },
      "logic_group_results": [
        {"logic_group_id": "lg-1", "operator": "ALL", "member_item_ids": ["a", "b"], "result": "unknown"}
      ],
      "usage": {"known": true, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reused": false},
      "response_hash": "hex64"
    }
  ],
  "summary": {
    "repositories_total": 5, "repositories_ok": 4, "repositories_partial": 0,
    "repositories_failed": 0, "repositories_outcome_unknown": 1, "repositories_skipped": 0,
    "requirements_total": 15, "supports": 7, "contradicts": 0, "insufficient": 8,
    "model_omitted": 0, "duplicates": 0, "unknown_items": 0,
    "usage": {"calls": 4, "unknown_calls": 1}
  },
  "warnings": []
}
```

- 引用中的 `byte_start/byte_end` 由 Python 从行号计算，模型不提供字节；报告保存计算后的值用于展示与复核。

---

## 12. 预算（业务预算，非接口硬限制）

| 项 | 值 | 性质 |
|---|---|---|
| 每仓库唯一 chunk | 16（硬性） | 超限调用前 failed/skipped，不截断 |
| 每仓库输入 | 24,000 tokens | 业务预算，含系统提示/需求/证据/JSON 格式 |
| 每仓库输出 | 2,000 tokens | 业务预算 |
| 每操作调用 | ≤5（每仓库 1 次） | 业务预算 |
| 每操作仓库 | **1–5（硬性）** | 0 个或 >5 个：调用前整体拒绝（`DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES` / `DEEP_REVIEW_REPOSITORY_BUDGET`）；本期不做分批 |
| 超限 | `DEEP_REVIEW_CHUNK_BUDGET` / `DEEP_REVIEW_TOKEN_BUDGET` | 调用前显式失败 |

- Token 估算优先使用官方 tokenizer；不可用时用保守上界（UTF-8 字节数）并在调用前检查；实际用量以 usage 为准；
- **24k/2k 是业务预算，不是 DeepSeek 接口硬限制**；当前仓库只配置提取 20k 输入/6k 输出、深度核对 64k 输入/4k 输出；`deepseek-flash` 真实上下文/输出上限必须官方核实，方案不臆断；
- 客户端不得覆盖预算；POST 无 budget 字段，携带则 422；
- 若 Bundle 中可核查仓库数 >5，整个 operation 在调用前失败，不截断仓库列表。

### 12.1 输出不完整/截断（v4）

| 情况 | 处理 |
|---|---|
| `finish_reason=length` 或达到 2,000 输出 token 上限 | 该仓库整份判定丢弃，`status=failed`，`error_code=DEEP_REVIEW_OUTPUT_TRUNCATED`；不使用半份 JSON |
| JSON 语法错误/严格解析失败 | 仓库 `failed`，`DEEP_REVIEW_INVALID_OUTPUT` |
| JSON 合法但字段缺失/未知字段 | 按模型输出校验失败处理；不得猜测补全 |
| 合法 JSON 但漏掉部分需求 | 按 §8 的 `model_output_incomplete` 处理（仓库 partial），与“截断”区分 |
| 超时/连接中断 | `outcome_unknown`，不重放 |

- 截断/解析失败的响应仍保存原始字节与哈希用于审计；
- 不把截断响应中的前几条判定写入报告；
- 幂等重放（同 Key 同指纹）返回原 Operation；业务重试（新 Key + retry_from）被 409 拒绝；修复需调整输出策略并使用新配置/新 Operation。

---

## 13. v1 / v2 Bundle 兼容

| Bundle | 允许 | 条件 |
|---|---|---|
| v2 | 是 | 五项 trace（commit_sha、blob_sha、content_hash、readme_path、collected_at）逐项与 Manifest + MySQL 复核 |
| v1 | 有条件 | 五项 trace 必须可从 Manifest/MySQL 恢复并逐项一致；同时 Chunk Index `ready`，块 `snapshot_id/repository_id/commit_sha/content_hash/readme_path` 与快照绑定一致 |
| 无法核验 | 拒绝 | `DEEP_REVIEW_BUNDLE_UNVERIFIABLE` / `DEEP_REVIEW_INDEX_UNVERIFIABLE`，提示重新搜索生成 v2 |

不补造任何 trace 字段；报告绑定 `revision/plan/bundle hash + repository/snapshot/commit/blob`，任一不一致拒绝。

---

## 14. API

```text
POST /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
body: {
  revision_id: uuid,
  consent: {version: string, accepted: true},
  retry_from_operation_id: uuid|null,
  acknowledge_unknown: boolean        // 必填（无则 422），默认 false 不允许省略
}
headers: Idempotency-Key
→ 202 | 409（未就绪/冲突/已有活动 operation） | 422（授权/版本/字段错误）

错误码：`DEEP_REVIEW_NOT_READY`、`DEEP_REVIEW_ACTIVE_OPERATION`、`IDEMPOTENCY_CONFLICT`、`IDEMPOTENCY_RUN_CONFLICT`、`DEEP_REVIEW_NOT_RETRYABLE`、`CONSENT_REQUIRED`、`CONSENT_VERSION`、`ACKNOWLEDGE_REQUIRED`、`INVALID_FIELDS`、`DEEP_REVIEW_REPOSITORY_BUDGET`、`DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES`；与 §19.1 完全一致。

GET /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
  ?operation_id=<uuid>&repository_id=<id>&requirement_item_id=<uuid>
→ 200 报告或 running 视图 | 409 未就绪/失败/多操作 | 404 过滤项不存在
```

- 当 `retry_from_operation_id` 对应操作包含 `outcome_unknown` 仓库时，`acknowledge_unknown` 必须为 `true`，否则 422；该字段**纳入幂等规范化请求哈希**（同 Key 改该值视为不同请求）；
- POST 在 Claim 前可执行恢复检查（仅已过期 attempt），但不允许 GET 触发恢复；
- GET 纯只读，按 §19.2 返回持久化状态/报告。
- 幂等规范化请求 = `{revision_id, consent.version, consent.accepted, retry_from_operation_id, acknowledge_unknown}` 的 canonical JSON 哈希。

- 无 `operation_id` 且存在多个操作 → **409 `DEEP_REVIEW_OPERATION_REQUIRED`**，响应列出可选 operation_id/status（不含内部路径）；
- 无 `operation_id` 且只有一个操作：
  - `ready/partial` → 200 返回报告；
  - `running` → **200** 返回 `{operation_id, status:"running", report:null}`（只读，可轮询）；
  - `outcome_unknown` 且报告已生成 → **200** 返回完整报告，顶层 `status:"outcome_unknown"`，保留成功仓库结果、失败/未知仓库状态与 unknown usage；不得伪装为 failed 或 ready；
  - `outcome_unknown` 且报告未生成 → 409 `{operation_id, status, error_code}`；
  - `failed` → 409 `{operation_id, status, error_code}`；**不存在“failed 有报告返回 200”的例外**；
- 指定 `operation_id` 时按该操作状态返回，规则同上；指定不存在 → 404 `DEEP_REVIEW_OPERATION_NOT_FOUND`；
- GET 完整错误契约见 §19.2.1；
- GET 只读对象与 Manifest，不调模型、不重建报告；
- POST 不接受 budget/model/timeout 字段（`extra=forbid` 拒绝）；重试额外要求 `acknowledge_unknown:true`（若上一操作存在未知仓库）；
- **新配置修复 partial**：必须新 Key、新 run 指纹、且**不得携带 `retry_from_operation_id`**；携带旧 operation 会被视为跨配置复用并拒绝。

---

## 15. 前端

- 「06 / 深度核查」按仓库展示：结论、需求逐条状态、引用原文；
- 标签固定“文档支持 / 文档冲突 / 证据不足”；禁止“已实现/完全满足/已验证”；
- 每条引用显示 README 路径、行号、字节区间、quote；不复制完整 README；
- 顶部固定声明（见第 11.2 notice）；prohibited 固定提示“未提及不代表符合禁止条件”；
- 显示 required/prohibited/preferred 计数与逻辑组结果，不显示总分/满足率；
- v1 无法核验时提示重新搜索；
- 授权弹窗展示第 10 节文案，需显式勾选。

---

## 16. 验收测试矩阵

| # | 用例 | 单元 | 集成 | 真实 |
|---|---|---|---|---|
| 1 | revision_hash 不一致拒绝 | ✓ | ✓ | |
| 2 | bundle_hash 不一致拒绝 | ✓ | ✓ | |
| 3 | 跨仓库引用剔除 | ✓ | ✓ | |
| 4 | 伪造 chunk_id | ✓ | ✓ | |
| 5 | quote/字节不一致 | ✓ | ✓ | |
| 6 | byte/line 越界 | ✓ | ✓ | |
| 7 | required supports/contradicts/insufficient | ✓ | ✓ | |
| 8 | prohibited 未提及 ≠ pass | ✓ | ✓ | |
| 9 | preferred 不参与硬约束 | ✓ | ✓ | |
| 10 | ALL/ANY 全真值表 | ✓ | ✓ | |
| 11 | 同 chunk 多需求只入模一次 | ✓ | ✓ | |
| 12 | >16 chunks 调用前失败 | ✓ | ✓ | |
| 13 | token 预算超限调用前失败 | ✓ | ✓ | |
| 14 | 单仓库失败 → partial，其他保留 | ✓ | ✓ | |
| 15 | 仅失败/未知仓库可重试 | ✓ | ✓ | |
| 16 | 成功仓库复用零调用/零计费 | ✓ | ✓ | |
| 17 | 超时 → outcome_unknown，不自动重放 | ✓ | ✓ | |
| 18 | retry_from_operation_id 校验与历史保留 | ✓ | ✓ | |
| 19 | v1/v2 兼容；v1 索引非 ready 拒绝 | ✓ | ✓ | |
| 20 | 报告不可变/哈希回读 | ✓ | ✓ | |
| 21 | GET 无副作用；多操作需指定 ID | ✓ | ✓ | |
| 22 | 前端文档边界、引用、禁止项提示 | ✓（浏览器） | | |
| 23 | consent 版本/哈希/时间戳/绑定 | ✓ | ✓ | |
| 24 | 客户端伪造预算/模型字段被拒 | ✓ | ✓ | |
| 25 | quality_warnings 绑定 revision_hash、编辑后重算 | ✓ | ✓ | |
| 26 | 模型漏项/重复/未知/跨版本处理 | ✓ | ✓ | |
| 27 | UTF-8 bytes 切片：中文/Emoji/CRLF/CR/字符边界 | ✓ | ✓ | |
| 28 | dispatching 崩溃 → outcome_unknown，不自动重放 | ✓ | ✓ | |
| 29 | 响应已写 Manifest 未更新 → attempt_id 恢复，不重调 | ✓ | ✓ | |
| 30 | Idempotency-Key：同 key 同请求同一 operation；不同请求 409；并发仅一个 | ✓ | ✓ | |
| 31 | outcome_unknown usage 为未知，不记 0；unknown_calls 单列 | ✓ | ✓ | |
| 32 | 同一需求模型重复输出 → 唯一 insufficient + duplicate_item，仓库 partial | ✓ | ✓ | |
| 33 | GET 唯一 running operation → 200 running view | ✓ | ✓ | |
| 34 | 每次重试 operation 都有新 consent 记录；unknown 重试需 acknowledge | ✓ | ✓ | |
| 35 | quality_warnings 持久化/跨进程 CAS/编辑后 hash 重算 | ✓ | ✓ | |
| 36 | v1 五项 trace 逐项核验 + Chunk Index ready/绑定 | ✓ | ✓ | |
| 37 | 多行/空行/行末换行/Chunk 行中边界的 bytes 引用 | ✓ | ✓ | |
| 38 | 迟到响应：unknown 后返回、重试后返回，只存审计不改状态 | ✓ | ✓ | |
| 39 | 不同 Idempotency-Key 并发：活动锁只允许一个 operation | ✓ | ✓ | |
| 40 | 无有效引用的 supports/contradicts → insufficient + citation_rejected | ✓ | ✓ | |
| 41 | ALL/ANY 组内 operator/modality 不一致 → 不参与硬约束 + warning | ✓ | ✓ | |
| 42 | finish_reason=length / JSON 截断 → 仓库 failed，不写半份结果 | ✓ | ✓ | |
| 43 | required_features=null 的“仅主题”版本可确认，警告可见 | ✓ | ✓ | |
| 44 | 存在 pending/dispatching/recorded 时 Operation 必须 running，不聚合 | ✓ | ✓ | |
| 45 | Bundle 可核查仓库 >5 → 调用前整体拒绝 | ✓ | ✓ | |
| 46 | Client/Server 两级幂等指纹；跨 Bundle/配置同 Key → 409 | ✓ | ✓ | |
| 47 | retryable 表：不可恢复错误禁止重试 | ✓ | ✓ | |
| 48 | 非 retryable operation 的 retry_from → 409 | ✓ | ✓ | |
| 49 | 启动恢复：响应存在→recorded 不调模型；缺失超时→unknown 并释放锁 | ✓ | ✓ | |
| 50 | 每个 Run 单活动锁；终态与释放原子 | ✓ | ✓ | |
| 51 | Chunk byte_end 不含末尾换行；字节/行号一致性校验 | ✓ | ✓ | |
| 52 | retryable/non-retryable 混合仓库：只重跑可重试，其余复用 | ✓ | ✓ | |
| 53 | OUTPUT_TRUNCATED：同 Key 同指纹幂等重放 → 原 Operation；新 Key 业务重试 → 409 NOT_RETRYABLE（两场景分别断言） | ✓ | ✓ | |
| 54 | 重启时未超时 dispatching 保留；后续超时由周期 Worker/POST 恢复；GET 不恢复 | ✓ | ✓ | |
| 55 | 可核查仓库 0 个 → 422，不建 Operation、不调模型 | ✓ | ✓ | |
| 56 | skipped 统计完整，聚合与 summary 一致 | ✓ | ✓ | |
| 57 | GET 纯只读：过期 Operation 返回持久化状态，不 CAS/不释放锁/不恢复 | ✓ | ✓ | |
| 58 | POST 创建前执行过期锁恢复，再 Claim | ✓ | ✓ | |
| 59 | HTTP 5xx 执行不确定 → outcome_unknown；usage known=false | ✓ | ✓ | |
| 60 | 发送前 DNS/连接失败 → failed + retryable | ✓ | ✓ | |
| 61 | Worker 生命周期：202 → pending → dispatching → recorded → 终态；重启重新发现 | ✓ | ✓ | |
| 62 | 不同仓库 attempt 时间不同，超时相互独立；旧 attempt 迟到不覆盖 | ✓ | ✓ | |
| 63 | outcome_unknown 已生成报告 → GET 200，成功仓库结果保留 | ✓ | ✓ | |
| 64 | partial 普通重试复用、不调用模型；不自动修复 | ✓ | ✓ | |
| 65 | 新运行配置/新指纹与旧 Operation 隔离；同 Key 跨配置 409 | ✓ | ✓ | |

### 16.1 新增可验证用例（v5.2.1，66–82）

| # | 测试目的 | 前置状态 | 执行操作 | 预期 HTTP/状态 | 允许调模型 | 允许改 Manifest |
|---|---|---|---|---|---|---|
| 66 | 一个 Run 保存多个历史 Operation | 已完成 op1，锁空闲 | POST 新 Key 创建 op2 | 202；两个 op 并存且 op1 不变 | 是（仅 op2） | 是 |
| 67 | 多 Key 独立映射 | 第一个 Operation 已终态且活动锁已释放 | 先后两个 Key 创建 | 两个 key_hash 各自指向不同 op；第二个创建成功 | 是（第二个） | 是 |
| 68 | 同 Key 不同 Client 指纹 | Key 已登记 | 同 Key 改 acknowledge_unknown | 409 IDEMPOTENCY_CONFLICT | 否 | 否 |
| 69 | 同 Key 不同 Server Run 指纹 | Key 已登记 | Bundle/配置变化后同 Key | 409 IDEMPOTENCY_RUN_CONFLICT | 否 | 否 |
| 70 | Operation+Key+锁原子性 | 无历史 | 同 Key 同指纹并发 + 不同 Key 并发 | 同 Key 返回同一 op；不同 Key 仅一个 202 其余 409 ACTIVE；无半登记 | 最多一次 | 是（原子） |
| 71 | 历史 Operation GET | 两个终态 op | GET ?operation_id=op1 | 200 op1 报告；不返回 op2 | 否 | 否 |
| 72 | Key/Operation 清理规则 | 终态历史 | 触发清理检查 | 本阶段不清理；Key 保留；无报告删除 | 否 | 否 |
| 73 | 初次 vs 重试错误码 | 0/>5 仓库；或无可重试仓库 | 初次 POST / retry POST | 422 NO_ELIGIBLE/BUDGET；409 NOT_RETRYABLE | 否 | 否 |
| 74 | 429 执行确定性 | Provider 模拟 429 | 调用 | 确认未执行→failed/retryable；不确定→unknown/需确认 | 是 | 是 |
| 75 | WriteTimeout vs ReadTimeout | Provider 模拟 | 调用 | Write→unknown；Read→unknown；均 usage null | 是 | 是 |
| 76 | 新配置修复不复用 | 旧 op 有 partial | 新配置+新 Key+无 retry_from 创建 | 全仓库重新调用；不引用旧结果 | 是（全部） | 是 |
| 77 | POST 后 BackgroundTasks 未注册 | op 已持久化、仓库 pending | 重启/恢复扫描 | 重新调度 pending；不建新 op | 是（pending） | 是 |
| 78 | 多实例恢复不重复 | dispatching 未超时 | 两实例同时扫描 | 无一接管；超时后仅一个转 unknown | 否 | 是（单次 CAS） |
| 79 | pending 重新调度 | 重启后存在 pending 且锁仍指向该 Run | 启动/周期 Worker 扫描 | pending 被重新调度并进入 dispatching；若已超时则 unknown；不得永久保持 pending | 是（pending） | 是 |
| 80 | retryable 与 error_code 一致 | 各仓库不同错误 | 重建/读取 | 状态→retryable 表一致；客户端不能覆盖 | 视仓库 | 视仓库 |
| 81 | GET 指定不存在 operation_id | 有历史 | GET 随机 uuid | 404 DEEP_REVIEW_OPERATION_NOT_FOUND | 否 | 否 |
| 82 | GET 全错误码一致 | 各状态 | 逐场景 GET | 与 §19.2.1 表一致；GET 全程无 CAS/锁释放/恢复 | 否 | 否 |
| 83 | 错误码字典唯一性 | 全文文档 | 检索所有 error_code | 只存在 §5.6 一套定义；其余章节仅引用 | 否 | 否 |
| 84 | retryable/requires_ack 服务端计算 | 客户端提交伪造字段 | POST | 422 INVALID_FIELDS；服务端字段不可覆盖 | 否 | 否 |
| 85 | Recovery 分页与续扫 | Local 多页 / S3 IsTruncated | 扫描中途中断再续 | 不漏 Run；cursor 续扫；完成后重扫新增 Run | 否 | 否 |
| 86 | 多实例分页扫描 | 两实例同时扫描同一前缀 | 并发恢复 | CAS 仅一个认领 attempt；无重复模型调用 | 否 | 是（单次 CAS） |

单元 = 纯函数与 Schema；集成 = 临时存储 + 假响应 + 真实 ASGI GET；真实 = 单独授权后 1 个运行、≤5 次调用、0 GitHub/Embedding，人工抽查引用。

---

## 17. 实施顺序与风险

1. 需求质量（prompt v4 + 原文校验 + 持久化 warnings + 只读 API）— 单独验收
2. 引用校验/真值表/预算/报告模型（纯函数 + 单元）
3. **Provider 执行确定性适配器**（DNS/Connect/Write/Read/401/402/429/5xx 分类）+ 单元测试
4. `deep_review_operations` / `deep_review_idempotency_keys` / 活动锁的 Manifest 结构与不变量
5. **Worker + Recovery Worker**（BackgroundTasks、启动扫描、周期恢复、枚举接口）
6. POST/GET 路由（多历史 operation_id、错误码、授权记录、预算拒绝）+ 假模型集成
7. 前端深度核查面板（浏览器 mock）
8. 离线 v1/v2 Bundle 夹具验收
9. 真实运行验收（单独授权）

风险：模型语义误判；引用真实性≠语义支持；候选片段覆盖不足；未知请求无法证明是否计费；provider 实际限制需核实；报告体积与前端加载；v1 无法核验只能拒绝；**Worker/Recovery/Provider 适配器与存储枚举接口均为待开发项**。

---

## 18. 本轮边界

只修改本方案文档；不编码、不调用 DeepSeek/SiliconFlow/GitHub、不改数据库、不改历史 Bundle、不修改 3a 已通过功能与预算。

---

## 19. 最终 API Schema 与状态机

### 19.1 POST

```text
POST /api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review
Idempotency-Key: uuid
{
  "revision_id": "uuid",
  "consent": {"version": "consent-v1", "accepted": true},
  "retry_from_operation_id": "uuid|null",
  "acknowledge_unknown": false
}
→ 202 {operation_id, status:"running"}
→ 409 DEEP_REVIEW_NOT_READY | DEEP_REVIEW_ACTIVE_OPERATION | IDEMPOTENCY_CONFLICT | IDEMPOTENCY_RUN_CONFLICT | DEEP_REVIEW_NOT_RETRYABLE
→ 422 CONSENT_REQUIRED | CONSENT_VERSION | ACKNOWLEDGE_REQUIRED | INVALID_FIELDS | DEEP_REVIEW_REPOSITORY_BUDGET | DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES
```

- `DEEP_REVIEW_REPOSITORY_BUDGET`：可核查仓库 >5；不创建 Operation、不调用模型；
- `DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES`：**初次提交**无任何可核查仓库；不创建 Operation、不调用模型；
- `DEEP_REVIEW_NOT_RETRYABLE`：**重试**时无任何符合条件仓库（见 §5.6.3）；不创建 Operation、不调用模型。
- `IDEMPOTENCY_RUN_CONFLICT`：同 Key 但 Server Run 指纹不同（Bundle/模型/Prompt/配置变化）；
- `DEEP_REVIEW_NOT_RETRYABLE`：retry_from 指向的 operation 不含任何可重试仓库（或整体不可重试）。

### 19.2 GET

```text
GET .../deep-review?operation_id&repository_id&requirement_item_id
无 operation_id：
  0 个 → 409 DEEP_REVIEW_NOT_READY
  1 个 running → 200 {operation_id,status:"running",report:null}
  1 个 ready/partial → 200 报告
  1 个 outcome_unknown + 报告存在 → 200 报告（status:"outcome_unknown"，含 unknown usage）
  1 个 outcome_unknown + 无报告 → 409 {operation_id,status,error_code}
  1 个 failed → 409 {operation_id,status,error_code}
  ≥2 个 → 409 DEEP_REVIEW_OPERATION_REQUIRED {operations:[{operation_id,status}]}
指定 operation_id：按该操作状态返回（规则同上）
```

GET 纯只读：不修改状态、不 CAS、不释放锁、不恢复报告、不调模型、不触发后台任务；读到未恢复的过期 Operation 时按持久化状态返回。

### 19.2.1 GET 完整响应契约（v5.2.1）

| 场景 | HTTP | 错误码/响应 | 只读 |
|---|---|---|---|
| 没有任何 Operation | 409 | `DEEP_REVIEW_NOT_READY` | 是 |
| 多个 Operation 且未指定 ID | 409 | `DEEP_REVIEW_OPERATION_REQUIRED`（列 operation_id/status） | 是 |
| 指定 operation_id 不存在 | 404 | `DEEP_REVIEW_OPERATION_NOT_FOUND` | 是 |
| Operation failed | 409 | `DEEP_REVIEW_FAILED` + operation 状态/错误码 | 是 |
| outcome_unknown 无报告 | 409 | `DEEP_REVIEW_OUTCOME_UNKNOWN` | 是 |
| outcome_unknown 有报告 | 200 | 报告，顶层 `status:"outcome_unknown"` | 是 |
| running | 200 | `{operation_id,status:"running",report:null}` | 是 |
| ready/partial | 200 | 完整报告 | 是 |
| repository/requirement 过滤项不存在 | 404 | `RETRIEVAL_FILTER_NOT_FOUND` | 是 |
| 未授权 | 401 | 现有会话错误 | 是 |

GET 任何情况下都不触发 CAS、锁释放、恢复任务或模型调用。

### 19.3 状态机

```
Manifest: active_deep_review (null)
  POST: 参数/授权/Key 校验 → 创建前恢复检查（仅已过期 attempt）
        → 一次 CAS：Operation(status=running) + Key 索引 + 每 Run 活动锁
        → background.add_task(worker) → 202
  Worker: pending → dispatching(attempt_id/issued_at/timeout_at) → 调用
          → 响应落盘 → recorded(校验 attempt_id) → 校验/报告
  Recovery: 启动扫描 + 周期 Worker + POST 前检查；GET 不恢复
  repo: pending → dispatching → recorded → ok|partial|failed|outcome_unknown|skipped
        dispatching --timeout(仓库自己的 timeout_at)--> outcome_unknown
        dispatching --响应文件已写、Manifest 未更新--> 哈希恢复 recorded
操作终态聚合（仅当全部仓库终态）:
  all ok → ready
  any unknown → outcome_unknown（优先于 partial）
  any partial 或 (some ok/partial + failed/skipped) → partial
  none ok/partial, none unknown → failed
终态后在同一次 CAS 中写入 report 引用并释放 active_deep_review
迟到响应只写审计，不改状态
重试: 新 POST + retry_from_operation_id + 新 Key + server run 指纹一致 + acknowledge_unknown(若需)
GET: 只读；返回持久化状态/报告，不触发任何状态变更
```

### 19.4 幂等指纹（最终）

```
client_fingerprint = sha256(canonical_json({
  revision_id, consent.version, consent.accepted,
  retry_from_operation_id, acknowledge_unknown
}))
run_fingerprint = sha256(canonical_json({
  revision_hash, plan_hash, bundle_hash, model, prompt_version, config_hash
}))
处理顺序：Key 记录 → 活动锁 → CAS 创建
```

### 19.5 重试资格（最终）

唯一来源：§5.6 错误码字典。

- **场景 A（同运行指纹）**：新 Key + retry_from；ok/partial 复用；仅 retryable=true 的 failed 与确认后的 outcome_unknown 重新调用；
- **场景 B（新配置）**：新 Key、不带 retry_from、新 run 指纹、全部仓库重跑、不复用旧结果；
- 幂等重放（同 Key 同指纹）返回原 Operation，不等于业务重试；
- `retryable`/`requires_acknowledgement` 由服务端计算，客户端不可传入。

### 19.6 后台 Worker 生命周期（v5.2.1）

现有系统只有 FastAPI `BackgroundTasks`，无持久化队列。3b 复用该机制并在 Manifest 上做持久状态：

1. **POST**：参数/授权/Key 校验 → 创建前恢复检查（仅已过期 attempt）→ 一次 CAS 完成 Operation + Key + 活动锁 → `background.add_task(worker)` → 202 `status:"running"`。
2. **Worker**：读取 pending 仓库 → CAS `pending → dispatching`（写 attempt_id/issued_at/timeout_at/input_hash）→ 调用 → 响应落盘 → CAS `dispatching → recorded`（校验 attempt_id）→ 引用校验/生成报告 → 全部仓库终态后一次 CAS 写终态 + report + 释放活动锁。
3. **Recovery 契约**：
   - **启动扫描**：枚举需要恢复的 Run（见下方枚举限制）。
   - **POST 后、BackgroundTasks 注册前崩溃**：Operation 已持久化、仓库仍 pending；启动扫描/周期 Worker 重新调度 pending，不重复创建。
   - **周期扫描**：重新发现 pending/running；`pending` 重新调度；`recorded` 继续编译；`dispatching` 未超时保留，超时且无响应 → outcome_unknown；响应已落盘 → 哈希恢复 recorded。
   - **多实例**：所有状态转换通过 Manifest CAS 与 attempt_id 条件；一个 attempt 只能被一个执行者认领；未超时 dispatching 任何实例不得接管；恢复不自动重放 unknown。
   - **终态与释放**：一次 CAS 原子写入 Operation 终态、report 引用与 `active_deep_review=null`。
4. **枚举契约（v5.2.2）**：`StorageService` 当前只有 `get/put/delete/read_version/compare_and_swap`，**没有 list/scan**；无法直接枚举 `runs/`。3b 需新增只读 `list_keys(prefix, after=None, limit=1000) -> {keys, next_cursor}`（开发任务，不引入新基础设施）：
   - **LocalStorage**：对 `runs/` 递归 `os.scandir`，按对象键字典序返回；`after` 为上一页最后一个 key；中断后从 `after` 续扫；扫描期间新目录可能出现，完成后**再扫一轮**，直到一整轮无新 key 或达到轮次上限（建议 3 轮）。
   - **S3 ListObjectsV2**：`Prefix="runs/"`、`MaxKeys=limit`、`ContinuationToken=after`；返回 `IsTruncated/NextContinuationToken` 作为 `next_cursor`；**不得只取单页**，必须循环直到 `IsTruncated=false`。
   - **扫描所有 Run**：只解析 `runs/<owner>/<run_id>.json`；读取 Manifest 后仅处理 `active_deep_review != null` 或存在 pending/dispatching/recorded 仓库的 Run。
   - **中断续扫**：Recovery checkpoint 保存 `{cursor, scan_started_at, last_completed_cursor}`；重启后从 `after` 继续；完成一轮后清空 cursor 并记录完成时间。
   - **扫描期间新增 Run**：完成一轮后重新扫描；新增 Run 最迟在下一轮被发现；恢复操作幂等（CAS），重复扫描安全。
   - **多实例**：不依赖“只有一个扫描器”；每个实例可独立扫描，但仓库认领/终态/锁释放全部用 Manifest CAS + attempt_id；同一 attempt 只能一个实例成功；未超时 dispatching 不得接管。
   - 备选（仅当无法扩展 list）：固定键 `deep-review-recovery.json` 登记活跃 Run，但存在“Manifest 已建、登记未写”窗口，必须配合启动/周期/POST 检查，不能单独作为完整方案。
5. **重启后**：丢失的 BackgroundTasks 由启动/周期恢复重新发现；不永久遗留 pending；不重复调用已 recorded 的仓库。
6. **Provider 适配器、Worker、Recovery Worker 均为 3b 新开发任务**，不是现有能力。

### 19.7 Provider 执行确定性（v5.2.2）

- 唯一错误码、状态、retryable、requires_ack、usage 以 **§5.6 唯一错误码字典**为准，本节不重复定义；
- 现有 `call_provider` 无法区分 DNS/Connect/Write/Read，3b 必须新增执行确定性适配器（开发任务）；
- 适配器未实现前，除 401/402 与完整响应的 JSON/截断外，超时/网络/5xx 一律保守 `outcome_unknown`；
- unknown usage `known=false`、token=null；不得自动重放；业务重试需新 Key + retry_from + acknowledge_unknown。

## 20. 逐项关闭状态

| 编号 | 问题 | 状态 | 方案位置 |
|---|---|---|---|
| P0-1 | 字节偏移冲突、行表、换行/空行/多行/行中边界 | 已关闭 | §6、§6.1–6.4 |
| P0-2 | 迟到响应、重试后旧响应、不同 Key 并发、活动锁 | 已关闭 | §5.3、§5.4.1 |
| P0-3 | POST acknowledge_unknown 与幂等 | 已关闭 | §14 |
| P0-4 | 引用与 verdict 强制关联 | 已关闭 | §8.2 |
| P1-1 | 操作最终状态聚合 | 已关闭 | §5.1.1 |
| P1-2 | ALL/ANY 组一致性 | 已关闭 | §9.1 |
| P1-3 | required_features=null 合法性 | 已核实（`list[Nonempty] \| None`） | §3 |
| P1-4 | 输出截断/JSON 不完整 | 已关闭 | §12.1 |
| 补充 | 对象存储无自动 TTL | 已保留为部署前专项 | §10.1 |
| v5-1 | 全仓库终态才聚合；running 覆盖非终态 | 已关闭 | §5.1.1 |
| v5-2 | 每操作最多 5 仓库，超限调用前拒绝 | 已关闭 | §12 |
| v5-3 | Client/Server 两级幂等指纹、先 Key 后锁 | 已关闭 | §5.4.0 |
| v5-4 | error_code → retryable 表 | 已关闭 | §5.6 |
| v5-5 | 启动恢复扫描与锁释放原子性 | 已关闭 | §5.4.2 |
| v5-6 | 活动锁作用域每个 Run 一个 | 已关闭 | §5.4.2 |
| v5-7 | Chunk byte_end 不含换行 + 字节/行号一致性 | 已关闭 | §6.5 |
| v5.1-1 | Operation 状态表统一、终态聚合条件 | 已关闭 | §5.1/§5.1.1 |
| v5.1-2 | 活动锁每 Run 一个 | 已关闭 | §5.4.2/§11.1 |
| v5.1-3 | 仓库级 retryable 与混合重试 | 已关闭 | §5.6.1 |
| v5.1-4 | OUTPUT_TRUNCATED 改为不可重试 | 已关闭 | §5.6/§19.5 |
| v5.1-5 | 错误码与 0/>5 仓库处理 | 已关闭 | §14/§19.1 |
| v5.1-6 | 过期锁的 GET/POST/周期恢复 | 已关闭 | §5.4.2 |
| v5.1-7 | 1–5 仓库与 skipped 统计 | 已关闭 | §11.2/§12 |
| v5.2-1 | 仓库级重试统一、0/>5/无符合条件错误码 | 已关闭 | §5.6/§5.6.3 |
| v5.2-2 | GET 纯只读；恢复仅启动/周期/POST | 已关闭 | §5.4.2/§14/§19.2 |
| v5.2-3 | 幂等重放与业务重试拆分 | 已关闭 | §5.6/§16 #53 |
| v5.2-4 | HTTP 执行确定性分类与保守 unknown | 已关闭（适配器为开发任务） | §5.6/§19.7 |
| v5.2-5 | Worker 生命周期与重启恢复 | 已关闭（Worker 为开发任务） | §19.6 |
| v5.2-6 | 仓库级 attempt 超时时间 | 已关闭 | §11.1/§5.4.2 |
| v5.2-7 | outcome_unknown 报告可读 | 已关闭 | §14/§19.2 |
| v5.2-8 | partial 产品处理 | 已关闭 | §5.6.2 |
| v5.2.1-1 | 多 Operation/Idempotency 索引/原子创建 | 设计已关闭；代码待开发 | §11.1/§19 |
| v5.2.1-2 | 重试错误码统一 | 已关闭 | §5.6.3/§19.1 |
| v5.2.1-3 | failed/outcome_unknown 按执行确定性 | 已关闭 | §5.1/§5.6/§19.7 |
| v5.2.1-4 | partial 新配置全量重跑 | 已关闭 | §5.6.2 |
| v5.2.1-5 | Operation 仅 running，仓库 pending | 已关闭 | §5.1/§19 |
| v5.2.1-6 | retryable 状态表 | 已关闭 | §5.6/§11.1 |
| v5.2.1-7 | Worker/Recovery 契约与枚举限制 | 设计已关闭；代码待开发 | §19.6/§17 |
| v5.2.1-8 | Provider 细分分类 | 已关闭（适配器开发任务） | §19.7 |
| v5.2.1-9 | GET 完整错误契约 | 已关闭 | §14/§19.2.1 |
| v5.2.2-1 | 版本/历史记录 | 已关闭 | 头部/§0.6/§0.7 |
| v5.2.2-2 | 场景 A/B 重试统一 | 已关闭 | §4/§5.2/§5.6/§19.5 |
| v5.2.2-3 | 唯一 Provider 错误码字典 | 已关闭 | §5.6/§19.7 |
| v5.2.2-4 | retryable/requires_ack 服务端字段 | 已关闭 | §5.6/§11.1 |
| v5.2.2-5 | GET 唯一契约 | 已关闭 | §14/§19.2.1 |
| v5.2.2-6 | Recovery 枚举分页/续扫 | 设计已关闭；代码待开发 | §19.6 |
| v5.2.2-7 | 多 Operation Manifest/格式修复 | 已关闭 | §5.1/§11.1 |
| v5.2.2-8 | 测试矩阵修正/新增 | 已关闭 | §16 |
| v5.2.2-9 | 代码核查依据 | 已关闭 | §1.1 |

## 21. 仍存风险

- 模型语义误判与“引用真实但语义不支持”无法由 Python 完全排除，仍需人工复核提示；
- 迟到响应无法确认服务商是否计费，只能记录 unknown，成本对账仍可能不完整；
- 16 chunk / 24k token 可能使部分需求证据不足，需产品接受；
- v1 Bundle 在 trace 或索引不完整时只能拒绝；
- provider 实际上下文/输出限制未在仓库中记录，需官方核实；
- 报告体积与前端渲染需要折叠/分页；
- 数据保留与删除策略未实现，部署前必须单独制定；
- **Worker 与 Recovery Worker 尚未实现**：当前只有 FastAPI BackgroundTasks，启动扫描/周期恢复是 3b 开发任务；
- **Provider 执行确定性适配器尚未实现**：现有 `call_provider` 无法区分“发送前失败”和“已发送后超时”，编码前必须新增；在此之前统一按保守 unknown 处理；
- **StorageService 无 list/scan**：启动扫描需要新增只读 `list_keys(prefix, limit)` 或固定登记对象；这是 3b 开发任务，不是现有能力；
- 多实例恢复依赖 Manifest CAS；未做真实多实例联调；
- outcome_unknown 报告可读依赖报告先落盘；若崩溃发生在报告写入前，只能返回 409，不能展示部分结果；
- 每操作 5 仓库上限意味着未来更多仓库必须重做分批策略；
- 不同运行配置下旧幂等 Key 会 409，用户需使用新 Key；
- 迟到响应/未知计费无法仅靠本地状态完全消除对账缺口。

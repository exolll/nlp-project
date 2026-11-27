# 证据闸门修复记录（slave1，2026-11-27）

> 基线：`slave1` @ `40b0401 实施P0-1前`
> 本次改动：4 个缺陷修复 + 配置对齐 + 回归测试
> 原则：只修实现，不改既有契约语义；历史版本、change_log、三份分析文档全部保留。

---

## 1. 为什么要改

`40b0401` 把"需求文字必须是原文证据的逐字片段"从提取阶段提前到了**确认阶段**（`confirm()` 调 `validate_quality`）。
真实作业跑下来暴露两类误伤：

| 现象 | 根因 |
|---|---|
| 正常抽取也在确认页 422 | `project_topic / project_description / project_type / domain` 天生是概括，几乎不可能是某句原文的子串，却被一并要求逐字 |
| 引文完全正确仍被判 `evidence_not_bound` | `validate_extraction()` 允许 `evidence.requirement_item_id is None`（绑定只由 `item.evidence_ids` 表达），而 `evidence_mismatches()` 要求反向绑定 |
| 失败后无法归因 | prompt 内容已改，`PROMPT_VERSION` 仍是 `requirements-v3`；`validation_diagnostic` 只落库不出口 |
| Planner 归一化警告无人可见 | `source_normalization` 只写进 manifest 的 `planner_operation`，API/前端都不读 |

---

## 2. 改动清单

| # | 文件 | 改动 | 原因 |
|---|---|---|---|
| 1 | `backend/app/services/requirement_quality.py` | 新增 `VERBATIM_FIELDS = BUSINESS_FIELDS[4:]`；`evidence_mismatches()` 只对必需/偏好类字段做逐字校验，概述类字段跳过 `not_verbatim` | 概述字段是总结，逐字要求会常态性阻断确认 |
| 2 | 同上 | 绑定判定改为 `evidence.requirement_item_id in (None, item.requirement_item_id)`，以 `item.evidence_ids` 为权威 | 消除"引文正确但反向字段为空"的误判 |
| 3 | 同上 | `VERSION` → `requirement-quality-v2` | 校验语义变了，输出需可区分 |
| 4 | `backend/app/services/extraction_provider.py` | `PROMPT_VERSION` → `requirements-v4` | prompt 文本在 `40b0401` 已改（新增"忠实原文"段），版本号必须同步 |
| 5 | `backend/app/schemas/requirements.py` | 新增 `ExtractionDiagnostic{validator, field}`；`ExtractionJob` 增加可选 `diagnostic` | 让失败可解释，且不泄露行号/供应商载荷 |
| 6 | `backend/app/services/requirements.py` | 新增 `extraction_view()`：从 `validation_diagnostic` 提取 `validator`/`field` 并清洗；`snapshot()` 与幂等复用路径改用该视图；确认失败文案区分概述类字段 | 失败信息出口统一、可回放 |
| 7 | `backend/app/services/search_routes.py` | `run_view()` 增加 `plan_warnings`（来自 `planner_operation.source_normalization`） | 把"记录了但没人看"的警告接到 API |
| 8 | `frontend/src/requirements.ts` | `ExtractionJob` 增加可选 `diagnostic` | 与服务端契约同步 |
| 9 | `frontend/src/RequirementsPanel.tsx` | 提取记录区展示"上一次校验失败位置" | 用户能看到是哪条规则没过 |
| 10 | `frontend/src/search.ts` | `SearchRun` 增加可选 `plan_warnings` | 同上 |
| 11 | `frontend/src/SearchPanel.tsx` | 展示"规划来源已按实际执行的查询归一化" | 让归一化对用户可见 |
| 12 | `.env.example` | `MAX_TASK_EXTRACTIONS=3 → 10`，并注明全局 `EXTRACTION_MAX_CALLS` 才是真上限 | 代码默认值（`le=10`）此前被 env 覆盖回 3，放宽并未生效 |

---

## 3. 行为变化（before → after）

| 场景 | 之前 | 之后 |
|---|---|---|
| `project_description` 是概括 | 确认 422 `REQUIREMENT_EVIDENCE_MISMATCH` | 确认 200 |
| `required_features` 改写（如"preserve a track ID for each tracked object"） | 确认 422 | 仍然 422（不变） |
| 引文正确、`evidence.requirement_item_id` 为空 | 确认 422 `evidence_not_bound` | 确认 200（`item.evidence_ids` 权威） |
| 证据 id 重复 / 引文缺失 / 文本为空 | 422 | 仍然 422（不变） |
| 提取校验失败 | job 只有错误码 | job 增加 `diagnostic.validator` + `diagnostic.field`（无行号、无载荷） |
| 规划来源与实际查询不一致 | 警告只落 manifest | API `plan_warnings` + 前端提示，执行口径仍以实际查询为准 |
| job 里的 `prompt_version` | `requirements-v3` | `requirements-v4` |

**未被放宽的部分**：`required_features`、`preferred_features`、`programming_languages`、`required_technologies`、`preferred_technologies` 仍然必须逐字落在证据中；否定、偏好、ALL/ANY、覆盖率等提取期校验一条未动。

---

## 4. 历史与契约保留

- **版本历史**：`RequirementRevision` 的 append-only 语义、`change_log` 结构、`parent_revision_id` 链、编辑指纹全部未改。
- **旧数据兼容**：`ExtractionJob.diagnostic` 是可选字段，历史 job 没有该键时读出 `null`；`evidence_mismatches()` 放宽只影响"是否报告不一致"，不删除任何 item/evidence。
- **既有文档**：`docs/ProjectPilot_Requirement_Rewrite_Analysis.md`、`docs/ProjectPilot_Planner_Source_Mismatch_Analysis.md`、
  `docs/ProjectPilot_DeepReview_Requirement_Evidence_Mismatch.md` 三份分析保持原样，本记录只补实施结论。
- **契约变更点唯一的破坏性变化**：`ExtractionJob` 多一个键，已同步更新 `test_frozen_shapes_idempotency_usage_and_restart` 的冻结集合。

---

## 5. 测试

新增 `backend/tests/test_requirement_evidence_gate.py`（4 个用例）：

1. `test_confirm_allows_a_paraphrased_overview_field` — 概述字段改写在确认阶段放行；
2. `test_confirm_still_blocks_a_paraphrased_requirement_field` — 必需字段改写仍被拦（防止过渡放宽）；
3. `test_failed_extraction_exposes_the_sanitised_diagnostic_and_prompt_version` — `diagnostic` 只有 `validator`/`field` 两个键，`prompt_version` 为 `requirements-v4`；
4. `test_run_view_exposes_planner_source_normalisation` — `plan_warnings` 出口。

`backend/tests/test_requirement_quality.py` 新增 3 个用例：

5. 概述字段概括 → 无不一致；
6. 需求字段改写 → 仍报 `not_verbatim`；
7. `requirement_item_id = None` → 视为已绑定。

修改既有用例 1 处：`test_frozen_shapes_idempotency_usage_and_restart` 的 job 冻结键集合加入 `diagnostic`，并断言成功时为 `null`。

运行结果（本机，Python 3.12.10）：

```text
受影响模块（requirements_api / evidence_gate / requirement_quality / extraction_provider / search_routes / query_planner）
278 passed

全量 backend/tests
1164 passed, 1 skipped, 10 failed
```

10 个失败全部是环境原因，与本次改动无关：

- `test_api.py::test_real_subprocess_*` → 沙箱拒绝子进程写临时文件（PermissionError）；
- `test_chroma_store.py::*`（8 项）→ 本机未安装 `chromadb`（ModuleNotFoundError）。

> 说明：本机 pip 在沙箱内无法解包 wheel，本次通过手工下载并解包到 `.pydeps/` 搭建了临时测试环境；仓库依赖声明未改。

---

## 6. 未做 / 建议下一步

1. **前端未跑 `tsc`/`node --test`**：仓库克隆未带 `node_modules`，npm 安装受网络限制；本次前端只做类型可选字段与展示，未改纯函数逻辑。
2. **概述字段仍是自由文本**：若要连"证据覆盖"也保证，应把 `project_description` 改为由证据 span 拼接（P1-6 的同一思路）。
3. **`item.text` 仍是模型输出**：根治方案（句子 ID + 起止索引，服务端切短语）尚未实现，本次只是把失败前移到可诊断的位置。
4. **`validation_diagnostic` 的行号仍只在服务端**：前端只拿到 validator/field，定位到具体条目需要下一步把失败条目 id 一并回传。

---

## 7. 回滚

```bash
git revert --no-edit <本次提交>
```

单点回滚：

- 若只想恢复严格逐字：把 `evidence_mismatches()` 里的 `item.field not in VERBATIM_FIELDS` 分支删掉；
- 若只想恢复反向绑定：把 `candidates` 的比较改回 `== item.requirement_item_id`；
- 若只想恢复旧版本号：`PROMPT_VERSION = 'requirements-v3'`（注意这与 prompt 内容已不符）。

---

## 8. 追加修复（同日，随真实验收暴露）

第一次修复上线后，用真实作业 `Assignment_1 (1).pdf` 继续验收，又暴露出两处"记账错误导致整单失败"，处理如下：

| 提交 | 位置 | 现象 | 改法 |
|---|---|---|---|
| `75b5073` | `requirement_validation.py` `validate_links` | 歧义项引用了他字段的证据 → `EXTRACTION_INVALID`（真实作业命中） | 删除"不确定性证据必须同字段"的检查；保留"证据必须存在且被引用"，条目级字段一致性不变 |
| `f25420c` | `requirement_validation.py` `validate_coverage` | 模型给 `project_type` 写了条目但顶层值为 null → `EXTRACTION_INVALID` | 描述类四字段移出覆盖率契约（只承载证据）；需求类五字段的覆盖率、modality、否定校验不变 |

新增回归用例 3 个（跨字段不确定性 1 个、未知证据 1 个、描述类条目 1 个），需求相关测试 115 项通过。

验收结果与完整证据链见 `docs/ProjectPilot_Acceptance_Record.md`。

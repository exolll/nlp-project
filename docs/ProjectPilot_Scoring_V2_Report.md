# ProjectPilot 100 分制综合评分与确定性排名：实施报告

> 版本：`hard-tier-score-v2`
> 日期：2026-11-25
> 范围：仅排名与前端展示
> 状态：已完成离线实施与验证，等待人工审核
> 约束：未调用真实 API，未新增费用，未修改历史报告，未提交 Git

---

## 1. 修改目标

恢复“硬性条件分级 + 100 分制综合评分 + 证据覆盖率 + 确定性排序”。

此前版本使用：

```text
硬性条件层级 → preferred 有效证据数量 → repository_id
```

第三次真实测试中五个仓库全部为 `needs_review`，preferred 有效证据数均为 0，最终顺序实际上由 `repository_id` 决定，缺少综合匹配能力。

新版本保持硬约束层级不变，只在同一层级内部使用综合评分排序。

---

## 2. 评分字段与现有数据源映射

| 评分维度 | 使用的实际数据 | 数据来源 | T07 第三次运行可用性 |
|---|---|---|---|
| Required Feature Match | `required_features` 的 verdict / 硬约束结果 | `RepositoryReview.requirements` + `hard_constraint_units` | 可用（4 条） |
| Topic Match | `project_topic`、`domain`、`project_description` | 同上（informational 项） | 可用（2 条） |
| Technology Match | `required_technologies` | 同上 | 本次没有该需求 → N/A |
| Programming Language Match | `programming_languages` | 同上 | 本次没有该需求 → N/A |
| Project Type Match | `project_type` | 同上 | 本次没有该需求 → N/A |
| Preferred Match | `preferred_features`、`preferred_technologies` | 同上 | 本次没有该需求 → N/A |
| Activity / Popularity | `stars`、`forks`、`pushed_at` | Manifest 候选 `metadata`（真实 GitHub 元数据） | 可用 |
| 引用有效性 | `Citation` + `RepositoryInput.chunk_ids` | 报告与 Chunk 归属 | 可用 |
| ALL/ANY | `logic_group_results` | Python 聚合结果 | 可用 |

补充事实：

- `EvidenceBundle` 只保存 `full_name/html_url/stars/description/language`；
- `forks`、`pushed_at`、`topics` 只存在于 Manifest 候选元数据；
- 因此由 `deep_review_routes` 把 Manifest 元数据额外传给排名函数，**没有修改 Bundle 结构**；
- `description` / `topics` 可用，但 v1 不参与打分，避免把“描述里出现关键词”当成需求证据。

---

## 3. 实际评分公式

### 3.1 六个需求维度

先把适用需求转成“评价单元”：

- 普通 required / informational / preferred 项：一条一个单元；
- ALL/ANY 硬约束组：整个组一个单元，使用现有 Python 真值表结果；
- prohibited 不参与打分，只参与硬约束层级。

单元得分：

```text
supports     → 100%
contradicts  → 0%
insufficient → 0%（但降低证据覆盖率）
```

维度得分：

```text
dimension_score = 100 × supports / (supports + contradicts + insufficient)
```

没有适用单元 → 该维度 N/A，`score = null`。

### 3.2 Activity / Popularity（5%）

```text
stars_score   = 100 × min(stars, 1000) / 1000
forks_score   = 100 × min(forks, 200) / 200
freshness     = 100 × max(0, 1 − age_days / 365)

activity_score = 0.4 × stars_score
               + 0.3 × forks_score
               + 0.3 × freshness
```

- 参考日期使用报告 `created_at`，并写入 `scored_at`，保证同一报告重复计算一致。
- 缺失字段按 0 计，同时 `missing_data = true`，不会伪装成 N/A。
- 不使用实时时间、不新增 API 请求。

### 3.3 总分

```text
total_score = Σ(适用维度权重 × 维度得分) / Σ(适用维度权重)
```

N/A 维度从分母剔除。T07 只有 Required、Topic、Activity 适用，分母为 `35% + 15% + 5% = 55%`。

### 3.4 证据覆盖率

```text
evidence_coverage = (supports + contradicts) / 全部适用评价单元
```

同时单独记录 supports / contradicts / insufficient 计数。`insufficient` 不计入反证。

### 3.5 最终排序

```text
sort_key = (
    hard_constraint_tier,
    −total_score,
    −evidence_coverage,
    repository_id 字符串
)
```

- 硬约束层级仍然最高优先：`meets_requirements` > `needs_review` > `hard_conflict`。
- 分数再高也不能覆盖层级。
- `preferred_evidence_count` 保留为展示字段，不再作为主排序键。

---

## 4. 修改文件清单

| 文件 | 修改 |
|---|---|
| `backend/app/services/deep_review_models.py` | 新增 `ScoreDimension`、`EvidenceCoverage`；`RepositoryRanking` 增加评分字段；排名版本升级为 `hard-tier-score-v2`，兼容旧版本 |
| `backend/app/services/deep_review_logic.py` | 新增七维权重、评分单元、活动度、覆盖率、解释文本；排序改为 tier → 总分 → 覆盖率 → ID |
| `backend/app/services/deep_review_pipeline.py` | 接收 Manifest 元数据，生成评分排名，`as_of=报告创建时间` |
| `backend/app/services/deep_review_routes.py` | 从 Manifest 读取 `stars/forks/pushed_at` 并传入管线 |
| `backend/tests/test_deep_review_logic.py` | 新增 13 项评分回归测试 |
| `backend/tests/test_deep_review_pipeline.py` | 版本断言更新 |
| `frontend/src/search.ts` | 新评分类型与运行时校验；修正旧排序校验方向错误 |
| `frontend/src/SearchPanel.tsx` | 展示总分、七维得分/权重、覆盖率、解释 |
| `frontend/src/styles.css` | 评分与表格样式 |
| `frontend/search.test.mjs` | 新增评分校验与渲染测试 |

未修改：

- PDF 解析
- 需求提取
- Query Planner
- GitHub 搜索预算
- Embedding / Chroma
- EvidenceBundle 结构
- 数据库表
- 历史运行与历史报告

---

## 5. 每个维度的测试案例

| 要求 | 对应测试 |
|---|---|
| 全部硬条件通过时分数正确 | `test_all_dimensions_applicable_and_passing_gives_full_score` |
| needs_review 按分数区分名次 | `test_needs_review_repositories_are_ordered_by_score` |
| hard_conflict 总分更高也不能超过其他层级 | `test_hard_conflict_stays_last_even_with_higher_score` |
| insufficient 不会变成 contradicts | `test_insufficient_is_not_treated_as_contradiction` |
| preferred 数量不替代综合评分 | `test_preferred_count_does_not_replace_composite_score` |
| ALL/ANY 只计算一次 | `test_all_any_group_is_scored_once` |
| N/A 正确调整权重分母 | `test_not_applicable_dimensions_adjust_denominator` |
| 缺失元数据不崩溃、不虚高 | `test_missing_activity_metadata_is_marked_not_inflated` |
| Stars 压不过必需功能 | `test_stars_cannot_outweigh_required_features` |
| RRF / 相似度不进入评分 | `test_retrieval_similarity_is_not_part_of_score` |
| 同分按覆盖率和仓库 ID | `test_equal_score_uses_coverage_then_repository_id` |
| 相同输入结果完全一致 | `test_scoring_is_repeatable_for_same_inputs` |
| 历史无评分报告兼容 | `test_legacy_ranking_without_score_fields_remains_valid` |
| 前端评分校验与展示 | `scored ranking validates sort key and renders 100-point breakdown` |

原有排名与引用测试全部保留，没有删除或弱化。

---

## 6. 测试结果

| 项目 | 结果 |
|---|---|
| 深审/排名相关测试 | 150 passed |
| 全量后端 | **1152 passed，0 failed**（97.45 秒） |
| 前端 Node 测试 | **35 passed，0 failed** |
| TypeScript | 通过 |
| Vite 生产构建 | 成功（JS 322.62 kB / gzip 100.38 kB） |

---

## 7. 第三次真实报告离线重排前后对比

未调用任何模型，只使用已保存的报告、Bundle 和 Manifest 元数据；原报告文件哈希保持不变。

| 新名次 | 仓库 | 旧名次 | 分数 | 覆盖率 | Required | Topic | Activity |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | marcelolszewski911/transcription-pipeline | 1 | **76.35** | 83.3% | 75.0 | 100.0 | 14.88 |
| 2 | mikeesto/gemini-transcribe | **5** | **61.88** | 66.7% | 50.0 | 100.0 | 30.67 |
| 3 | samuelbradshaw/text-to-timestamps | 3 | **59.19** | 66.7% | 50.0 | 100.0 | 1.05 |
| 4 | agar2995/Transcription-of-audio-video-files-… | **2** | **45.53** | 50.0% | 50.0 | 50.0 | 0.81 |
| 5 | bakaburg1/minutemaker | 4 | **30.12** | 33.3% | 25.0 | 50.0 | 6.30 |

全部仓库仍然是 `needs_review`，没有被升级为推荐；变化只发生在同一层级内部。

关键变化：

- 旧规则下五者首选证据均为 0，实际由仓库 ID 字符串决定顺序。
- 新规则下，mikeesto 的必需功能支持和活跃度都优于 agar，因此从第 5 升到第 2。
- Activity 分数普遍偏低，是因为仓库更新时间和 Stars/Forks 本身有限，且只占 5%。
- 覆盖率：marcel 5/6，mikeesto/samuel 4/6，agar 3/6，bakaburg 2/6。

结果文件：

- `data/acceptance/t07-full-e2e-run3/scoring-recompute.json`
- `data/acceptance/t07-full-e2e-run3/scoring-recompute.py`

---

## 8. 数据不足或无法实现的维度

| 维度 | 状态 |
|---|---|
| Required Feature | 可用 |
| Topic | 可用（本次有 project_topic + description） |
| Technology | 代码已实现，本次 PDF 没有该需求，N/A |
| Language | 代码已实现，本次 PDF 没有该需求，N/A |
| Project Type | 代码已实现，本次 PDF 没有该需求，N/A |
| Preferred | 代码已实现，本次 PDF 没有该需求，N/A |
| Activity | 可用；依赖 Manifest 的 GitHub 元数据，Bundle 本身只有 Stars |

没有发现因数据缺失而导致无法实现的维度。

需要注意：`forks/pushed_at` 不在 Bundle 中，未来若绕过 Manifest 单独使用 Bundle 生成报告，Activity 会被标记 `missing_data` 并以 0 分计入，而不是伪造 N/A。

---

## 9. 是否建议冻结

**建议冻结当前排名版本 `hard-tier-score-v2` 作为比赛 MVP 版本。**

理由：

- 硬约束层级、引用校验、ALL/ANY 规则未被削弱。
- 100 分制评分只作用于同层级内部，不会让冲突项目“高分洗白”。
- 评分完全由 Python 计算，模型不输出总分，RRF/相似度不参与。
- 同输入、同参考日期下结果完全可复现。
- 历史无评分报告仍可正常读取和展示。

保留说明：权重是第一版产品配置，不是统计验证结果；后续若要调权，只应改权重配置，不要改硬约束层级和引用规则。

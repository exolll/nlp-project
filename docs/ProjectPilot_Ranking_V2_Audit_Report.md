# ProjectPilot Ranking V2 最后一轮精简审计报告

> 日期：2026-11-25
> 范围：`hard-tier-score-v2` 排名模块
> 目标：比赛 MVP 最终审核
> 约束：未调用外部 API，未修改历史报告和 Manifest，未提交 Git
> 结论：无阻断问题，建议冻结当前版本

---

## 1. 代码状态核对

`git status` 确认上一轮评分修改真实存在于工作区：

```text
M backend/app/services/deep_review_logic.py
M backend/app/services/deep_review_models.py
M backend/app/services/deep_review_pipeline.py
M backend/app/services/deep_review_routes.py
M backend/tests/test_deep_review_logic.py
M backend/tests/test_deep_review_pipeline.py
M frontend/src/search.ts
M frontend/src/SearchPanel.tsx
M frontend/src/styles.css
M frontend/search.test.mjs
?? docs/ProjectPilot_Scoring_V2_Report.md
```

本轮实际只改了 2 个文件：

| 文件 | 修改 |
|---|---|
| `backend/app/services/deep_review_logic.py` | 修复混合逻辑组可能重复计分的边界问题 |
| `backend/tests/test_deep_review_logic.py` | 新增 3 条必要测试 |

文档、测试结果和实际代码分开记录，不虚报完成情况。

---

## 2. 五个问题逐项审计

### 2.1 同一需求是否被多个评分维度重复计算

**发现问题并修复。**

原实现中，如果一个人工逻辑组同时包含 `prohibited` 和 `required` 成员，评分循环会跳过整组，但没有把组成员标记为“已结算”，随后 required 成员可能被当作普通条目再计一次。

修复方式：

- 所有硬约束成员在进入评分循环时先加入 `grouped` 集合；
- 无论该组是否可评分，后续“非硬约束条目”循环都不会再次计入。

### 2.2 ALL/ANY 是否只计算一次

**是。**

评分直接复用现有 `_hard_constraint_units`：

- 单个 required / prohibited 项 → 一个单元；
- 同一 `logic_group_id` 的成员 → 一个逻辑组单元；
- 使用现有 Python 真值表结果，不重新计算子项。

已有测试与新增测试均覆盖。

边界说明：

- ALL/ANY 聚合只存在于硬约束（required / prohibited）；
- 如果 preferred 被模型赋予 `logic_group_id`，评分按单条独立计分，不做组级聚合；
- 当前 T07 没有这种情况，不构成阻断。

### 2.3 supports、contradicts、insufficient、N/A 的得分与分母

**处理正确，未修改。**

```text
dimension_score = 100 × supports / (supports + contradicts + insufficient)
```

- `supports` → 获得该维度对应分值；
- `contradicts` → 0 分，同时计入证据覆盖率；
- `insufficient` → 0 分，但不算反证，只降低覆盖率；
- N/A 维度 → 不参与总分分母，按剩余适用权重重新归一化；
- 证据覆盖率：

```text
evidence_coverage = (supports + contradicts) / 全部适用单元
```

`insufficient` 没有被解释为明确不满足。

### 2.4 Activity 是否存在越界或缺失数据处理错误

**未发现越界，未修改。**

```text
stars_score = 100 × min(stars, 1000) / 1000
forks_score = 100 × min(forks, 200) / 200
freshness   = 100 × max(0, 1 − age_days / 365)

activity_score = 0.4 × stars_score
               + 0.3 × forks_score
               + 0.3 × freshness
```

- 上限 100，不会出现负值；
- 负数、字符串、非法日期 → 对应分项按 0；
- 缺失字段 → `missing_data = true`，不伪装成 N/A；
- 参考日期固定为报告创建时间，不使用实时时间。

新增边界测试覆盖极大值、负数、非法日期和缺失字段。

### 2.5 相同输入是否始终产生相同排名

**是。**

排序键：

```text
(tier, -total_score, -evidence_coverage, repository_id 字符串)
```

- 总分保留 2 位小数；
- 覆盖率使用确定性除法；
- 参考日期固定为报告 `created_at`；
- 新增“输入仓库顺序反转结果不变”的测试。

---

## 3. 最终评分公式

### 3.1 六个需求维度

```text
dimension_score = 100 × supports / (supports + contradicts + insufficient)
```

### 3.2 总分

```text
total_score = Σ(适用维度权重 × 维度得分) / Σ(适用维度权重)
```

### 3.3 证据覆盖率

```text
evidence_coverage = (supports + contradicts) / 全部适用单元
```

### 3.4 排序

```text
sort_key = (
    hard_constraint_tier,
    -total_score,
    -evidence_coverage,
    repository_id 字符串
)
```

### 3.5 权重

| 维度 | 权重 |
|---|---:|
| Required Feature Match | 35% |
| Topic Match | 15% |
| Technology Match | 15% |
| Programming Language Match | 10% |
| Project Type Match | 10% |
| Preferred Match | 10% |
| Activity / Popularity | 5% |

没有新增评分体系，也没有引入独立匹配率模块。

---

## 4. T07 第三次真实运行离线重排

只读使用已保存的报告、Bundle 和 Manifest 元数据；未调用 DeepSeek、GitHub、Embedding；历史报告哈希未变。

| 名次 | 仓库 | 分数 | 覆盖率 |
|---:|---|---:|---:|
| 1 | marcelolszewski911/transcription-pipeline | 76.35 | 83.3% |
| 2 | mikeesto/gemini-transcribe | 61.88 | 66.7% |
| 3 | samuelbradshaw/text-to-timestamps | 59.19 | 66.7% |
| 4 | agar2995/Transcription-of-audio-video-files-… | 45.53 | 50.0% |
| 5 | bakaburg1/minutemaker | 30.12 | 33.3% |

与上一轮评分结果一致，修复没有改变 T07 的分数和顺序：

| 仓库 | 旧名次 | 新名次 |
|---|---:|---:|
| marcelolszewski911/transcription-pipeline | 1 | 1 |
| mikeesto/gemini-transcribe | 5 | 2 |
| samuelbradshaw/text-to-timestamps | 3 | 3 |
| agar2995/Transcription-of-audio-video-files-… | 2 | 4 |
| bakaburg1/minutemaker | 4 | 5 |

五个仓库仍全部为 `needs_review`，没有被升级为推荐。

结果文件：

- `data/acceptance/t07-full-e2e-run3/scoring-recompute.json`
- `data/acceptance/t07-full-e2e-run3/scoring-recompute.py`

---

## 5. 测试结果

| 项目 | 结果 |
|---|---|
| 全量后端 | **1155 passed，0 failed**（96.30 秒） |
| 前端 Node 测试 | **35 passed，0 failed** |
| TypeScript + Vite 构建 | 成功 |
| 离线重排 | 确定性 true，历史报告未变 |

新增测试：

| 测试 | 覆盖点 |
|---|---|
| `test_mixed_group_with_prohibited_member_is_not_scored_twice` | 混合逻辑组不重复计分 |
| `test_activity_is_clamped_and_missing_values_are_flagged` | Activity 上限、负数、非法值、缺失 |
| `test_repository_input_order_does_not_change_scored_ranking` | 排名确定性 |

原有测试全部保留，没有删除或弱化。

---

## 6. 是否存在阻断问题

**没有阻断比赛 MVP 的问题。**

已知但不阻断的边界：

1. `forks` / `pushed_at` 依赖 Manifest 元数据，Bundle 本身只有 Stars；绕过 Manifest 时 Activity 会标记缺失并按 0 计。
2. preferred 的 ALL/ANY 不做组级聚合；当前需求和 T07 都不涉及。
3. 权重仍是第一版产品配置，不是统计验证结果；建议冻结，不再调。

---

## 7. 修改原因与结论

本轮唯一的代码修改是修复“混合逻辑组可能重复计分”的边界问题。其余四个审计项均验证正确，没有为了体现工作量而改动代码。

当前 Ranking V2 简单、稳定、可解释，能够在比赛中完整演示。建议冻结当前版本，等待人工验收。

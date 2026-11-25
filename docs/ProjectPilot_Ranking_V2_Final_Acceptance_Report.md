# ProjectPilot Ranking V2 最后一轮修复与验收报告

> 版本：`hard-tier-score-v2`
> 日期：2026-11-25
> 范围：排名精度、Activity 边界、异常逻辑组、旧版/新版对比
> 约束：未调用外部 API，未修改历史报告、Bundle 或 Manifest，未提交 Git
> 结论：四个问题已处理，建议正式冻结 Ranking V2

---

## 1. 四个问题是否解决

| 问题 | 状态 |
|---|---|
| P0-1 旧版/新版对比矛盾 | 已查清并给出可复现对比 |
| P0-2 Activity 缺失数据与边界 | 已修复 |
| P0-3 异常 ALL/ANY 逻辑组遗漏 | 已修复，并补回归测试 |
| P1-4 排序精度与显示精度 | 已修复 |

---

## 2. 修改文件与具体内容

### 2.1 `backend/app/services/deep_review_logic.py`

- Activity 改为只对有效子指标重新归一化权重；
- 三个子指标全部缺失时 Activity 标记 N/A，并退出总分分母；
- Activity、维度分、总分都不再提前四舍五入，只做 0–100 边界夹取；
- 新增 `_ranking_sort_key`，排序使用完整精度；
- 异常混合逻辑组计入一个 `insufficient` 覆盖单元，不再静默消失，也不会抬高分数。

### 2.2 `backend/tests/test_deep_review_logic.py`

新增/调整测试：

- Activity 全部缺失 → N/A；
- Activity 部分缺失 → 有效子指标重新归一化；
- 极大值、负数、非法日期、缺失字段；
- 混合 ALL 组、混合 ANY 组的硬约束与覆盖率；
- 完整精度排序，不按显示值排序。

### 2.3 `frontend/src/SearchPanel.tsx`

- 总分与维度分统一显示两位小数（`toFixed(2)`）。

### 2.4 `frontend/search.test.mjs`

- 同步断言两位小数显示。

### 2.5 `data/acceptance/t07-full-e2e-run3/scoring-recompute.py`

- 增加旧版排序规则重建；
- 明确旧版分数不可得；
- 并列输出完整精度与两位小数显示值。

---

## 3. P0-1：旧版与新版排名对比

旧版排名算法真实规则：

```text
tier
→ preferred.evidence_match_count（降序）
→ repository_id
```

使用该规则重建旧顺序，与历史报告中的旧顺序完全一致。

但旧版报告**没有保存数值分数**，因此旧版分数无法恢复。没有推算或伪造历史分数。

| 仓库 | 旧版分数 | 新版完整分数 | 前端显示 | 旧名次 | 新名次 | 变化 |
|---|---:|---:|---:|---:|---:|---:|
| marcelolszewski911/transcription-pipeline | 未记录 | 76.35242839352429 | 76.35 | 1 | 1 | 0 |
| mikeesto/gemini-transcribe | 未记录 | 61.87921544209215 | 61.88 | 5 | 2 | ↑3 |
| samuelbradshaw/text-to-timestamps | 未记录 | 59.18636363636365 | 59.19 | 3 | 3 | 0 |
| agar2995/Transcription-of-audio-video-files-… | 未记录 | 45.52818181818183 | 45.53 | 2 | 4 | ↓2 |
| bakaburg1/minutemaker | 未记录 | 30.11851805728518 | 30.12 | 4 | 5 | ↓1 |

排名变化原因：

- 旧版五者的 preferred 有效证据都是 0，最终实际上由 `repository_id` 字符串排序决定。
- 新版在 `needs_review` 层级内部按综合分排序：
  - mikeesto 的 Required 50.0、Topic 100.0、Activity 30.67，高于 agar 的 Topic 50.0、Activity 0.81，因此升到第 2。
  - bakaburg 的 Required 25.0、Topic 50.0、Activity 6.30，综合分最低，降到第 5。

关于“修复未改变 T07”的表述：

- 上一轮“修复未改变 T07”指的是**混合 prohibited 组修复**，不是“新版与旧版排名相同”；
- `order_stable_since_previous_new_version = true`，确认该边界修复没有改变 T07；
- 旧版与 V2 的顺序变化，是恢复综合评分本身带来的。

矛盾已解释清楚，且没有修改历史报告。

---

## 4. P0-2：Activity 缺失数据处理规则

保留 Activity 5% 总权重和 40 / 30 / 30 子权重。

| 情况 | 处理 |
|---|---|
| Stars 超上限 | 截断到 1000，得分最多 100 |
| Forks 超上限 | 截断到 200，得分最多 100 |
| Freshness 未来日期 | `age_days = 0`，得分最多 100 |
| 负数 / 字符串 / 非法日期 | 视为缺失，不产生负分 |
| 部分缺失 | 只对有效子指标重新归一化 |
| 全部缺失 | Activity 标记 N/A，退出总分分母 |
| 任意字段缺失 | `missing_data = true`，前端可见 |

示例：只有 Stars = 1000 有效时，Activity 得分 = 100，因为权重在有效子指标中重新归一化，而不是被缺失项拉低。

---

## 5. P0-3：异常逻辑组处理规则

对同时包含 required 和 prohibited 的混合组：

1. 组内成员全部标记为“已结算”，不会再次按单条计分。
2. 不分配给任何评分维度。
3. 但仍作为一个 `insufficient` 单元进入证据覆盖率分母，避免“跳过计分反而抬高分数”。
4. 硬约束仍由现有 `_hard_constraint_units` 判断：
   - ALL 混合组推导 fail → `hard_conflict`；
   - ANY 混合组无法验证 → `unknown` → `needs_review`。
5. 没有扩展 Schema，没有改 ALL/ANY 真值表。

测试覆盖 ALL 与 ANY 两种运算符。

---

## 6. P1-4：精度规则

- 后端维度分、总分、Activity 分均使用完整浮点精度；
- 排序不再基于四舍五入后的值；
- 只在 0/100 边界做夹取，防止浮点误差产生 `100.00000000000001`；
- 前端只做显示层两位小数。

关于示例：

`82.341` 按标准四舍五入显示为 `82.34`，不是 `82.35`。  
因此测试改用 `82.3464` 和 `82.3461`：两者都显示 `82.35`，但内部排序由完整精度决定，测试通过。

排序键仍为：

```text
(
    hard_constraint_tier,
    -full_precision_total,
    -full_precision_coverage,
    repository_id 字符串
)
```

---

## 7. 最终评分公式

```text
dimension_score = 100 × supports / (supports + contradicts + insufficient)

total_score = Σ(适用维度权重 × 维度得分) / Σ(适用维度权重)

evidence_coverage = (supports + contradicts) / 全部适用证据单元
```

七维权重：

| 维度 | 权重 |
|---|---:|
| Required Feature Match | 35% |
| Topic Match | 15% |
| Technology Match | 15% |
| Programming Language Match | 10% |
| Project Type Match | 10% |
| Preferred Match | 10% |
| Activity / Popularity | 5% |

没有新增维度、权重或评分算法。

---

## 8. 测试结果

| 项目 | 结果 |
|---|---|
| 全量后端 | **1158 passed，0 failed**（99.97 秒） |
| 前端 Node 测试 | **35 passed，0 failed** |
| TypeScript + Vite 构建 | 成功（322.62 kB / gzip 100.38 kB） |
| T07 离线重算 | 确定性 true |
| 新版顺序与上一轮 V2 对比 | 未变化 |
| 历史报告哈希 | 未变化（`9933a101…ca8719`） |

---

## 9. 尚存问题与风险

1. 旧版排名没有保存分数，历史旧分数永久不可恢复；本轮只重建顺序，没有伪造分数。
2. 混合逻辑组按 `insufficient` 计入覆盖率，但不会获得任何维度分；这是保守处理。
3. preferred 的 ALL/ANY 不做组级聚合，与现有产品规则一致，当前需求和 T07 不涉及。
4. Activity 依赖 Manifest 元数据；绕过 Manifest 仅用 Bundle 时，缺失指标会按上述规则进入 N/A 或重新归一化。

没有阻断比赛 MVP 的问题。

---

## 10. 冻结建议

**建议正式冻结 Ranking V2。**

四个问题均已处理，评分公式未改，硬约束优先规则未改，排序确定性已验证，前端显示与后端精度已分离。当前版本简单、稳定、可解释，适合比赛演示，不再开启新的排名架构设计。

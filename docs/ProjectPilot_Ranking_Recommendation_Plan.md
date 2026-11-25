# ProjectPilot 确定性排名与带引用推荐扩展方案

> 状态：`FROZEN`
>
> 批准日期：2026-11-24
>
> 本方案是 `docs/ProjectPilot_MVP_Implementation_Plan.md`（MVP-FROZEN）的后续业务扩展，不改变已完成的输入、确认、搜索、检索和深度核查主链路。本轮只允许离线开发和测试，不调用外部 API、不修改数据库。

## 1. 目标与边界

在现有 `DeepReviewReport` 中直接增加确定性排名和推荐结果，解决“有逐仓库核查报告，但没有最终推荐顺序”的缺口。

本期不做：

- 不新增模型调用，不重新调用 GitHub、Embedding 或 DeepSeek。
- 不引入权重优化、机器学习二次排序或实时 Stars 等外部指标。
- 不新增数据库表，不重构现有 3a、EvidenceBundle v2 或 DeepReview 主链路。
- 不把 `insufficient` 当成 `null`，也不把证据不足当成通过。
- 不生成无法追溯的推荐文案，不进行新的模型式 Citation Repair。

## 2. 输入与输出

### 2.1 输入

直接复用现有报告中的已校验数据：

- `repositories[].status`
- `repositories[].coverage`
- `repositories[].requirements[]`
  - `modality`
  - `verdict`
  - `hard_constraint_check`
  - `logic_group_id`
  - `citations`
- `repositories[].logic_group_results[]`
- `repositories[].hard_constraint_summary`
- EvidenceBundle v2 中的仓库名称、链接和简介

### 2.2 输出

在 `DeepReviewReport` 中新增可选字段：

```text
ranking: RankingResults | null
```

旧报告没有 `ranking` 时按 `null` 读取；新报告包含完整排名和推荐。报告版本、Manifest 和现有 GET 接口不新增。

## 3. 最终排名规则

### 3.1 可参与排名的仓库

现有 `RepositoryReview.status` 实际枚举为 `completed | failed | unknown | skipped`，不是旧方案中的 `ok | partial | outcome_unknown`。本功能不修改该枚举，按以下映射执行：

| 需求中的状态 | 现有代码状态 | 排名处理 |
|---|---|---|
| `ok` | `completed` | 正常参与排名 |
| `partial` | 无独立 Repository 状态 | 当前等价情况为 `completed` 且覆盖不完整或硬约束存在 `unknown`；最高只能是 `needs_review`，已有明确冲突时仍为 `hard_conflict` |
| `failed` | `failed` | 排除 |
| `outcome_unknown` | `unknown` | 排除 |
| `skipped` | `skipped` | 排除 |

被排除的仓库放入 `excluded_repositories`，保留实际状态、错误代码和排除原因，不能伪装成“不符合”或“待核查”。

### 3.2 第一层：硬约束分层

硬约束优先级最高，preferred 数量不能跨层抵消冲突。

#### 硬约束判定单元

- 有有效 `logic_group_id` 的 required/prohibited 需求：使用现有 ALL/ANY `logic_group_results.result` 作为该组判定单元。
- 无逻辑组的 required/prohibited 需求：使用 `hard_constraint_check`。
- 逻辑组结果缺失或不一致：
  - 任一成员存在明确 `fail`，保守判为冲突；
  - 否则判为待进一步核查。
- informational 和 preferred 不参与硬约束层级。

#### 三层定义

| 层级 | JSON 值 | 规则 |
|---|---|---|
| 符合要求 | `meets_requirements` | 仓库已完成；所有硬约束判定单元为 `pass`；README 覆盖为 `full` |
| 待进一步核查 | `needs_review` | 没有硬约束冲突，但存在 `unknown`、逻辑组无法确定，或 README 覆盖不是 `full` |
| 存在硬约束冲突 | `hard_conflict` | 任一硬约束判定单元为 `fail`，包括 required 被有效证据反驳、prohibited 被有效证据支持，或 ALL/ANY 逻辑组判为 `fail` |

冲突一旦成立，直接进入 `hard_conflict`，不再检查 preferred 数量。

如果需求中没有任何 required 或 prohibited 项，则不存在可核查的硬约束单元：仓库归入 `needs_review`，推荐理由必须明确说明“当前需求没有可核查的硬约束，不能确认项目完全符合要求”。不得把空集合解释为全部通过。

### 3.3 第二层：同层内按 preferred 有效证据数量排序

`preferred_evidence_match_count` 只统计同时满足以下条件的 preferred 需求：

1. `modality == preferred`；
2. `verdict == supports`；
3. `citations` 中至少有一条已通过 Python 校验的引用。

`contradicts` 和 `insufficient` 不计入匹配数量，但分别保留统计，不能混为 `null`。

preferred 摘要状态固定为：

- `supports`：只有支持，没有反证；
- `contradicts`：只有反证，没有支持；
- `insufficient`：有适用需求，但没有支持或反证；
- `mixed`：同时存在 `supports` 和 `contradicts`；
- `null`：没有 preferred 需求，不适用。

原有 `supports_count`、`contradicts_count`、`insufficient_count` 和 `evidence_match_count` 全部保留。摘要状态不能替代逐条 `RequirementReview.verdict`，也不修改原始判定。

同层排序键：

```text
hard_constraint_tier: meets_requirements > needs_review > hard_conflict
preferred_evidence_match_count: 降序
repository_id: 字符串升序
```

不使用总分、权重、Stars、更新时间或实时外部指标。`repository_id` 作为最终稳定排序依据。

### 3.4 状态语义

| 状态 | 含义 |
|---|---|
| `supports` | 有有效 README 证据支持该需求 |
| `contradicts` | 有有效 README 证据反驳该需求 |
| `insufficient` | 已核查但证据不足，不能声称通过，也不能声称功能不存在 |
| `null` | 该维度确实不适用；不生成虚假的 `RequirementReview`，不计入支持或不足 |

现有 `RequirementReview.verdict` 保持 `supports/contradicts/insufficient` 不变。`null` 只用于排名结果中的维度适用性摘要，例如没有 preferred 需求时：

```json
{
  "applicable": false,
  "status": null,
  "supports_count": 0,
  "contradicts_count": 0,
  "insufficient_count": 0,
  "evidence_match_count": 0
}
```

### 3.5 推荐状态

- `meets_requirements` → `recommended`
- `needs_review` → `needs_review`
- `hard_conflict` → `not_recommended`

推荐理由只使用 Python 模板：

- 符合要求：说明硬约束均有有效证据支持，并汇总 preferred 有效证据数量。
- 待核查：说明存在证据不足或覆盖不完整，不声称功能不存在。
- 冲突：指出存在有效硬约束冲突证据，不推荐。
- 每条事实性理由必须绑定同一仓库、同一 Snapshot、同一需求中已校验的 citations；状态说明可以没有引用，但不能据此输出功能断言。
- required/prohibited 理由只引用对应硬约束需求的 citations；preferred 理由只引用对应 preferred 需求的 citations。
- 硬约束冲突必须逐项展示产生冲突的需求和反证引用，不得复制其他需求或其他仓库的引用。

所有推荐仍必须显示限制：仅基于已保存 README，不代表源码审计、运行验证或部署可用性。

## 4. 排名计算示例

假设有 5 个已完成仓库：

| repository_id | 硬约束结果 | preferred 有效证据数 | 层级 | 最终排名 |
|---|---:|---:|---|---:|
| `repo-200` | 全部 pass | 2 | meets_requirements | 2 |
| `repo-100` | 全部 pass | 2 | meets_requirements | 1 |
| `repo-500` | 全部 pass | 0（preferred 不适用） | meets_requirements | 3 |
| `repo-300` | 1 项 required unknown | 5 | needs_review | 4 |
| `repo-400` | 1 项 required contradicts | 9 | hard_conflict | 5 |

说明：

- `repo-100` 和 `repo-200` 同层、同 preferred 数量，按 `repository_id` 升序排列。
- `repo-300` 即使有 5 条 preferred 证据，也不能超过符合要求的仓库。
- `repo-400` 即使有 9 条 preferred 证据，也必须排在冲突层最后。
- `repo-500` 的 preferred 为不适用，不是 `insufficient`，也不能记为通过。

## 5. DeepReviewReport JSON 结构

以下只展示新增字段；现有 `repositories`、`summary`、`warnings`、费用和身份字段保持不变。

```json
{
  "ranking": {
    "ranking_config_version": "hard-tier-preferred-v1",
    "repositories": [
      {
        "repository_id": "repo-100",
        "full_name": "example/task-tracker",
        "html_url": "https://github.com/example/task-tracker",
        "rank": 1,
        "hard_constraint_tier": "meets_requirements",
        "hard_constraint_units": [
          {
            "kind": "required",
            "source_id": "req-required-1",
            "result": "pass"
          },
          {
            "kind": "prohibited",
            "source_id": "req-prohibited-1",
            "result": "pass"
          }
        ],
        "preferred": {
          "applicable": true,
          "status": "supports",
          "supports_count": 2,
          "contradicts_count": 0,
          "insufficient_count": 1,
          "evidence_match_count": 2
        },
        "recommendation": {
          "status": "recommended",
          "reason_code": "HARD_CONSTRAINTS_MET",
          "reasons": [
            {
              "reason_code": "REQUIRED_SUPPORTED",
              "requirement_item_id": "req-required-1",
              "text": "README 中有有效证据支持该必需需求。",
              "citations": [
                {
                  "chunk_id": "snapshot-1:0:abc123",
                  "quote": "Task tracker with React and TypeScript.",
                  "line_start": 1,
                  "line_end": 3,
                  "byte_start": 0,
                  "byte_end": 39
                }
              ]
            }
          ],
          "limitations": [
            "仅基于已保存 README，不代表源码审计、运行验证或部署可用性。"
          ]
        }
      },
      {
        "repository_id": "repo-300",
        "full_name": "example/needs-review",
        "html_url": "https://github.com/example/needs-review",
        "rank": 4,
        "hard_constraint_tier": "needs_review",
        "hard_constraint_units": [
          {
            "kind": "required",
            "source_id": "req-required-1",
            "result": "unknown"
          }
        ],
        "preferred": {
          "applicable": true,
          "status": "supports",
          "supports_count": 5,
          "contradicts_count": 0,
          "insufficient_count": 0,
          "evidence_match_count": 5
        },
        "recommendation": {
          "status": "needs_review",
          "reason_code": "HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT",
          "reasons": [
            {
              "reason_code": "HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT",
              "requirement_item_id": "req-required-1",
              "text": "README 证据不足，无法确认该硬约束。",
              "citations": []
            }
          ],
          "limitations": [
            "证据不足不等于项目不具备该功能。",
            "仅基于已保存 README。"
          ]
        }
      }
    ],
    "excluded_repositories": [
      {
        "repository_id": "repo-900",
        "status": "unknown",
        "reason_code": "REPOSITORY_NOT_COMPLETED"
      }
    ]
  }
}
```

结构约束：

- `ranking` 为可选字段，新报告写入，旧报告缺省为 `null`。
- 只增加 `ranking_config_version`，不增加配置哈希、权重表或分数字段。
- `repositories` 已按最终规则排序，`rank` 从 1 连续递增。
- 每条证据理由通过 `requirement_item_id` 绑定唯一需求；`citations` 必须从该需求的现有 `RequirementReview.citations` 原样复制，不重新生成坐标。
- 状态说明可保留空 `citations`，但不能据此声称功能存在或不存在。
- 不新增推荐运行 ID、Operation 或数据库记录。

## 6. 前端展示示例

在现有“逐仓库深度核查”下方增加一个只读排名区，不增加新的授权或提交按钮。

```text
推荐排名
1. example/task-tracker                    [符合要求]
   偏好需求有效证据：2 / 3
   推荐理由：已保存 README 证据支持全部硬约束，并发现 2 条偏好需求的有效证据。
   引用：README.md 行 1–3
   限制：仅基于 README，不代表源码审计或运行验证。

2. example/needs-review                    [待进一步核查]
   偏好需求有效证据：5 / 5
   推荐理由：未发现明确硬约束冲突，但有 1 项硬约束证据不足。
   提示：证据不足不等于项目不具备该功能。

3. example/conflict                        [存在硬约束冲突]
   推荐理由：README 中存在与硬约束冲突的有效证据，不推荐。
```

展示要求：

- 三层使用不同标签，不用百分比或总分包装。
- preferred 不适用时显示“不适用”，不显示 0%。
- 引用保持纯文本，展示 README 路径、行号和字节范围。
- 冲突仓库即使 preferred 很多，也排在符合要求和待核查仓库之后。
- 刷新仍只调用现有 GET，不触发 POST 或模型调用。

## 7. 实施文件

### 后端

- `backend/app/services/deep_review_models.py`
  - 新增 `RankingResults`、`RepositoryRanking`、`RecommendationResult` 等严格模型。
  - `DeepReviewReport` 新增 `ranking: RankingResults | None = None`。
- `backend/app/services/deep_review_logic.py`
  - 新增硬约束分层、preferred 计数、稳定排序和模板推荐函数。
- `backend/app/services/deep_review_pipeline.py`
  - 在现有报告组装阶段调用排名逻辑，不增加 Provider 调用。
- `backend/tests/test_deep_review_logic.py`
- `backend/tests/test_deep_review_pipeline.py`

### 前端

- `frontend/src/search.ts`
  - 增加排名和推荐类型。
- `frontend/src/SearchPanel.tsx`
  - 增加只读排名展示。
- `frontend/search.test.mjs`
- `frontend/deep-review-browser-demo.mjs`
  - 扩展浏览器验收，不修改历史截图。

现有 `deep_review_routes.py`、Manifest、EvidenceBundle 和数据库表原则上不改。

## 8. 对应测试用例

### 8.1 排名逻辑

1. `hard_conflict` 仓库即使 preferred 有效证据为 99，也排在 `needs_review` 之后。
2. required `supports`、prohibited `supports` 且覆盖 `full`，判为 `meets_requirements`。
3. required `insufficient` 判为 `needs_review`，不能判为 `meets_requirements` 或 `null`。
4. prohibited `contradicts` 表示存在禁止项冲突，判为 `hard_conflict`。
5. 没有任何 required/prohibited 需求时归入 `needs_review`，并生成“没有可核查硬约束”的理由。
6. 当前 partial 等价情况（`completed + coverage != full` 或硬约束 `unknown`）最高为 `needs_review`；明确冲突仍为 `hard_conflict`。
7. ALL 组任一成员 `fail`，组结果为 `fail`，仓库判为冲突。
8. ANY 组任一成员 `pass`，组结果为 `pass`；其他成员 `unknown` 不把组降为待核查。
9. 逻辑组缺失或不一致且成员有 `fail`，保守判为冲突；否则判为待核查。
10. preferred `supports` 但 citations 为空，不计入有效证据数量。
11. preferred 正确汇总为 `supports/contradicts/insufficient/mixed/null`，且不修改逐条 verdict。
12. 没有 preferred 需求时，`applicable=false`、`status=null`、匹配数为 0，不生成 `insufficient`。
13. 同层、同 preferred 数量时，按 `repository_id` 字符串升序；打乱输入顺序结果不变。
14. `failed/unknown/skipped` 仓库进入 `excluded_repositories`，不参与排名。
15. README 覆盖为 `truncated/non_utf8/empty` 时，最高只能到 `needs_review`。

### 8.2 推荐与引用

16. required/prohibited 理由只引用对应硬约束需求的 citations。
17. preferred 理由只引用对应 preferred 需求的 citations。
18. 推荐理由不能引用其他仓库或其他需求的 citations。
19. 没有有效 citation 时，不生成“项目具备某功能”的事实断言。
20. `insufficient` 的推荐文案只能表示证据不足，不能表示功能不存在。
21. `hard_conflict` 必须逐项展示冲突需求和反证引用。
22. 所有推荐都包含“仅基于 README”的限制说明。

### 8.3 兼容性与链路

23. 旧 `DeepReviewReport` 没有 `ranking` 时仍可通过严格模型校验，并序列化为 `ranking: null`。
24. 新报告的 `ranking_config_version` 固定为 `hard-tier-preferred-v1`，不生成配置哈希。
25. 排名过程 Provider 调用次数为 0，不产生新的 GitHub、Embedding 或 DeepSeek 请求。
26. 现有 Manifest、Operation、报告哈希和 GET 只读行为保持兼容。
27. 前端正确展示三层标签、preferred 五态、引用和限制说明。
28. 浏览器刷新只产生 GET，不产生 POST，不重复调用模型。

## 9. 验收标准

- 排名规则完全按本文件执行，无额外权重、分数或实时指标。
- 硬冲突不能被 preferred 数量抵消。
- `supports/contradicts/insufficient/null` 语义严格区分。
- 每条事实性推荐理由都有可复现的 README 引用；无证据时不断言。
- 同输入、同 `ranking_config_version` 得到相同排名。
- 现有后端测试、前端测试、构建和浏览器演示通过。
- 不新增数据库表，不重构现有主链路，不调用外部 API。

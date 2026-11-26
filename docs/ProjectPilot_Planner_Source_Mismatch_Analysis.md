# ProjectPilot Query Planner 来源不一致问题分析与修复建议

> 日期：2026-11-25
> 范围：Query Planner 的 `group.sources` 校验
> 状态：仅分析，未修改代码
> 目标：消除 `PLANNER_INVALID_DRAFT` 中“来源声明与实际查询不一致”的重复失败

---

## 1. 问题现象

用户在完成需求确认后发起搜索运行，前端最终显示：

> 运行已停止自动轮询。已保存证据仍可查看；未发布的报告可稍后手动 GET 刷新。不会自动续跑或新建搜索。

运行的实际终态是：

```text
pipeline_status = interrupted
pipeline_error_code = PLANNER_INVALID_DRAFT
```

本次运行没有进入 GitHub 搜索，也没有调用 Embedding。

涉及的真实运行：

```text
task_id = 789f8f27-f2d2-4489-97f3-9badf90b4db9
run_id  = b4cd68d3-17a8-496c-8f19-8e4cce4b6bcc
planner_error = PLANNER_INVALID_DRAFT
planner_usage = {
  "prompt_tokens": 4046,
  "completion_tokens": 258,
  "total_tokens": 4304
}
```

同类问题此前也出现过：

```text
run_id = 735b68a2-56b9-454c-931f-a117224187b4
planner_error = PLANNER_INVALID_DRAFT
```

---

## 2. 直接原因

模型返回的 B 组草稿声明了 5 个来源：

```text
/required_features/0
/required_features/1
/required_features/2
/required_features/3
/preferred_features/0
```

但 B 组只有 3 条查询，实际使用了前 3 个来源：

```text
/required_features/0
/required_features/1
/required_features/2
```

`/required_features/3` 和 `/preferred_features/0` 被声明为组来源，却没有任何查询使用。

当前校验要求：

```text
group.sources 必须等于该组所有查询 term.source 的并集
```

因此整份草稿被拒绝。

失败草稿的实际结构：

```json
{
  "groups": [
    {
      "name": "A",
      "status": "ready",
      "sources": ["/project_topic", "/project_description"],
      "queries": [
        {"terms": [{"source": "/project_topic", "text": "course lecture QA"}]},
        {"terms": [{"source": "/project_description", "text": "PDF lecture question answering"}]}
      ]
    },
    {
      "name": "B",
      "status": "ready",
      "sources": [
        "/required_features/0",
        "/required_features/1",
        "/required_features/2",
        "/required_features/3",
        "/preferred_features/0"
      ],
      "queries": [
        {"terms": [{"source": "/required_features/0", "text": "PDF upload"}]},
        {"terms": [{"source": "/required_features/1", "text": "document library question answering"}]},
        {"terms": [{"source": "/required_features/2", "text": "passage retrieval citation"}]}
      ]
    },
    {
      "name": "C",
      "status": "skipped",
      "sources": [],
      "queries": [],
      "skip_reason": "没有可用正向来源"
    }
  ]
}
```

---

## 3. 代码位置

主要校验位于：

```text
backend/app/services/query_planner.py
```

关键逻辑：

```python
_require(len(group.sources) == len(set(group.sources)) and set(group.sources) <= allowed)

_require(bool(group.sources) and 1 <= len(group.queries) <= 3
         and group.skip_reason is None)

_require(set(group.sources) == {t.source for q in group.queries for t in q.terms})
```

最后一条要求：

```text
声明来源集合 == 查询实际使用来源集合
```

这是为了确保：

- 计划里不会出现无法解释的来源；
- `source_requirement_pointers` 能真实反映本次搜索覆盖了哪些需求；
- 后续 Bundle 与引用归属可追溯。

校验本身是合理的，问题在于模型输出没有遵守契约。

---

## 4. 为什么反复出现

该输入的结构是：

```text
required_features：4 条
preferred_features：1 条
```

而 B 组最多允许 3 条查询。

模型的常见做法是：

1. 用 3 条查询覆盖 3 条必需功能；
2. 仍然把第 4 条必需功能和偏好功能写进 `sources`；
3. 不生成对应查询。

于是 `sources` 有 5 个，实际使用只有 3 个。

这不是网络故障，也不是随机超时，而是：

```text
“最多 3 条查询”的硬约束
与
“模型倾向于把重要来源全部声明出来”
之间的稳定冲突
```

因此重新提交很可能再次失败。

---

## 5. 可选方案对比

| 方案 | 做法 | 优点 | 风险 | 建议 |
|---|---|---|---|---|
| A. 只改提示词 | 明确要求“不得声明未使用来源” | 不改代码逻辑 | 模型仍可能不遵守 | 可以，但不充分 |
| B. 编译时确定性裁剪 | 用查询实际使用来源重算 `group.sources` | 彻底消除该类失败 | 需要记录警告，避免默默改变 | **推荐** |
| C. 放宽校验 | 允许 `group.sources` 包含未使用来源 | 改动最小 | 计划来源与真实查询不符，破坏可追溯性 | 不推荐 |
| D. 自动重试规划 | 失败后再调用一次模型 | 提高单次成功率 | 增加费用，违反“不自动重试” | 不推荐 |
| E. 丢弃超长查询/来源 | 删除部分查询 | 流程可继续 | 静默降低检索覆盖 | 不推荐 |

推荐组合：**A + B**。

---

## 6. 推荐方案

### 6.1 提示词收紧

在现有提示词中补充：

```text
Do not list a source in group.sources unless at least one query term uses it.
If a group has more positive sources than the 1..3 query limit, omit the
less important source from the plan entirely; never declare it unused.
Prefer omitting a preferred source over declaring it without a query.
```

目的：

- 模型在来源数量超过查询上限时，主动放弃次要来源；
- 不要把“没有查询的来源”挂在组上；
- 不改变现有查询数量、词数和安全规则。

### 6.2 编译阶段确定性裁剪

位置：

```text
backend/app/services/query_planner.py
compile_draft()
```

建议逻辑：

```text
1. 继续校验 group.sources 无重复，且每个来源属于该组允许字段；
2. 对 ready 组计算：
   used_sources = 所有查询 term.source 的并集
3. 校验 used_sources 非空，且 used_sources 是 allowed 的子集；
4. 将 group.sources 设置为 used_sources；
5. 不再要求模型声明的 group.sources 与 used_sources 完全相等。
```

伪代码：

```python
used_sources = sorted({term.source for query in group.queries for term in query.terms})
_require(bool(used_sources) and set(used_sources) <= allowed)
group.sources = used_sources
```

效果：

- 多声明的来源被裁掉；
- 少声明的来源自动补上；
- 查询本身一条都不删；
- 计划中的 `source_requirement_pointers` 永远等于真实查询来源；
- 不改变 Bundle、搜索结果和后续引用。

### 6.3 记录归一化警告

建议在 Manifest 的 `planner_operation` 中追加自由字段：

```json
{
  "source_normalization": [
    {
      "group": "B",
      "declared": [
        "/required_features/0",
        "/required_features/1",
        "/required_features/2",
        "/required_features/3",
        "/preferred_features/0"
      ],
      "used": [
        "/required_features/0",
        "/required_features/1",
        "/required_features/2"
      ]
    }
  ]
}
```

`planner_operation` 是自由字典，不需要修改 Plan Schema、Bundle 或数据库。

警告的意义：

- 说明模型确实漏掉了某些来源；
- 便于人工判断是否需要调整需求或提示词；
- 不把“自动裁剪”伪装成“模型完整覆盖”。

---

## 7. 必须保留的规则

无论采用哪种实现，以下规则不能变：

- 每条查询 1–8 个词；
- 每组 1–3 条查询；
- 整个运行 ≤5 条查询；
- 来源必须属于 A/B/C 对应的字段；
- 禁止项、否定语义、安全字符校验保持不变；
- 不自动重试模型；
- 不因为裁剪来源而删除查询；
- 不把 preferred 强行升级为 required；
- 不新增评分维度或搜索预算。

---

## 8. 建议补充的测试

1. 模型声明来源多于实际使用 → 编译成功，最终 `sources` 只含实际使用；
2. 模型声明来源少于实际使用 → 编译成功，最终 `sources` 自动补齐；
3. 正常组声明与使用完全一致 → 结果与现在完全相同；
4. 查询使用了不属于该组的来源 → 仍然拒绝；
5. 裁剪后组来源为空 → 仍然拒绝；
6. 查询数量、词数、ALL/ANY、安全字符规则不回归。

---

## 9. 风险与边界

- 裁剪只影响规划器声明的来源列表，不影响查询本身；
- 如果模型完全没有为某条必需功能生成查询，这条功能仍然不会被搜索；
- 系统不会伪造查询，也不会自动补模型调用；
- 因此归一化警告很重要，它能让“模型漏搜”变得可见；
- 本方案不增加任何模型调用，不增加 GitHub 或 Embedding 预算。

---

## 10. 验收标准

修复后应满足：

1. 同一份 T07 类似需求不再因为“声明来源多于查询来源”而整单失败；
2. 计划中的组来源与实际查询完全一致；
3. 原有查询数量、词数、安全校验测试全部通过；
4. 全量后端、前端测试和构建通过；
5. 历史报告和 Bundle 不被修改；
6. 不产生额外模型调用。

---

## 11. 结论

最合理的落点是：

```text
提示词要求模型不要声明未使用来源
+ 编译阶段用查询实际来源重算 group.sources
+ Manifest 记录归一化警告
+ 不重试、不删查询、不放宽来源安全校验
```

改动范围很小，集中在 `query_planner.py` 和对应测试，可以从根上消除这一类 `PLANNER_INVALID_DRAFT`。

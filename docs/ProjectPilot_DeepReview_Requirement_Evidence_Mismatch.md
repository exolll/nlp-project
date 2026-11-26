# ProjectPilot 深度核查前置校验失败：需求证据不一致分析

> 日期：2026-11-26
> 范围：深度核查（逐仓库深度核查 MVP）
> 现象：已确认需求的证据校验未通过
> 状态：仅分析，未修改代码

---

## 1. 问题现象

用户已经确认需求版本，也已完成搜索运行和 EvidenceBundle 发布，但点击“逐仓库深度核查”时返回 422：

```text
已确认需求的证据校验未通过
```

日志对应请求：

```text
task_id = 4ce10fb5-c27a-460d-adb9-ea337b03089e
run_id  = 856e997c-ac6e-4f02-9a2b-51b04d3ba1fd
```

深度核查操作没有创建，也没有产生 DeepSeek 调用费用。

---

## 2. 直接原因

深度核查在调用模型前会执行：

```text
validate_quality(revision)
```

其中包含一个逐字证据校验：

```text
需求条目的文字，必须是对应 evidence.original_text 的子串
```

本次已确认版本有两项不满足：

| 条目 | 字段 | 需求文字 | 绑定证据 | 问题 |
|---|---|---|---|---|
| `6afe8f0b-4c2d-4b56-8c81-a735ac57dbb4` | project_description | 为一门课程实现资料问答工具，助教会提供从 Word 导出的 PDF 讲义，**并用讲义中的内容进行现场提问。** | E2：`本次作业要求为一门课程实现资料问答工具。助教会提供从 Word 导出的 PDF 讲义，` | 需求文字比证据多了后半句 |
| `a837ac96-01c1-4ab6-8356-9691b466d00a` | required_features | 允许先在资料库页面导入，再到问答页面选用该资料**库** | E6：`允许先在资料库页面导入，再到问答页面选用该资料` | 需求文字多了一个“库”字 |

校验结果：

```json
{
  "evidence_mismatches": [
    {
      "requirement_item_id": "6afe8f0b-4c2d-4b56-8c81-a735ac57dbb4",
      "field": "project_description",
      "reason": "not_verbatim",
      "evidence_ids": ["E2"]
    },
    {
      "requirement_item_id": "a837ac96-01c1-4ab6-8356-9691b466d00a",
      "field": "required_features",
      "reason": "not_verbatim",
      "evidence_ids": ["E6"]
    }
  ],
  "ok": false
}
```

因此深度核查在模型调用前被拒绝。

---

## 3. 校验规则

代码位于：

```text
backend/app/services/requirement_quality.py
```

核心规则：

```python
text = canonical(item.text)
if not any(text in canonical(evidence.original_text) for evidence in candidates):
    reason = 'not_verbatim'
```

同时要求：

- evidence 必须绑定到当前 requirement_item；
- evidence 不能重复 ID；
- 需求文字不能为空；
- 需求必须有 evidence_ids。

这是为了保证前端展示的引用能真实支撑需求，不是模型随便编一句话。

---

## 4. 为什么确认阶段没有拦住

需求确认阶段检查的是：

- 是否还有未解决歧义；
- ALL/ANY 逻辑是否明确；
- 字段与条目是否覆盖完整。

确认阶段不执行深审的 `not_verbatim` 检查。  
所以版本可以确认成功，但在深审前置校验时失败。

这是两个不同阶段的校验边界，不是确认按钮失效。

---

## 5. 为什么会产生这条不一致

### project_description

模型把相邻两句合并成一条项目描述：

```text
为一门课程实现资料问答工具，助教会提供从 Word 导出的 PDF 讲义，并用讲义中的内容进行现场提问。
```

但只绑定了前半句证据 E2。

### required_features

模型生成的证据 E6 是：

```text
允许先在资料库页面导入，再到问答页面选用该资料
```

而需求条目多了一个“库”字。

修订记录显示 required_features 的文字在提取阶段就已经带着“资料库”，不是用户后来修改造成的。

---

## 6. 修复方案

### 方案 A：修改需求文字，使其被现有证据覆盖（推荐）

1. 点击“新建修订草稿”；
2. 修改两条需求文字：
   - project_description 改为：  
     `本次作业要求为一门课程实现资料问答工具。助教会提供从 Word 导出的 PDF 讲义，`
   - required_features 改为：  
     `允许先在资料库页面导入，再到问答页面选用该资料`
3. 保存为新版本；
4. 重新确认；
5. 新建一次搜索运行；
6. 等 Bundle 就绪后再授权深度核查。

注意：新版本 revision_hash 不同，旧搜索运行的 Bundle 不能直接复用，必须新建搜索运行。

### 方案 B：重新提取

重新提取一次，可能得到证据覆盖完整的新版本。  
但重新提取仍有概率生成“需求略长于证据”的条目。

### 方案 C：换一个更短、更明确的需求材料

对于演示场景，使用条目清晰的文本材料，比直接上传完整作业说明更稳定。

---

## 7. 不建议的做法

- 放宽 `not_verbatim` 校验：会允许前端展示无法追溯的需求；
- 把深审强行绑定旧 Bundle：revision_hash 不一致会被拒绝；
- 修改证据对象绕过校验：接口不提供证据编辑，也不应这样做；
- 反复重试同一个已确认版本：问题不会自动消失。

---

## 8. 结论

这次失败不是搜索、GitHub 或模型服务故障，而是：

```text
已确认需求中有 2 条文字超出了它们绑定的原文证据范围
```

深审按设计拒绝进入模型调用。

最稳妥的修复是：

```text
新建修订 → 把两条需求改成证据能覆盖的文字 → 确认
→ 新建搜索运行 → 再授权深度核查
```

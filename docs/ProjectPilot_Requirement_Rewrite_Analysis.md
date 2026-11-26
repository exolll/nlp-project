# ProjectPilot 需求提取改写问题：原因与解决方案

> 日期：2026-11-26
> 范围：需求提取文本与原文证据不一致
> 状态：仅分析与方案建议，未修改代码
> 目标：减少或消除模型改写导致的需求确认与深度核查失败

---

## 1. 问题现象

用户提取需求后，确认阶段返回：

```text
部分需求文字不是原文证据的逐字片段，不能确认。
REQUIREMENT_EVIDENCE_MISMATCH
```

当前版本的两条失败项：

| 条目 | 需求文字 | 绑定证据 |
|---|---|---|
| `c0b7e578…` | `preserve a track ID for each tracked object across frames` | `The program must read a local video file, detect objects, and preserve a track ID` |
| `fea0ad18…` | `Demonstrate the prototype on a short clip and show the track IDs on the video frames` | `Demonstrate the prototype on a short clip and show the track IDs on the video` |

第一条多了：

```text
for each tracked object across frames
```

第二条多了：

```text
frames
```

模型认为这是同义改写，但系统要求需求文字必须是证据原文的逐字子串，因此拒绝确认。

---

## 2. 为什么会这样

### 2.1 提取任务本身是“总结”，但系统要求“逐字引用”

模型被要求从材料中提取需求。  
它的自然倾向是：

- 归纳；
- 改写；
- 把多个句子合成一条；
- 补充上下文。

而系统为了保证引用可追溯，要求：

```text
item.text 必须是 evidence.original_text 的子串
```

这两种目标天然冲突。

### 2.2 当前 Schema 要求模型重复输出两套表示

模型需要同时输出：

1. `requirements.required_features` 等字段值；
2. `requirement_items[].text`。

两者必须一致，并且都要被证据覆盖。

模型很容易在一边改写，另一边照抄，或者在两处都改写。

### 2.3 Prompt 是软约束，不是强制校验

当前 Prompt 已经写了：

```text
Requirement text must stay faithful to the original material.
Preserve source wording; do not expand, add, infer or merge separate passages.
```

但模型仍可能改写。  
Prompt 只能降低概率，不能保证遵守。

### 2.4 校验被提前到确认阶段后，问题暴露得更早

之前这种不一致会确认成功，直到深度核查才报 422。  
现在确认阶段就会拦截，所以用户更早看到错误，但返工步骤也更早出现。

---

## 3. 三种解决方案

### 方案 A：继续加强 Prompt（只能降低概率）

在现有 Prompt 中增加硬规则：

```text
item.text MUST be an exact, contiguous substring of evidence.original_text.
Copy it character-for-character; never paraphrase, translate, summarize,
reorder, add or remove words, punctuation or case.
If the source sentence has extra context, copy the WHOLE sentence into item.text.
Do not shorten it in one place and expand it in another.
```

并加入反例：

```text
Bad:
  evidence = "The program must read a local video file, detect objects, and preserve a track ID"
  item     = "preserve a track ID for each tracked object across frames"

Good:
  item = "The program must read a local video file, detect objects, and preserve a track ID"
```

优点：

- 不改代码逻辑；
- 不增加模型调用；
- 对明显改写有一定抑制作用。

缺点：

- 仍然是软约束；
- 模型仍可能改写；
- 不能从根本上保证不再出现。

---

### 方案 B：系统强制使用证据原文（能保证不再改写）

在提取完成、保存 revision 之前，做一次确定性规范化：

```text
如果 item.text 不是 evidence.original_text 的子串：
    用 evidence.original_text 替换 item.text
    同步更新 requirements 中对应的字段值
    记录一条 warning
```

效果：

- 模型无论怎么改写，最终需求文字一定是证据原文；
- 确认阶段不再因为 `not_verbatim` 失败；
- 深度核查也不会因此失败；
- 用户在确认前仍可看到最终文字并手动调整。

代价：

- 需求文字可能从“更具体的改写”退回为“证据原句”；
- 如果证据只覆盖半句，需求会变短；
- 字段值可能从简洁的需求变成较长原句；
- 这是自动规范化，需要明确记录 warning。

这是目前让“模型不改写”最可靠的方式。

---

### 方案 C：前端一键“使用证据原文”

在确认失败列表中，每条显示按钮：

```text
使用证据原文
```

点击后：

- 用对应证据的 `original_text` 替换当前需求文字；
- 用户保存新版本；
- 再确认。

优点：

- 不自动修改，用户可控；
- 不增加模型调用；
- 不改变逐字校验。

缺点：

- 仍然需要用户手动点一次；
- 不能从根本上减少模型改写。

---

## 4. 方案对比

| 方案 | 能否根治 | 是否改逻辑 | 用户体验 | 风险 |
|---|---|---|---|---|
| A. 加强 Prompt | 否 | 否 | 略好 | 模型仍可能改写 |
| B. 强制证据原文 | 是 | 是 | 最好 | 需求可能变短、变宽 |
| C. 一键替换 | 否 | 前端小改 | 较好 | 仍需人工操作 |

---

## 5. 推荐方案

### 比赛 MVP 推荐

```text
A + C
```

- Prompt 尽量让模型少改写；
- 失败时用户一键改成证据原文；
- 不改变逐字校验，不新增模型调用。

### 如果希望彻底不再因改写失败

采用：

```text
B
```

但需要接受：

- 需求文字会被规范化为证据原句；
- 更具体的改写会被丢弃；
- 系统需要记录规范化 warning。

---

## 6. 不建议的做法

- 放宽 `not_verbatim` 校验；
- 引入语义相似度替代逐字校验；
- 自动调用模型重写需求；
- 静默修改已确认需求；
- 修改证据原文来迁就模型输出。

这些都会破坏“引用可追溯”这一核心设计。

---

## 7. 当前建议的落地顺序

1. 先在 Prompt 中加入逐字规则与反例（方案 A）；
2. 在确认失败列表加入“使用证据原文”按钮（方案 C）；
3. 如果后续仍频繁出现，再评估方案 B，并对规范化行为加 warning 和前端提示。

---

## 8. 结论

这次失败不是系统 Bug，而是：

```text
模型改写了需求文字
```

系统按设计拒绝确认。

Prompt 只能降低概率。  
要彻底解决，只有两条路：

```text
让用户一键改成证据原文
或
由系统强制使用证据原文
```

当前代码尚未修改，等待确认采用 A + C 还是 B。

# ProjectPilot P0-1：固定需求提取采样参数与真实效果验证报告

> 日期：2026-11-26
> 范围：仅需求提取采样参数
> 测试文件：`D:\作业\NLP\project\video-tracking.pdf`
> PDF SHA256：`ad2264f34cb0113e776ed30d68e4067d9fbb083203943705b84c10650f225ff0`
> 文件大小：1,910 字节
> 状态：代码修改完成，真实 B 组已完成，等待验收

---

## 1. 修改前代码基线

- Git 分支：`slave`
- HEAD：`4e01aa5`（第一次优化）
- 工作区：本轮只修改了提取采样参数与对应测试，并新增本报告；没有覆盖、回滚或清理其他工作。
- 本轮未提交 Git。

需求提取请求构造位置：

```text
backend/app/services/extraction_provider.py::build_payload()
```

修改前请求参数：

```text
model, thinking, response_format, stream, max_tokens, messages
```

没有 `temperature`，没有 `top_p`。

当前提取额度：

```text
会话/全局提取上限：1000 次
每任务提取上限：10 次
本轮开始前全局计数：38
本轮结束后全局计数：41（B 组 3 次）
```

---

## 2. 实际修改文件及 diff

### `backend/app/services/extraction_provider.py`

```diff
     payload = {
         'model': provider.deepseek_model, 'thinking': {'type': 'disabled'},
         'response_format': {'type': 'json_object'}, 'stream': False,
+        'temperature': 0, 'top_p': 1,
         'max_tokens': settings.extraction_max_output_tokens,
```

只影响需求提取请求。  
没有修改 Scanner/Planner/Deep Review 的请求构造。

### `backend/tests/test_requirement_validation.py`

新增断言：

```python
assert payload['temperature'] == 0
assert payload['top_p'] == 1
```

---

## 3. 修改前后请求参数

| 阶段 | temperature | top_p |
|---|---:|---:|
| 修改前 | 未设置 | 未设置 |
| 修改后 | 0 | 1 |

`temperature=0` 与 `top_p=1` 已确认进入提取 HTTP 请求体。

---

## 4. 单元测试

| 测试 | 结果 |
|---|---|
| 提取/规划相关测试 | 198 passed |
| 全量后端 | **1168 passed，0 failed** |
| 前端测试 | 之前 35 passed；本轮未改前端 |
| 前端构建 | 之前成功；本轮未改前端 |

---

## 5. 真实 API 调用记录

### A 组：修改前历史运行（同一 PDF）

| 运行 | 结果 | Token | 说明 |
|---|---|---:|---|
| `74985945-87dc-4e5e-ab41-9fcd67a3e5b1` | failed | 4,805 | `resolve_spans`，`required_features` 重复/定位失败 |
| `9ad9332f-834f-4729-bdc0-4f55cc95ba0c` | completed | 4,553 | 生成 7 条需求，但质量校验有 2 条 `not_verbatim` |

A 组完成版本 `08b3ea2a…`：

- 7 条需求
- 4 条 required_features
- 2 条证据不一致（`not_verbatim`）
- 一次完整质量通过：**否**

### B 组：修改后真实运行（3 次独立任务）

| 运行 | 结果 | 耗时 | 输入 Token | 输出 Token | 总 Token |
|---|---|---:|---:|---:|---:|
| B-1 | failed | 5.094s | 3,310 | 1,262 | 4,572 |
| B-2 | completed | 6.078s | 3,310 | 1,470 | 4,780 |
| B-3 | completed | 5.078s | 3,310 | 1,257 | 4,567 |

B-1 失败位置：

```text
validate_coverage，field=required_features
```

B-2 / B-3 提取完成，但质量校验仍有 `not_verbatim`：

- B-2：3 条不一致（domain、2 条 required_features）
- B-3：3 条不一致（domain、2 条 required_features）

两次完成版本的 7 条需求文字完全相同，说明采样参数提高了这两次的文本一致性；但仍未解决逐字证据问题。

---

## 6. 修改前后对比

| 指标 | A 组（修改前） | B 组（修改后） |
|---|---|---|
| 一次完整质量通过率 | 0/1 完成版本 | 0/2 完成版本 |
| 提取阶段失败 | 1/2 | 1/3 |
| 完成版本条目数 | 7 | 7 / 7 |
| 跨轮文本一致性 | 无法直接比较（历史仅 1 个完成版本） | B-2 与 B-3 完全一致 |
| required_features 数量 | 4 | 2 |
| 证据不一致 | 2 条 | 每轮 3 条 |
| 否定要求召回 | 本次材料无明确否定项 | 本次材料无明确否定项 |
| ALL/ANY | 无 | 无 |

结论：

```text
temperature=0 / top_p=1 让两次完成结果高度一致，
但没有提高“逐字证据校验”的一次通过率。
```

---

## 7. 需求条目及证据差异

B-2 / B-3 完成版本的典型差异：

```text
需求：read a local video file, detect objects, and preserve a track ID for each tracked object across frames
证据：The program must read a local video file, detect objects, and preserve a track ID
```

- 需求省略了证据开头的 `The program must`
- 需求扩写了 `for each tracked object across frames`

另一条：

```text
需求：Demonstrate the prototype on a short clip and show the track IDs on the video frames
证据：Demonstrate the prototype on a short clip and show the track IDs on the video
```

- 需求多出了 `frames`

domain 条目也存在证据不一致。

这说明：

- 模型仍把“提取”理解为“归纳改写”；
- 固定采样参数只减少了随机波动，没有改变这种行为；
- 逐字证据校验仍然会拦截这些结果。

---

## 8. Token 与耗时

B 组：

```text
总输入 Token：3,310 × 3 = 9,930
总输出 Token：1,262 + 1,470 + 1,257 = 3,989
总 Token：13,919
平均耗时：约 5.4 秒/次
```

A 组历史：

```text
成功运行 4,553 tokens
失败运行 4,805 tokens
```

---

## 9. 是否发现新问题

发现两点：

1. **采样参数提高了稳定性，但不解决改写**
   B-2 与 B-3 文本完全一致，但仍然无法通过逐字证据校验。

2. **完成版本的 required_features 数量与 A 组不同**
   A 组 4 条，B 组 2 条，说明需求切分仍不稳定。

没有发现采样参数导致的接口错误或下游回归。

---

## 10. 是否建议继续实施 P0-2

**暂不建议直接实施 P0-2。**

原因：

- P0-1 的目标是降低波动，这一点在两个完成版本上表现明显；
- 但“逐字证据不一致”仍然存在，说明主要瓶颈在需求文字生成方式，不是采样随机性；
- 在这种情况下增加内容级缓存，可能只是缓存住不稳定或不合格的结果。

建议下一步优先级：

1. 先评估 P0-4（否定/改写处理）或 P0-5（诊断修复请求）；
2. 或采用“系统强制使用证据原文”的确定性方案，从根上消除 `not_verbatim`；
3. 内容级缓存 P0-2 暂缓，直到提取结果本身稳定通过质量校验。

---

## 11. 结论

```text
P0-1 代码正确，参数确实进入请求，现有流程无回归。
固定 temperature=0 / top_p=1 提高了两次完成结果的一致性，
但没有解决需求改写与逐字证据不一致的问题。
```

本轮未实施 P0-2 至 P1 的任何内容，未提交 Git，未修改历史数据。

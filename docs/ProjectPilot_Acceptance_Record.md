# 验收记录：需求提取 → 编辑 → 确认（2026-11-27）

> 环境：Windows 11 + Python 3.12.10 + 本地 LocalStorage（`data/local-preview`），无 MySQL，Embedding 关闭。
> 用途：比赛材料中的"真实链路"证据；只记录实际发生的事，未执行的项不勾选。

## 1. 本次链路

| 步骤 | 结果 | 证据 |
|---|---|---|
| 上传 PDF | 成功 | `Assignment_1 (1).pdf`，1 页，正文 2,094 字 |
| 解析与预览 | 成功 | 文档正常返回正文与页码 |
| 需求提取 | 成功（第 5 次尝试） | `extraction_id = f30fbadf-fef2-407c-93d1-b5d7ca713b14`，`status = completed` |
| 模型与用量 | 真实调用 | `deepseek-flash`，`prompt 4,398 / completion 2,590 / total 6,988` token，其中缓存命中 4,224 |
| 版本 1 | `extracted`（待核对） | `revision_id = 0f7f3668-a06c-4f25-acde-607d1073bdf6`，11 条需求项 |
| 编辑 | 成功 | 补充「必需技术」，`change_log = 2` 条 |
| 版本 2 | **`confirmed`** | `revision_id = f9c878bd-f66b-42a5-a9eb-e25a54e6420b`，12 条需求项，`confirmed_at = 2026-11-27T05:19:00Z` |

任务：`task_id = 0401e056-2ae7-4895-b0cd-4b302efb9591`

## 2. 过程中暴露并已修复的问题（按出现顺序）

| # | 现象 | 错误码 / 位置 | 处理 |
|---|---|---|---|
| 1 | 长讲义被输入上限拦住 | `EXTRACTION_INPUT_LIMIT`（`EXTRACTION_MAX_INPUT_TOKENS=20000` 按字节判定，中文 3 倍膨胀） | 配置调到 60000；属于调参，不是 bug |
| 2 | 长讲义模型输出被截断 | `PROVIDER_OUTPUT_LIMIT`（`completion_tokens = 6000` 顶满） | 输出上限调到 8000；仍不足以支持 1 万字以上单篇，列为已知限制 |
| 3 | 确认阶段拦下概述字段的概括 | `REQUIREMENT_EVIDENCE_MISMATCH`（`validate_quality`） | 逐字校验只约束需求类字段（`413c6a6`） |
| 4 | 引文正确却判 `evidence_not_bound` | `evidence_mismatches` 要求反向绑定 | 以 `item.evidence_ids` 为权威绑定（`413c6a6`） |
| 5 | 歧义引用了其他字段的证据导致整单失败 | `EXTRACTION_INVALID`，`validate_links : 107` | 移除该字段一致性检查（`75b5073`） |
| 6 | 描述类字段被纳入覆盖率契约导致整单失败 | `EXTRACTION_INVALID`，`validate_coverage : 63`（`project_type`） | 描述类字段移出覆盖率契约（`f25420c`） |

## 3. 未在本机验证的部分（不得勾选）

- 真实私有用 S3 / OSS 对象存储（本次只用 LocalStorage）；
- MySQL 8 迁移与搜索持久化；
- Embedding 与向量检索（`EMBEDDING_ENABLED=false`）；
- GitHub 搜索、README 证据、深度核对与报告导出；
- 公网部署与多人并发。

## 4. 已知限制（写进材料）

1. **单篇上限**：约 1.3 万字、6 千 token 级输出的文档会触发 `PROVIDER_OUTPUT_LIMIT`；需要分段抽取才能支持长讲义。
2. **概述字段不做逐字校验**：`project_topic/description/type/domain` 允许概括，只有必需/偏好类字段强制逐字。
3. **描述类字段不参与"每个字段恰好一条"契约**：模型多写/漏写描述条目不会失败，但也不保证描述与证据逐字一致。
4. **失败即整单作废的规则仍在**：需求类字段的覆盖率、否定语义、modality 校验保持严格；遇到 `EXTRACTION_INVALID` 时先看「提取记录 → 上一次校验失败位置」。
5. **额度**：开发环境全局累计计数，失败也计数；本次共消耗 5 次，`.env` 已上调到 20 用于调试。

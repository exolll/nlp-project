# ProjectPilot 前端展示层交接文档

> 范围：仅前端。后端业务代码、路由、请求参数、响应结构、数据库、算法与配置**均未改动**。
> 本文覆盖 2026-11-27 完成的三层展示优化（界面 03 / 04 / 05）与界面 06 项目详情页，也说明如何与现有 19 条后端路由联调；同日补充需求确认成功后自动进入阶段 4 的导航修正、界面 03 右侧「GitHub 候选项目」工作台重排，以及深度筛选完成后自动进入阶段 6（保留手动入口）的导航。
> 关联文档：`docs/ProjectPilot_Frontend_Backend_Integration_Todo.md`（接口待办与只读字段依赖）。

---

## 一、结论摘要

| 项 | 结果 |
|---|---|
| 后端契约 | 零改动；未新增接口、未改请求体、未改响应结构 |
| 新增 HTTP 请求 | 无。项目详情打开/关闭/切换页签/需求跳转全部复用已加载数据 |
| 模型调用 | 次数与改造前一致：搜索 1 次、深度核对 1 次、逐仓库核查 ≤5 次；刷新只执行 GET |
| 单元/渲染测试 | `npm test` **66/66 通过**；`npx tsc -b` 通过 |
| 浏览器验收 | 7 组脚本共 **212 项检查全部通过**（含控制台零错误） |
| 截图 | `docs/screenshots/{screen,deep,result2,detail}/` |
| 待后端接口 | 当前展示无阻塞项；可选改进见第五节 |

本次改造的原则是：**普通用户看到结论，想核对时能看到原文与坐标，技术同学能展开完整原始字段**；任何一层都不编造后端未提供的数据。

---

## 二、三层展示规则

| 层级 | 位置 | 内容 | 数据来源 |
|---|---|---|---|
| L1 普通视图 | 默认可见 | 中文结论、状态徽标、指标、优势/缺口、下一步、限制说明 | viewModels 派生；原始枚举经 `statusOf` 映射 |
| L2 证据视图 | 按需展开 / 证据页签 | README 原文、需求编号、行号、字节区间、README 快照链接 | 报告 `citations` 与初筛 `quotes` / `chunks` |
| L3 技术审计 | 默认折叠 `<details>` | Run/Operation/版本/仓库 ID、SHA、哈希、原始枚举、完整时间戳、预算、Token、未通过校验引文 | 报告与运行响应的原始字段，逐项可复制 |

实现约束：

1. 普通视图不出现 UUID、哈希、原始英文枚举；技术字段只存在于折叠审计中，且同一字段不在普通视图重复。
2. 所有审计面板使用统一组件 `AuditDetails`，默认关闭，值以 `<code>` 呈现并提供复制按钮。
3. 折叠状态不改变数据、不触发重新请求；展开也不触发。
4. 结论到证据的跳转只改变页签与滚动位置，不新增请求。

---

## 三、代码结构与职责

| 文件 | 职责 |
|---|---|
| `frontend/src/display/status.ts` | 唯一的中文状态映射表；未知值统一回退「未知状态」，缺失值统一「未提供」 |
| `frontend/src/display/format.ts` | `shortRef` 短引用、`formatStamp` 友好时间（普通视图 `MM-DD HH:mm`，完整时间仅审计） |
| `frontend/src/display/viewModels.ts` | `summarizeRanking` / `summarizeReview` / `summarizeScreening`：把 API 结构转成页面可用模型，同时生成 L1 文案、L2 引文、L3 审计字段 |
| `frontend/src/components/display.tsx` | `StatusBadge`、`SummaryCard`、`MetricGrid`、`EvidenceCard`、`RequirementMatchCard`、`AuditDetails`、`EmptyState`、`TraceabilityPanel` |
| `frontend/src/SearchPanel.tsx` | 阶段 4/5/6 工作台集成；`RankingView`（界面 05）与 `ProjectDetailWorkspace`（界面 06，五页签） |
| `frontend/src/styles.css` | 展示组件、排名卡、详情工作台与审计样式；沿用既有灰紫背景、白卡片、紫色主色体系 |
| `frontend/display.test.mjs` | 映射、语义、格式化、三类摘要、数据完整性/坐标保留测试 |
| `frontend/search.test.mjs` | 运行校验、排名校验、SSR 渲染文案、技术字段与普通视图隔离测试 |

组件不直接依赖 API 响应结构：页面只消费 viewModel；字段适配集中在 `display/viewModels.ts`，后端字段变化时优先改这一层。

---

## 四、中文状态映射与语义红线

### 1. 映射表（`status.ts`）

| 类别 | 原始值 → 中文 |
|---|---|
| run | reserved 已受理 / queued 排队中 / running 进行中 / completed 已完成 / partial 部分完成 / failed 失败 / interrupted 已中断 / not_started 未开始 |
| plan | not_started 未开始 / queued 排队中 / running 规划中 / published 已发布 / partial 部分完成 / failed 规划失败 / interrupted 已中断 |
| search | not_started 未开始 / queued 排队中 / running 搜索中 / completed 搜索完成 / partial 部分完成 / failed 搜索失败 / interrupted 已中断 |
| evidence | not_started 未开始 / not_ready 未发布 / ready 已发布 / published 已发布 / partial 部分发布 / failed 发布失败 |
| retrieval | not_started 未生成 / running 正在生成 / ready 已就绪 / ok 检索完成 / partial 部分就绪 / failed 生成失败 / empty 没有可检索快照 |
| deepPhase | not_started 未授权 / running 正在核对 / ready 已发布 / failed 未完成 / interrupted 已中断 / blocked 完整性校验失败 |
| review | running 正在核查 / completed 已完成 / partial 部分完成 / failed 失败 / unknown 结果未知 |
| verdict | supports 有证据支持 / contradicts 有证据冲突 / insufficient 信息不足 |
| hardCheck | pass 通过 / fail 未通过 / unknown 未知 / not_applicable 不适用 |
| reason | supported_by_citation 有效引文支持 / conflicted_by_citation 有效引文显示冲突 / not_found_in_provided_evidence 所提供证据中未找到 / citation_rejected 引文未通过校验 / model_output_incomplete 模型输出不完整 |
| repository | completed 已完成 / failed 失败 / unknown 结果未知 / skipped 已跳过 |
| coverage | full README 全文 / truncated 证据覆盖不完整 / non_utf8 非 UTF-8 文本 / empty README 为空 |
| availability | available 已保存快照可读 / snapshot 已保存快照 / missing 无可读快照 / unavailable 不可读取 |
| evidenceItem | mention_found 发现相关提及 / unconfirmed 尚未核实 |
| collection | not_started 未采集 / completed 采集完成 / partial 部分采集 / failed 采集失败 / skipped 已跳过 |
| modality | required 必需项 / preferred 偏好项 / prohibited 禁止项 / informational 参考信息 |
| operator | ALL 全部满足 / ANY 任一满足 / AND 全部满足 / not_applicable 不适用 |
| tier | meets_requirements 符合要求 / needs_review 待进一步核查 / hard_conflict 存在硬约束冲突 |
| recommendation | recommended 推荐 / needs_review 待进一步核查 / not_recommended 不推荐 |
| preferred | supports 有支持 / contradicts 有反证 / insufficient 证据不足 / mixed 支持与反证并存 |
| rankingRule | hard-tier-preferred-v1 硬约束优先 + 偏好证据（v1） / hard-tier-score-v2 硬约束优先 + 加权得分（v2） |
| unitKind | required 必需项 / prohibited 禁止项 / logic_group 逻辑组 |
| unitResult | pass 通过 / fail 未通过 / unknown 未知 |
| field | project_topic 项目主题 / project_description 项目描述 / project_type 项目类型 / domain 领域 / required_features 必需功能 / preferred_features 偏好功能 / programming_languages 编程语言 / required_technologies 必需技术 / preferred_technologies 偏好技术 |

### 2. 禁止的语义反转（有测试守护）

| 原始状态 | 允许表达 | 禁止表达 |
|---|---|---|
| `mention_found` | 发现相关提及 | 已满足 / 符合要求 |
| `unconfirmed` | 尚未核实 | 不满足 / 不符合 |
| `insufficient` | 信息不足 | 不符合 / 未通过 |
| 仓库未参与排名 | 未参与排名 | 不符合需求 / 不推荐 |
| 排名第 N | 排名第 N / 首选（仅 recommended） | 满足全部需求 |
| 字段缺失 | 未提供 | 用推算值替代 |
| 未知枚举 | 未知状态（保留 raw 供审计） | 猜测成已知状态 |

同时明确写入文案的口径：

- 「信息不足不等于项目不具备该功能。」
- 「无引文不等于需求不满足，仍需查看逐条需求证据。」
- 「排名不等于满足；未参与排名不等于不符合需求。」
- 「仅基于已保存 README，不代表源码审计、代码运行验证或部署可用性。」

---

## 五、页面行为与数据来源

### 0. 界面 02：需求确认（阶段 3）

- 「确认此版本」成功后自动切到阶段 4「GitHub 初筛」：先刷新阶段信号，再用 `history.replaceState` 更新路由并记录阶段，不重新打开任务、不新增请求。
- 已确认版本在底部操作栏保留 `进入 GitHub 初筛 →`，方便从阶段 3、历史版本或刷新恢复后再次进入；确认与进入初筛都不会自动发起搜索，搜索仍需在阶段 4 单独勾选授权。
- 历史版本只读：后端编辑与确认接口都只接受最新版本（历史版本返回 `409 STALE_REVISION`），因此历史版本页明确标注「仅最新版本可确认 / 历史版本不可保存」，并提供「回到最新版本」；切换版本后提示按所选版本状态更新，不沿用上一版本的成功提示。若产品需要按旧版本内容恢复，需后端新增复制接口（见接口待办 #9）。
- 截图：`docs/screenshots/requirements/04-confirmed-next-step.png`（已确认状态与下一步按钮）、`05-confirm-auto-advance.png`（确认后自动进入初筛，尚未授权搜索）、`06-history-readonly.png`（历史版本只读与仅最新版本可确认）。

### 1. 界面 03：GitHub 初筛（阶段 4）

- 顶部总览：`04 / INITIAL SCREENING` +「GitHub 候选项目」+「初筛完成」（仅证据发布后显示）；三块统计全部来自真实字段：
  - 检索仓库 = `candidateCount`；
  - 有检索片段 = `retrievalChunkCount > 0` 的仓库数；
  - 发现相关提及 = 已发布证据的 `mentioned`（未发布显示 `—`，不按 0 结论）。
- 筛选结果：卡片 / 表格切换为本地视图状态；排序固定为「检索片段数 ↓」，同分再按提及数与仓库编号，避免伪造顺序。
- 后端没有匹配度评分、初筛通过率、推荐深筛或项目选择接口，因此**不渲染 94 匹配度、通过率或勾选框**；排序提示明确「后端未提供评分，仅按片段数排序」。
- 候选卡片：仓库名 / URL / 描述 / 语言 / Stars、可用性 / 检索 / 覆盖徽标、需求 / 提及 / 未核实 / 片段计数；「查看初筛依据」展开逐需求引文，技术审计默认折叠。
- 表格行内「查看初筛依据」在表格下方展开同一张卡片，不发起新请求。
- 底部操作栏：`候选 N 个` + 阶段 5 单独授权说明；「进入深度筛选 →」仅做阶段导航（先刷新只读信号，不新增 POST），证据未发布时禁用，不提供项目勾选。
- 未变：左栏授权、一次搜索 POST、刷新仅 GET、检索片段与运行审计的原始字段入口。
- 响应式：1000 / 760 / 432 px 均无页面横向溢出；≤920 px 改为上下布局，表格在卡片内横向滚动，底部操作栏保持可见。
- 截图：`docs/screenshots/screen/02-stage4-results.png`（总览）、`08-stage4-table.png`（表格）、`09-stage4-cards.png`（卡片）、`06-stage4-entry.png`（底部操作栏）、`07-stage5-entry.png`（进入阶段 5 后的首个界面）。

### 2. 界面 04：深度筛选（阶段 5）

- 深度核对报告：完成/部分/未知仓库、需求统计、中文推荐与限制说明。
- 逐仓库深度核查：友好名称、结论、优势/缺口、待复核数量、下一步建议；逐需求引文与审计。
- 授权与追溯：授权时间、Operation ID、报告哈希、模型、Prompt 版本、Token 用量；全部默认折叠。
- 完成导航（2026-11-27）：本窗口内刚授权并完成的逐仓库核查，在 `ranking` 首次出现时自动切到阶段 6「最终结果」，每个 Operation 只自动跳转一次；跳转前只刷新既有阶段信号 `GET`，不重发 `POST`、不重复模型调用。已发布报告再次进入阶段 5 不自动跳走，底部操作栏保留「查看最终结果 →」手动入口；无 `ranking`（仅报告或未发布）时按钮禁用并说明原因，不伪造跳转。
- 截图：`docs/screenshots/deep/08-stage6-auto.png`（自动进入最终结果）、`09-stage6-manual.png`（从阶段 5 手动进入）。

### 3. 界面 05：最终推荐结果（阶段 6）

- 左栏：推荐结论与仓库名、参与/推荐/待核查/不推荐/未参与数量、友好规则名、报告时间、分区导航。
- 推荐结论区：首选摘要、五项指标、确定性规则说明、排名卡片。
- 排名卡片：名次、仓库编号/名称/链接、层级与推荐徽标、推荐理由、偏好统计（含 `有效证据 X / Y`）、硬约束单元、覆盖率/综合得分（仅后端提供时）、限制、评分明细。
- 推荐依据：理由 → 需求编号 → README 引文 → 引文技术字段（Chunk、行区间、字节区间、路径）。
- 报告与授权溯源：友好状态 + 默认折叠的授权/报告/排名审计。
- 排除仓库：保留原始状态/错误代码，明确未参与排名语义。

### 4. 界面 06：项目详情（阶段 6 内页）

打开方式：排名卡片「查看项目详情」；关闭后回到阶段 6 列表，不重新请求。

| 页签 | 内容 |
|---|---|
| 综合分析 | 结论摘要、推荐/层级、五项指标、优势、缺口、下一步、排名与偏好证据、页签快捷入口 |
| 需求匹配 | 逐需求卡片：字段、约束强度、硬约束检查、引文/未通过校验数量；`查看证据（N）` 跳到证据页签 |
| 证据核查 | 按需求分组展示 README 引文与坐标；目标需求组高亮并滚动到可视区；无引文给出口径说明 |
| 技术分析 | 得分与覆盖率口径、硬约束检查、偏好统计、评分明细、检索证据分布、README 快照；不出现原始 ID/哈希 |
| 技术审计 | 仓库审计、排名行审计、排名规则审计、初筛审计、逐需求审计；全部默认折叠、可复制 |

### 5. 派生规则

- **友好名称**：`ranking.full_name` → 初筛 `repository.full_name` → 「未命名仓库」。
- **仓库编号**：按当前报告/检索中的仓库顺序生成 `仓库 A/B/C…`；同一 run 内稳定。
- **需求编号**：按报告/证据 `requirement_item_id` 首次出现顺序生成 `需求 1/2/3…`；原始 ID 仅出现在审计。
- **时间**：普通视图 `MM-DD HH:mm`（缺失回退报告时间或「尚未发布」）；完整 ISO 时间只在审计。
- **排名得分**：`hard-tier-score-v2` 且后端提供 `total_score / evidence_coverage / score_breakdown` 时展示；`hard-tier-preferred-v1` 明确「当前规则不产生百分制」，不显示 0 分或假覆盖率。
- **得分方向**：检索距离字段按后端 `score_direction` 原样展示，不在前端反转大小含义。
- **硬约束单元**：单成员单元显示「必需项 · 需求 5 · 通过」，不重复成「需求 5（需求 5）」；逻辑组显示「逻辑组（需求 3、需求 4）」。

---

## 六、请求与业务行为（与改版前一致）

| 行为 | 现状 |
|---|---|
| 搜索 | 用户勾选授权后 `POST .../search-runs` 恰好 1 次，沿用 sessionStorage 幂等键 |
| 深度核对 | 显式授权后 `POST .../deep-check` 1 次 |
| 逐仓库核查 | 显式授权后 `POST .../deep-review` 1 次；后端最多 5 次仓库级模型调用 |
| 深度筛选完成导航 | 本窗口内授权完成的核查在 `ranking` 首次出现时自动进入阶段 6；先刷新既有阶段信号（GET），不新增 POST；已发布报告重新进入阶段 5 时仅保留手动入口 |
| 刷新 / 切换阶段 / 切换页签 / 打开详情 | 只执行既有 GET；不重复 POST/模型调用 |
| 确认需求版本 | `POST .../confirm` 1 次；成功后仅前端切换到阶段 4，不重新打开任务、不发起搜索 |
| 需求→证据跳转 | 纯前端滚动与高亮，无请求 |
| Mock | 仅 `npm run dev:mock` 本地开发/测试；响应带 `X-ProjectPilot-Mock`；不作为真实结果交付 |
| localStorage | 只存草稿文本/UI 偏好/任务与 run 标识；不存大文件、API Key、凭据 |

浏览器验收记录（临时 playwright-core + 系统 Edge，1440×900，zh-CN）：

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `screen2.mjs` | 阶段 4 初筛：布局、授权、请求次数、候选总览、卡片/表格、初筛依据、阶段 5 入口、审计、滚动 | 42/42 |
| `deep2.mjs` | 阶段 5 深度：两类授权、报告、完成后自动进入阶段 6、返回阶段 5 不二次跳转、证据、审计、刷新、手动最终结果入口 | 34/34 |
| `result2.mjs` | 阶段 6 最终结果：排名、证据、审计、刷新、阶段切换 | 44/44 |
| `detail.mjs` | 项目详情：五页签、跳转、无额外请求、返回 | 29/29 |
| `module3.mjs` | 界面 02 需求确认回归 | 29/29 |
| `saveconfirm.mjs` | 保存/确认接口、历史版本只读提示、确认后自动进入阶段 4、下一步按钮与请求次数 | 21/21 |
| `screen4-responsive.mjs` | 阶段 4 在 1000 / 760 / 432 px 的分栏、横向溢出、底栏与表格滚动；432 px 阶段 5 底栏不溢出 | 13/13 |

以上脚本位于本机临时验证目录（`%TEMP%\pp-shot`），不属于仓库源码；截图已保存到 `docs/screenshots/` 供复核。旧的 `search.mjs` / `deep.mjs` / `result.mjs` 已被 `screen2.mjs` / `deep2.mjs` / `result2.mjs` 取代（旧脚本仍断言「原始字段直出」的旧界面），请勿再用旧断言验收新界面。

改造前后截图对照（同一 Mock 数据，1440×900）：

| 页面 | 改造前 | 改造后 |
|---|---|---|
| 界面 03 GitHub 初筛 | `docs/screenshots/search/01-first-screen.png`（原始字段墙、仓库 ID 直出） | `docs/screenshots/screen/02-stage4-results.png`、`08-stage4-table.png`、`09-stage4-cards.png` |
| 界面 04 深度筛选 | `docs/screenshots/deep/01-deep-check-report.png`、`02-review-report.png` | `docs/screenshots/deep/05-stage5-report.png`、`06-stage5-evidence.png`、`08-stage6-auto.png`、`09-stage6-manual.png` |
| 界面 05 最终推荐结果 | `docs/screenshots/result/00-result-first-screen.png`、`01-ranking-scrolled.png` | `docs/screenshots/result2/01-stage6-overview.png`、`02-stage6-evidence.png` |
| 界面 06 项目详情 | 无（本次新增） | `docs/screenshots/detail/01`–`05`（综合/需求/证据/分析/审计） |

---

## 七、待后端接口支持与联调约束

### 1. 当前无阻塞项

本次展示优化没有要求新增接口。现有 deep-review、evidence、retrieval 响应已覆盖所有展示字段。

### 2. 字段稳定性要求（联调不得删改）

- 同一 run 内 `requirement_items[]` / `requirements[]` 的顺序稳定（影响「需求 N」编号）。
- `citations[]` 恒含 `chunk_id / quote / line_start / line_end / byte_start / byte_end`。
- `ranking.scored_at` 建议恒返回；缺失时前端回退报告 `created_at`。
- `ranking.repositories[].full_name` 建议保留；缺失时回退初筛仓库名。
- `excluded_repositories[]` 保留 `status / error_code / reason_code`。
- 可选字段只增不改；v1 报告允许没有 `total_score / evidence_coverage / score_breakdown`。

### 3. 可选改进（「待后端接口支持」，非阻塞）

1. **详情页自包含**：在 deep-review 报告 `repositories[]` 增加可选 `repository_metadata`（语言、Stars、描述），可让详情页仅凭 deep-review 展示元数据；当前复用初筛 retrieval，缺失时安全隐藏。
2. **评分时间**：`ranking.scored_at` 恒返回，避免普通视图回退到报告创建时间。
3. **引文坐标**：所有引文提供完整行号/字节区间，否则 L2 只能展示文本、无法逐行核对。
4. **阶段 4 评分与选择**：逐仓库匹配度、初筛通过/推荐深筛与项目勾选均无现有字段，当前不渲染、不伪造；若后端补充，可替换「检索片段数 ↓」排序与底部操作栏文案（详见接口待办第 10 项）。

详细字段依赖见 `docs/ProjectPilot_Frontend_Backend_Integration_Todo.md` 第五节。

---

## 八、已知限制

1. 所有证据结论仅基于已保存的 README，不代表源码审计、运行验证或部署可用性；界面已明示。
2. v1 排名规则不产生百分制得分或覆盖率，界面按规则如实说明，不推断分数。
3. 项目详情左栏的语言/Stars 来自同 run 初筛检索；历史 run 的检索缺失时该项不显示，且不会额外请求 GitHub。
4. 普通视图时间省略年份；需要完整时间时展开技术审计。
5. 若后端在同一 run 内改变需求数组顺序，「需求 N」编号会重排；数据不丢失，但历史截图编号可能与新编号不同。
6. 浏览器验收脚本未纳入仓库依赖；需要重跑时，安装 `playwright-core` 并使用本机 Edge 即可复现（脚本逻辑见验收记录）。
7. 阶段 4 不提供匹配度、初筛通过率或项目勾选：后端没有对应字段与选择参数，界面改用真实计数与「检索片段数 ↓」排序，并明确说明进入深度筛选不做预选；不把 RRF 片段分数换算成仓库匹配度。
8. 阶段 5 的自动跳转只针对本窗口内刚授权、随后完成的逐仓库核查：页面刷新后若报告已完成，不会自动把用户从阶段 5 弹走，而是显示「查看最终结果 →」手动入口；报告已发布但无 `ranking` 时停留在阶段 5 并禁用入口，不伪造阶段 6 可进入状态。

---

## 九、变更文件清单

### 修改

- `frontend/src/SearchPanel.tsx`：界面 03/05/06 展示重构、排名卡、详情工作台、需求跳转、审计接入；阶段 5 完成后的自动/手动最终结果导航。
- `frontend/src/main.tsx`：阶段 5/6 入口先刷新只读阶段信号再切换，不新增请求。
- `frontend/src/RequirementsPanel.tsx`：向搜索面板透传阶段 5/6 导航回调。
- `frontend/src/styles.css`：展示组件、排名、详情、审计与隐藏状态样式。
- `frontend/search.test.mjs`：新排名 UI 文案、硬约束去重、原始字段与审计断言。
- `docs/ProjectPilot_Frontend_Backend_Integration_Todo.md`：新增展示层只读字段依赖、界面 05/06 行为、字段稳定性要求。
- `docs/screenshots/requirements/01`–`03`：重跑界面 02 回归时更新（同一验收脚本生成）。

### 新增

- `frontend/src/display/status.ts`
- `frontend/src/display/format.ts`
- `frontend/src/display/viewModels.ts`
- `frontend/src/components/display.tsx`
- `frontend/display.test.mjs`
- `docs/ProjectPilot_Frontend_Display_Handoff.md`（本文）
- `docs/screenshots/screen/`（阶段 4）
- `docs/screenshots/deep/04-09`（阶段 5 新界面、完成后的最终结果自动/手动入口）
- `docs/screenshots/result2/`（阶段 6 新界面）
- `docs/screenshots/detail/`（项目详情五页签）

### 未改动

- `backend/**` 全部文件（路由、模型、算法、配置、SQL 均未触碰）。
- 既有请求方法、路径、请求体、响应结构、幂等键策略、模型调用次数。

---

## 十、运行与验收命令

```bash
# 前端目录
cd frontend
npm install
npx tsc -b
npm test          # 66/66

# 本地演示（Vite 5173 + Mock API 8787）
npm run dev:mock
```

人工验收建议路径：

1. 输入课程项目需求 → 确认需求 → 进入「GitHub 初筛」，检查普通视图无 UUID、授权后仅一次 POST、刷新只 GET。
2. 进入「深度筛选」，分别授权深度核对与逐仓库核查；逐仓库核查完成后应自动进入「最终结果」，返回阶段 5 时不应再次自动跳走，并可展开需求证据与技术审计。
3. 点击阶段 5 底部「查看最终结果 →」可手动进入阶段 6；检查排名卡、推荐依据、引文坐标、审计折叠；刷新页面确认只 GET 且排名仍在。
4. 点击「查看项目详情」，依次检查五个页签；从「需求匹配」点击「查看证据（N）」确认跳到高亮证据组；返回列表确认无额外请求。
5. 检查控制台无页面错误。

---

## 十一、交接备注

- 后续如果后端调整字段，优先修改 `frontend/src/display/viewModels.ts` 与 `frontend/src/display/status.ts`，不要把新字段直接塞进页面组件。
- 新状态必须先补映射与测试，未知值保持「未知状态」，不得默认成成功或失败。
- 新增展示需求时，先判断属于 L1/L2/L3 哪一层；原始技术字段不得出现在 L1。
- Mock 数据只能用于开发与验收；不得把 Mock 截图或文案当作真实后端结果交付。
- 本项目当前未提交 Git；提交前请确认只包含前端、测试、文档与截图，不包含 `backend/**`。
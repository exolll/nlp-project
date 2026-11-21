# ProjectPilot 项目实施分析与开发计划

> 版本：StudyHack 比赛优先版，需求提取与确认阶段修订。状态：无数据库输入、真实DeepSeek提取、证据校验、编辑版本及单独确认已实现；本地验收记录见下。生产S3及后续能力另行验收。  
> 产品基准：根目录 `ProjectPilot_项目任务书.md` V1.2；历史已完整阅读。最新用户确认：MySQL仅用于后续GitHub项目搜索相关数据；输入阶段不使用MySQL或SQLite，会话/任务/文档/限速使用文件对象存储。当前首先交付Qoder StudyHack比赛；D10暂缓。任务书原文件本轮不修改。  
> 最终目标：2026年11月30日18:00前完成公开部署、可用产品、演示视频和参赛材料。暂按北京时间 UTC+8 排期；必须核对比赛官方时区和提交规则，不将用户给定目标冒充已核实的官方规则。  
> 保留现有技术架构、12字段 Requirement JSON、完整产品链路和单人后端模式。本文新增的具体参数是待批准的比赛初始配置，不是已经测试通过或确认的最终标准。

## 实施前状态与本轮范围

首次检查时工作区仅有任务书和 Git 仓库，未有前后端、数据库、依赖锁或运行配置；随后创建了本计划。首次实测 Python 3.12.1、Node.js v24.15.0、npm 11.12.1；全局有 FastAPI/Pydantic/ChromaDB/SQLAlchemy 等包，未安装 PyMuPDF。这些是历史检查结果，不等于项目依赖环境已就绪。MySQL 服务、云资源、账号权限、模型余额及公网连通性尚未验证。

用户已批准仅开发并测试需求提取与确认阶段，并授权将提供的DeepSeek密钥配置到Git忽略的根目录 `.env`。本轮不实现GitHub搜索、Embedding、排序、推荐、数据库迁移或部署。历史计划和排期保留作基线，当前实现与验证范围以下列记录为准。

历史输入阶段已保留 `backend/app/schemas/documents.py` 契约并替换数据库持久化，72项后端测试及正常LocalStorage浏览器五输入/来源/重启刷新恢复通过。S3只有Stub契约验证，未验证真实桶权限或服务；生产资源配额与自动清理仍待完成。

### 2026-11-21 当前阶段本地验收记录

- 已实现原12字段严格校验、需求条目和原文证据、不可变修订、用户编辑来源、单独确认；全部嵌入会话任务JSON，不新增数据库。
- DeepSeek采用真实Chat Completions JSON模式、关闭思考、禁重试与重定向。唯一引文由服务器定位；重复引文须明确位置，错误引文和越界来源拒绝。当前提示词 `requirements-v3`，模型记录为请求别名 `deepseek-flash`，不是不可变模型版本。
- 本地累计5次真实生成调用：早期4次输出未通过严格校验，最后v3在非隐私合成材料上成功，返回12字段、10条需求和10条证据；没有将修补后的诊断输出伪装成成功响应。仅说明该样例成功，不代表所有材料准确率已验收。
- 默认全局累计调用上限5、单任务3；已消耗5次，不自动清零、退款或提高额度。读取、编辑、确认和刷新不调用模型。付费入口仅开发环境启用，生产仍关闭；没有金额上限、正式匿名防滥用或生产可用性承诺。
- 浏览器实测通过：证据逐字定位；修改后先保存再确认；变更项新ID和user_revision来源、未变项ID保留；历史只读；旧版提交409、跨会话读取404、重复确认200；多必需语言未明确同组ALL/ANY时阻止保存确认，显式ANY可保存确认。
- 移除临时诊断启动方式，使用正常uvicorn重启并刷新后，从历史恢复4个完整版本及已确认状态，累计模型调用仍为5。刷新后的浏览器控制台无错误或警告。
- 前端TypeScript检查、Vite构建及13项测试通过。复验曾因整机提交内存不足中止；仅停止本次启动的开发服务，使用单进程顺序检查和较小Node堆后通过，未停止用户其他应用。构建产物密钥扫描未发现实际配置密钥。
- 后端最终完整202项通过（含13项新增存储回归）；原偶发失败场景另以100组独立夹具重复验证，100项通过。保留1条Starlette/httpx弃用警告，未隐藏。
- 偶发503已复现为Windows `os.replace` 返回WinError 5，未将具体占用来源臆测为杀毒软件。修复只在持锁期间，对WinError 5/32/33至多尝试4次原子替换；等待后核对临时内容和目标版本均未改变才继续。未知结果、已提交、源/目标变化及其他错误均停止，不重放业务回调或外部模型调用。确定性测试覆盖瞬时恢复、持久拒绝、旧数据保留、临时文件清理和未知结果不重试；这不等于承诺操作系统永久拒绝访问时仍能写入。

---

# 第一部分：产品理解与比赛范围

## 1.1 产品目标

ProjectPilot 面向学生的课程作业选题和参考项目发现：从 PDF、DOCX、纯文字或文件加补充文字提取需求，经用户确认后搜索真实 GitHub 仓库，通过 README 证据进行分析、确定性排序和带引用推荐。它不是普通搜索页面，也不是让 LLM 凭记忆生成项目名单。

本次交付是“公开可用的真实端到端产品 + 可追溯推荐 + 演示视频 + 参赛材料”。README 说明是文档证据，不代表实际运行、安装、测试或安全审计通过；所有推荐页面和材料应明确这一限制。

## 1.2 保留的五种输入

仅 PDF、仅 DOCX、仅纯文字、PDF+补充文字、DOCX+补充文字。最多一个文件，不同时上传两种格式；不支持 `.doc`，不做 OCR。

文件来源为 assignment_document；纯文字主要作业来源为 user_primary_text；文件附加文字为 user_supplement。补充的个人偏好不能伪装为教师硬性要求。修改后的用户表述标 user_revision，保留原始提取版本。

产品限制保持：单文件标称10 MB、PDF不超过30页、主要规范化正文不超过30,000字符、补充不超过2,000字符。建议明确10 MB=10,000,000字节，字符按Unicode码点计数，不含系统页码标签；Token预算另算。

## 1.3 完整业务链路和分工

| 环节 | 输入 → 输出 | 职责 |
|---|---|---|
| 文档处理 | 文件/文字 → NormalizedDocument | Python + PyMuPDF/python-docx解析、校验、保留来源，不调用LLM |
| Requirement Extraction | NormalizedDocument → 12字段需求及语义注释 | LLM理解；Pydantic和Python检查结构、原文及来源 |
| 核对与版本管理 | 初始需求/编辑 → confirmed RequirementRevision | 用户确认；Python保留不可变内容版本 |
| Query Planner | 确认版本 → A/B/C SearchPlan | LLM语义规划；Python检查依据、限定符及预算 |
| GitHub检索 | SearchPlan → SearchExecution/CandidatePool | REST API真实搜索、分页、去重、记录失败和截断 |
| 初筛 | 候选池与需求 → ScreeningDecision | Python规则，缺描述不等于功能不存在 |
| README采集 | 初筛候选 → RepositorySnapshot | 真实README、路径、Blob SHA、内容哈希、可获取的commit |
| Chunk/Embedding | README → 原文映射Chunk与显式向量 | Python分块；一种Embedding模型；ChromaDB存储 |
| Evidence Retrieval | 需求项+指定仓库快照 → EvidenceBundle | 关键词和向量检索，严格范围隔离 |
| 深度分析 | 需求、快照、证据 → RepositoryAnalysis | LLM逐项判断；Python检查引用与状态 |
| Matching & Ranking | 分析+批准配置 → RankingResults | Python硬约束判断、分项计算和稳定排序 |
| Draft/Review/Repair | 排名+证据 → RecommendationResults | Python有界编排；LLM生成与审查；无证据声明删除或降级 |
| 前端展示 | 结果 → 可核查推荐 | 展示匹配/待核查/排除原因、原文引用和文档证据限制 |

## 1.4 本次不做与未来方向

本次不开展 D10：固定语料库建设、金标准问题集、Baseline/V1/V2/V3/V4 消融和课程指标报告均不进入开发阶段、比赛验收或交付清单。未来重新启动课程工作时再制定独立实验计划，届时区分动态GitHub发现与固定语料RAG评价。

保留普通单元、契约、集成、安全、浏览器和端到端产品测试；保留实际运行快照和版本记录，因为它们是产品证据链，而不是课程实验任务。测试fixtures和真实演示记录不等于金标准实验语料。

OCR、源码下载执行、多Agent框架、多模型路由、复杂Hybrid优化、完整注册找回密码、多租户管理、分布式队列和大规模性能优化均后置。公开服务最小访问控制、任务隔离、限额及清理不是可后置高级功能。

---

# 第二部分：技术架构与前置部署验证

## 2.1 保持的架构

- 前端：React + TypeScript + Vite；不引入新的SSR层。
- 后端：Python + FastAPI + Pydantic v2，Python统一编排。
- 后续搜索关系存储：MySQL 8.4 LTS + SQLAlchemy 2.x + Alembic；驱动建议PyMySQL。仅用于GitHub搜索记录、仓库、快照等项目搜索相关数据，不作为输入启动依赖，不存通用会话/任务/文档/版本/运行/配额表。
- 文档解析：PyMuPDF、python-docx。
- 向量：一种显式Embedding模型 + ChromaDB，经VectorStoreService访问。
- 文件对象存储：会话、任务元数据、真实NormalizedDocument、需求版本、运行状态及限速/配额均保存为私有对象。开发LocalStorage指后端持久目录（不是浏览器localStorage），每对象跨进程文件锁+原子替换CAS；生产私有S3必须支持ETag条件写IfMatch/IfNoneMatch，缺失或失败不得静默回退。临时上传独立目录，详见§5.7。
- GitHub/LLM/Embedding分别封装Service。无Redis、Celery、微服务或多Agent框架。

## 2.2 Qoder Sites兼容性结论

依据历史修订读取的 Sites 插件文档：静态站点支持生产构建输出与SPA fallback；Functions入口为 `index.ts`、Edge运行时和HTTP handler，不是常驻Python/ASGI进程；Vite开发代理不会自动部署到生产。Sites内置数据库SDK不构成对MySQL 8.4、SQLAlchemy及Alembic的兼容性承诺。

| 组件 | 文档层结论 | 本项目部署建议 | 必须实际验证 |
|---|---|---|---|
| React/Vite | 可发布静态构建目录，支持SPA | `frontend/dist`发布到Sites；不发布项目根目录 | 首页/深链接刷新、资源MIME、打包无密钥 |
| FastAPI | 当前Sites Functions契约不是Python常驻进程 | 保留FastAPI，部署到独立Python主机/容器服务 | ASGI启动、TLS、后台任务不因请求结束被杀、持续运行 |
| MySQL 8.4（后续搜索） | 未核实Sites提供可直接使用的MySQL服务 | 搜索阶段接入私网MySQL；缺失不阻断输入 | 搜索阶段验证版本、网络、最小权限、TLS、真实迁移与重启持久化 |
| 文件对象存储 | 静态输出不是持久存储；Storage SDK不保证条件写 | 开发LocalStorage持久目录；生产私有S3；临时文件另放 | 本地跨进程CAS、冲突/未知结果及重启读取已测；S3仅Stub契约，真实权限/条件写及生产配额待验收 |
| ChromaDB | 无依据认为Edge可运行Python Chroma或保留本地索引 | 后端单实例本地持久Chroma，不开放向量端口 | 真实向量写入/过滤/重启读取，模型维度一致 |
| 环境变量 | Vite构建变量会暴露给浏览器；Sites Secrets仅供其Functions读取 | 模型/GitHub/MySQL密钥放实际后端Secret环境；前端仅公开API地址 | 构建包、日志无密钥；外部后端不能自动读取Sites Secrets |
| 外部API | 静态网页不能安全持有密钥 | FastAPI访问GitHub/DeepSeek/Embedding | 云端DNS、TLS、出口、区域和账户权限、限流 |
| `/api/v1`路由 | Sites的`/api`为平台保留路径，不是默认业务函数入口 | FastAPI原路由保留在独立后端HTTPS origin | 前端明确API base，不能对Sites origin盲发`/api/v1` |

推荐部署拓扑：Sites公开前端 → 独立HTTPS FastAPI → 私有文件对象存储；开发使用LocalStorage持久目录，生产使用满足条件写能力的私有S3。后续GitHub搜索阶段再接入私网MySQL及README/Chroma持久存储。输入不依赖MySQL或SQLite。这是存储职责与运行位置适配，不重写产品架构；资源、费用及赛事组合托管规则仍待确认，不宣称“全栈已部署到Sites”。

首选前端直连获批后端HTTPS地址，并用显式Bearer应用会话解决跨站Cookie阻断，详见§5.9。CORS精确放行真实Sites origin，不使用带凭据的通配符。Vite构建中API地址不是秘密，但绝不能放入服务API Key。

若比赛要求所有计算必须在Sites内完成，现有架构与平台运行时存在冲突，阶段0立即提出；不得擅改为Deno/Supabase来“适配成功”。可评估获批的固定目标轻量网关，但本版不默认增加Functions代理和身份信任链。

## 2.3 阶段0：最小部署验证闸门

在完整业务实现前执行，计划11月22日完成。必须先获得本计划、外部资源及发布范围批准；本轮不执行。

1. 核实比赛规则、Sites账号可用性、前端发布权限、独立后端及文件对象存储资源、域名/TLS和费用；MySQL资源另列后续搜索闸门。
2. 创建最小React页面和FastAPI健康/受控诊断路径，发布Sites预览或受限版本，连接独立后端；不把骨架包装为成品。
3. 从目标前端测试CORS预检、应用会话获取、带会话POST、202任务轮询和跨浏览器行为。
4. 输入先在无MySQL/SQLite环境验证会话、聚合task元数据及真实NormalizedDocument持久化、sessionStorage刷新恢复、状态查询和重启中断；开发验证跨进程锁+原子替换CAS，生产验证私有S3的ETag IfMatch/IfNoneMatch及失败不回退。不得用容器临时层过关。真实MySQL读写/Alembic迁移移至搜索阶段；显式Embedding/Chroma的检索及重启读取仍为后续完整链路闸门。
5. 后端最小真实GitHub请求、deepseek-flash结构化输出、选定Embedding请求；Token/费用真实计入预算。先完成正式密钥注入再测试，不在聊天收集Key。
6. 验证上传上限跨入口一致；网关可接收产品允许的文件；后台执行超过普通HTTP请求时长仍可通过轮询完成；后台重启会明确标中断。
7. 验证未授权请求拒绝、两会话隔离、无公开MySQL/Chroma端口、构建物无密钥。清理诊断数据，不留下公开任意写入接口。
8. 留下“验证项、时间、环境、观察结果、错误和修复”的记录。Sites发布状态成功、网页可达和业务API可用分别验收。

闸门按阶段生效：输入阶段缺少MySQL不能阻断启动、健康或五输入验收；文件对象存储不可用、CAS能力缺失/失败则阻断对应输入持久化，不能静默切换后端。搜索阶段才要求真实MySQL迁移和集成通过，模型/向量能力按所需阶段验收。11月23日仍无可用持久后端或安全入口时报告风险，由用户决定资源或排期，不等最后一天部署、不静默换架构。

## 2.4 目录和依赖关系

```text
nlp-project/
├── ProjectPilot_项目任务书.md
├── docs/
│   ├── ProjectPilot_Implementation_Plan.md
│   └── ProjectPilot_Competition_Checklist.md
├── .env.example
├── .gitignore
├── backend/
│   ├── pyproject.toml / requirements.lock
│   ├── alembic.ini / migrations/ # 后续GitHub搜索阶段，不是输入启动步骤
│   ├── app/
│   │   ├── main.py
│   │   ├── api/                 # 健康、会话、文档、需求、运行
│   │   ├── core/                # 配置、错误、日志、安全与限额
│   │   ├── schemas/             # 统一Pydantic契约
│   │   ├── services/            # 解析、外部API、检索、评分与引用
│   │   ├── workflows/           # 单一Python流程编排
│   │   ├── persistence/         # 文件对象存储/CAS；后续搜索专用MySQL存取
│   │   └── prompts/
│   ├── contracts/
│   └── tests/                  # 单元、契约、集成、端到端fixtures
├── frontend/                   # Sites发布仅取dist
└── data/                       # 开发LocalStorage持久目录；生产对象在私有S3，不打包到Sites
    ├── tmp/
    ├── sessions/               # 每会话一个聚合JSON，包含所有task元数据
    ├── quotas/                 # 限速/配额持久对象
    ├── documents/              # 完整真实NormalizedDocument及来源
    ├── repository_snapshots/
    └── chroma/
```

不为D10创建experiments目录。路由依赖应用服务，工作流依赖Service，Service通过统一客户端与持久化模块操作；Schema不导入路由，RankingService不联网、不让LLM直接输出总分。

## 2.5 执行和可靠性边界

单个Uvicorn worker和有界进程内后台执行器；异步请求先持久化任务/Run状态再按现有API返回202，前端轮询。对象更新按§5.7执行CAS，即使单worker也不能只用进程内锁。仅后续搜索SQLAlchemy工作单元放线程池，不共享Session，不把网络等待包进事务。解析需要硬时限时采用可终止子进程，不把线程超时当作停止解析。

服务重启不自动续跑；失去执行者的任务或超过状态期限的任务必须能经现有状态接口查到明确失败/超时，不永久processing。后续Run中断沿用failed+RUN_INTERRUPTED；当前输入沿用既有状态及错误契约，不新设计错误枚举。sessionStorage保留会话token及恢复所需标识，刷新后授权查询已存任务和真实文档，不重复上传或重启执行。已存在快照可幂等复用，新运行仍独立绑定版本。MySQL迁移仅为搜索阶段获授权的发布前步骤，不在请求中自动改表；输入启动不连接或迁移数据库。首次部署仍单实例，避免争用本地Chroma。

---

# 第三部分：统一数据结构和证据契约

## 3.1 共通元数据

Pydantic v2拒绝额外字段，显式校验可空/必填/枚举。ArtifactMeta包含schema_version、artifact_id、task_id、run_id（运行前可null）、created_at、input_artifact_ids、config_version。任务归属由后端会话决定，不相信客户端owner_id。

所有引用检查存在性、任务归属、revision、repository和snapshot。保存代码/Prompt/模型/配置版本和非敏感配置快照；时间UTC。相同保存的分析、配置和评价时间必须得到相同Python排序，不承诺重新调用LLM必然相同。

## 3.2 NormalizedDocument

Phase1已确认契约以 `backend/app/schemas/documents.py` 的NormalizedDocument、SourceSegment及相关响应模型为准；本轮只核对原文，不改Schema。历史计划中的source_type/filename/segments/document_text/supplementary_text、ExtractedSegment命名不是当前字段定义，不能据此重塑实现。

当前NormalizedDocument保留document_id、task_id、input_mode、original_filename、mime_type、file_size、total_pages、extracted_text、supplemental_text、normalized_text、source_segments、content_hash、extraction_status、warnings、created_at、character_count。SourceSegment保留segment_id、source_type、source_kind、content、original_content、source_location、start_offset、end_offset、location_detail；跨度必须对应normalized_text中的content。具体类型、可空性和校验以该文件为准，不在本任务新定义字符计数或响应语义。必须保存并恢复完整真实对象，不能用摘要/空片段重建替代；PDF页码、DOCX段落/表格位置及原始/规范化来源保持，不编造Word页码。

## 3.3 RequirementJSON：12字段不变

| 字段 | 类型 | 语义 |
|---|---|---|
| project_topic | str或null | 主题 |
| project_description | str或null | 整体描述 |
| project_type | str或null | 项目类型 |
| domain | str或null | 领域 |
| required_features | list[str]或null | 必需功能表达 |
| preferred_features | list[str]或null | 偏好功能表达 |
| programming_languages | list[str]或null | 语言，逻辑由伴随元数据表达 |
| required_technologies | list[str]或null | 必需技术表达 |
| preferred_technologies | list[str]或null | 技术偏好表达 |
| missing_information | list[str] | 无缺失时[] |
| uncertain_information | list[UncertainItem]或null | 无歧义时null |
| source_evidence | list[RequirementEvidence] | 动态数量，真实来源 |

所有12字段显式出现；null=来源未知/无法确认，[]=明确为空。不能通过默认空列表掩盖缺失字段。RequirementEvidence保留evidence_id、field、original_text、source_kind、source_location、可选requirement_item_id，并建议segment_id/source span精确定位。UncertainItem建议field、requirement_item_ids、reason、question、evidence_ids。

## 3.4 RequirementItem和Revision（本版冻结提案）

RequirementItem放在Revision伴随元数据中，不把12字段列表改成对象，不增加业务一级字段。

| 字段 | 定义 |
|---|---|
| requirement_item_id | 后端生成的稳定UUID；显示编号如REQ-001仅辅助阅读 |
| field/value_path/text | 对应12字段的值及具体条目；位置不是身份 |
| modality | required / preferred / prohibited / informational |
| source_kind | assignment_document / user_primary_text / user_supplement / user_revision |
| evidence_ids | 指向实际输入片段或真实修改记录 |
| logic_group_id/operator | 显式ALL、ANY；单独项not_applicable |
| supersedes_item_id | 实质语义替换时指向被替换项，允许null |

- required为必须满足；preferred仅加分；prohibited为必须遵守的禁止条件；informational只做上下文，不参与硬过滤或加分。
- 不变项和列表重排沿用ID；新增项新ID；措辞修改但语义不变经用户确认可沿用；极性、逻辑或实质含义改变新ID。始终以revision_id+item_id解释具体含义。
- 多语言不是自动ALL或ANY；歧义需用户核对。比赛只支持一层显式分组，混合嵌套复杂逻辑要求澄清，不自动展开成错误条件。
- 建议禁止技术/功能仍放相应required字段字符串中，保留“不使用X”等否定文本，伴随modality=prohibited；下游仅使用语义对象，绝不把它当正向搜索条件。用户批准后冻结此映射。
- user_supplement不等于必须为preferred：用户明确增加硬要求时可以标required，但来源仍是用户而非教师；冲突提示核对，不能悄悄覆盖作业来源。
- 用户改写保存修改原文、时间、字段和旧/新值；新证据标user_revision，不能保留旧教师引文为新结论背书。

RequirementRevision保留revision_id、task_id、revision_number、parent_revision_id、status extracted/draft/confirmed、requirements、requirement_items、change_log、created_at、confirmed_at。内容不原地覆盖；确认指定版本；编辑创建新draft，原始和旧confirmed均保留。乐观并发检查防止旧页面覆盖新版本；未确认不可搜索。

## 3.5 搜索、仓库、快照和Chunk

| 模型 | 核心字段 |
|---|---|
| SearchPlan | search_plan_id、confirmed_revision_id、planner_version、groups、created_at |
| SearchGroup/Query | A/B/C、ready/skipped、purpose、source_requirement_ids、skip_reason；query_id/text/type、language_filter、created_by |
| SearchExecution | query_id、实际参数、页码、时间、耗时、HTTP状态、返回数、incomplete_results、缓存命中、截断和失败 |
| CandidatePool | revision/plan引用、repository_id去重候选、各组/查询/页/排名命中记录 |
| ScreeningDecision | repository_id、keep/exclude/needs_review、原因、rule_version |
| RepositoryMetadata | GitHub ID、full_name、URL、description、topics、language、stars/forks、更新时间、归档、license、default_branch；字段允许缺失 |
| RepositorySnapshot | snapshot_id、repository_id、commit_sha可空、readme_path、readme_blob_sha、content_hash、collected_at、collection_status、原文文件引用 |
| ReadmeChunk | chunk_id、repository_id、snapshot_id、source_file、heading_path、chunk_text、start/end_offset、source_spans、chunk_index |
| IndexManifest | snapshot、chunker版本、模型/维度、collection、预期/实际Chunk数、pending/ready/failed状态 |

Blob SHA不等于commit SHA。建议哈希对原始文件字节计算SHA-256，解码规则独立保存。原文不可被清洗或摘要覆盖；Chunk位置对应原始解码文本，多段删除用source_spans而非伪造单一连续跨度。

## 3.6 EvidenceBundle、Analysis和推荐声明

EvidenceBundle绑定confirmed_revision_id、repository_id、snapshot_id及检索配置；每条证据包含evidence_id、chunk_id、source_file、source_location、quote、retrieval_method、retrieval_score（注明度量和方向）、requirement_item_ids。没有召回也记录范围和原因。

RepositoryAnalysis保留analysis_status success/partial/failed、requirement_assessments、unprocessed_requirement_ids、failure_reason、model_version、prompt_version及revision/repository/snapshot关联。

为避免三态无法表达反证的歧义，本版提案：
- `status`继续使用supported/not_found/uncertain，表示支持证据是否充分。
- 增加伴随`evidence_polarity=supports/contradicts/mixed/none`和明确反证引用。
- 明确反证用`uncertain + contradicts + hard_constraint_check=fail`的组合，UI显示“明确违反”，不得只显示uncertain。混合冲突证据用uncertain+mixed+unknown；无证据用not_found+none+unknown。
- 对prohibited，支持证据是“明确满足禁止要求”，不是出现禁用项；明确违反禁令为fail。不提及不算pass。
- 非硬项hard_constraint_check=null（不适用）。最终映射和契约测试需用户批准后冻结；不私自新增status枚举。

每条推荐理由必须绑定requirement_item_ids、evidence_ids，并能沿“Run → 确认Revision → 仓库ID → Snapshot → 原始README → 原文跨度”追溯。metadata_refs只支持Stars、更新时间等元数据，不可替代README为功能匹配背书。证据面板展示快照日期、quote和原文上下文；原仓库最新README可能已改变，优先展示保存快照。引用不能跨仓库、快照、用户任务。

RankingResults记录分项适用性、分值/理由、hard状态、unknown项、证据覆盖、排序配置/哈希、evaluation_time、rank及排除原因。RecommendationResults内含结构化claims、审核状态、warnings和空结果原因，不能只返回一整段无法逐条核查的文字。

---

# 第四部分：API与运行状态

## 4.1 核心业务接口保留

下面保留历史后续业务API规划，不能当作替换当前Phase1输入API的指令。当前已有会话、任务列表/创建、输入提交/状态、文档读取和配置接口及全部请求/响应Schema完整保留，以现有实现及 `backend/app/schemas/documents.py` 为准；仅改变持久化职责，不新设计API。路径均属于独立FastAPI；Sites前端通过获批API base调用。会话创建及既有公开health/config按现有契约访问，任务/文档等资源须有效会话及归属检查。

| 方法/路径 | 请求 | 响应与错误 |
|---|---|---|
| POST `/api/v1/documents` | multipart file?、primary_text?、supplementary_text? | 201 document/task ID及NormalizedDocument；413/415/422 |
| POST `/api/v1/documents/{id}/requirement-extractions` | 空对象；Idempotency-Key | 202 extraction_id、状态；404/409/503 |
| GET `/api/v1/documents/{id}/requirements` | 可选extraction_id | 200 提取状态/错误、revisions、latest_revision_id |
| POST `/api/v1/requirement-revisions/{id}/revisions` | 完整requirements、语义编辑、change_reason | 201新draft；过期409、非法422 |
| POST `/api/v1/requirement-revisions/{id}/confirm` | 确认指定版本及歧义决策 | 200 confirmed；未澄清硬逻辑422 |
| POST `/api/v1/runs` | confirmed_revision_id；Idempotency-Key | 202 run_id/queued；未确认409、限额429 |
| GET `/api/v1/runs/{id}` | 无 | 阶段、已处理数量、耗时、warnings/error、results_available |
| GET `/api/v1/runs/{id}/results` | 无 | 已准备好200；未完成409；允许真实空结果 |
| GET `/api/v1/health` | 无 | 保持既有响应契约；公开不暴露内部配置；输入关键对象存储不可用503，未接入MySQL不影响输入健康；搜索依赖按阶段检查 |

为公开访问补充最小安全与证据接口，不改变核心业务流程：
- POST `/api/v1/sessions`：当前Phase1优先沿用最新输入方案的匿名独立会话，不强加旧计划邀请码。匿名不等于共享会话或免除限速/隔离；后续付费调用的访问策略另设闸门。
- DELETE `/api/v1/sessions/current`：撤销当前会话，不以此自动删除仍在运行的数据。
- GET `/api/v1/runs/{id}/evidence/{evidence_id}`：只返回属于该授权运行的证据及安全原文上下文，不接受客户端文件路径。

尚未实现的撤销/证据等后续接口仍为历史提案，需连同OpenAPI批准；不借此次存储修订改变当前Phase1契约。后续付费能力的评委访问方式、邀请码或等效匿名防滥用方案须另行确认，不作为当前匿名输入前置条件。

历史业务接口规则保留：有file时禁止primary_text；无file必须primary_text且无supplementary_text。当前Phase1的字段名称及校验以其既有API契约为准，不因存储变更改名。编辑不能由客户端指定教师来源。后续幂等键相同payload返回已有ID，不同payload返回409，范围包括owner/操作/资源；映射与状态保存在文件对象中，通过CAS控制并发，而非SQL唯一约束。CAS冲突只可重试无外部副作用的纯状态变更，不重放解析、模型或GitHub调用；未知写结果须读回核实归属、版本及操作结果，不能盲重放或盲删。刷新用sessionStorage恢复会话及标识，再查询任务/状态/完整文档，不触发重复执行。

## 4.2 状态与异常语义

ExtractionJob：queued/extracting/validating/completed/failed，与Revision状态分开。

Run：queued → planning → retrieving → screening → collecting → indexing → analyzing → ranking → reviewing → completed；终态另有partial_failed、failed。比赛不承诺取消接口和重启自动恢复。

- 正常无候选/无可推荐项：completed + empty_reason，不伪造仓库。
- 部分请求/仓库失败且有审核通过结果：partial_failed + 明确范围；预算截断单独记录warning，不伪装穷尽。
- 模型、文件对象存储、搜索阶段数据库或向量关键步骤失败，无可用结果：failed，不伪装空匹配；输入不因未配置MySQL失败。
- 重启/中断：failed + RUN_INTERRUPTED；旧产物保留，可由用户新建运行。
- 进度显示真实阶段和计数，轮询建议3秒，页面隐藏降频，不编造百分比/剩余时间。

统一错误：error_code、message、retryable、details（脱敏）、request_id。同步错误用适当HTTP码；查询失败运行仍是成功获取资源的200，其内部携带error。权限失败返回401，跨归属资源统一404，额度429可含Retry-After。模型超时、对象存储不可用、搜索阶段数据库不可用和认证失败按对应既有契约区分；当前输入状态超时可查，不新设计API或错误Schema。日志不带敏感输入和上游完整响应。

---

# 第五部分：比赛技术方案与明确规则

## 5.1 文档解析

流式验证实际字节数，不能只信Content-Length/扩展名。PDF检查格式、加密、损坏、页数，按页抽取；全扫描件提示暂不支持OCR；部分空页警告。DOCX检查ZIP总解压量、条目数、压缩比和时限，再按正文顺序读取段落/表格，避免合并单元格重复，不下载外部引用。

清洗仅规范空白/换行，不删除must/must not/only等语义。页眉、文本框等未覆盖内容检测后提示人工核对。原始提取片段及映射保留，超长不静默截断。临时文件用服务器随机名，解析结束或失败即清理。

## 5.2 一种LLM、一种Embedding

### LLM首选

用户确认本阶段使用`deepseek-flash`，仅接入官方DeepSeek服务，API base `https://api.deepseek.com`。2026-11-21官方模型列表请求返回200且包含该模型；该检查不代表生成调用、余额或生产部署验收。需求阶段选用官方Chat Completions JSON模式，不同时维护Responses或其他供应商协议，也不自动切换模型。

选择门槛：中英文需求提取正确、否定与ALL/ANY可保留、支持结构化JSON输出、上下文能容纳受限输入、延迟/成本可控、部署地区能调用、数据使用条款可接受。测试使用普通产品样例，不建立课程金标准或消融。

本阶段采用`response_format={"type":"json_object"}`并关闭thinking，不宣称服务端严格Schema约束；返回值仍须通过Pydantic、来源定位和业务校验。模型输出空白、截断、结构或证据错误均明确失败，不自动调用模型修复或重试。未来若启用结构修复或其他协议须单独验收，不静默换模型或把普通文本当合格JSON。

配置：LLM_PROVIDER、LLM_BASE_URL、LLM_MODEL、LLM_API_KEY、协议/结构化模式、各阶段输出上限、超时、最大调用数、价格版本。保存请求模型ID和响应模型标识，不把可能更新的别名宣称为不可变版本。比赛建议关闭不必要的推理/工具调用，先验证可用参数，不向模型开放浏览、Shell或外部工具；GitHub发现仍由Python负责。

### Embedding首选方案

建议选一个托管的多语言BGE-M3 Embedding API部署作为首选，语义模型`BAAI/bge-m3`；具体供应商/部署ID、价格和可达性须11月22日前确认。文档写出模型候选不等于某供应商已提供或账号已开通。使用dense向量，预期1024维须以实际服务返回和模型配置核验；不悄悄启用另一模型或Chroma默认Embedding。

优先托管API是为了不在比赛期增加GPU/模型下载运维。选择门槛：中英文语义效果、固定模型版本/维度、批量输入、Token限制明确、超时/价格透明、云端可达。若无可用服务，立即让用户选择资源或调整部署，不开发多模型回退框架。LLM服务可用不代表提供Embedding。

Embedding配置：PROVIDER、BASE_URL、MODEL/DEPLOYMENT_ID、API_KEY、DIMENSION、模型Token上限、预处理版本、batch_size（建议16）。保留独立Service，但运行期只配置一种模型。

### 调用与成本初始预算（待批准，不是价格承诺）

| 项目 | 比赛初始建议 | 处理 |
|---|---|---|
| 网络连接超时 | 10秒 | 超时明确标记，计入尝试次数 |
| LLM单次总时限 | 60秒 | 应用层总deadline，不仅socket idle timeout |
| Embedding单批时限 | 30秒 | 有限重试，不写入不完整索引为ready |
| 网络重试 | 每请求最多1次额外尝试 | 仅暂时网络/429/部分5xx，遵守Retry-After及剩余预算；401/非法参数不重试 |
| JSON结构修复 | 每步骤最多1次 | 引用/语义错误仍需校验，共享全局调用预算 |
| 文档提取 | 最多2次调用含修复，输出上限建议8,192 Token | 上下文/截断检查；失败停在需求阶段 |
| 推荐Run | 最多20次LLM尝试含重试/修复，单次输出建议4,096 Token | Planner1 + 深析最多10 + Draft1 + Review1，余量供必要修复；超额停止 |
| Embedding | 每Run最多40批请求尝试、200,000输入Token | 同时限制总Chunk数；先估算，超预算显式缩减候选或标partial |
| Run总时限 | 300秒，提取单独最多120秒 | 阶段开始前检查剩余预算；不是对用户保证300秒内一定成功 |
| 金额上限 | 建议提取¥0.20、每Run¥1、全站每日¥30 | 须确认人民币预算及价格表；未配置预算禁止公开付费入口 |

上述次数/Token/金额/时间是同时生效的上限，最先达到者停止；并非要用完。每次调用前预留最坏可计费用量，结束按usage结算；超时无usage按预留量记账而非0元。重试可能重复计费，必须纳入预算。价格按实际供应商/币种配置并注明日期，不写死猜测单价。全站预算、会话配额、限速和运行并发通过持久文件对象及CAS控制，重启不能清零绕过，不使用SQL配额表。每个限额对象更新必须原子；跨对象不宣称共同事务，后续付费闸门须验证保守预留、结算和失败核对，未知结果保留预留、不盲目退款或重放付费调用。

## 5.3 Query Planner与GitHub检索

A=主题/领域/描述；B=必需/偏好功能；C=语言/必需/偏好技术。每条查询带确认版本的source_requirement_ids；无可用字段该组skipped并记录原因。LLM输出词语及受控限定符，Python校验语法、依据、数量、去重，再拼GitHub查询；禁止条件不当正向词；偏好不能变全局硬过滤。禁止未经批准的Stars阈值、license等筛选。

比赛初始预算建议：每组1条有效查询、每页30条、每条最多2页，候选最多100，深度README最多10，最终推荐最多5。每Run搜索请求尝试最多8（最多6页含重试余量），GitHub全部请求尝试最多60，含ref/README/metadata/languages/重试；GitHub阶段累计最多90秒，单请求15秒。它们是应用预算，不是GitHub官方限流额度。

分页按Link/返回结果与预算停止；达到候选/页数预算记录truncated，记录incomplete_results，不承诺发现全部GitHub项目。API限流以实际响应头为准，搜索与普通请求分别处理；耗尽等待会超deadline时结束为部分失败而非无限sleep。

按repository_id去重并保存每个命中的组、query、页和位置。不同组保留候选覆盖机会，确定性裁剪；原始CandidatePool和筛选结果分开。Python初筛不调用逐仓库LLM，不默认排除归档仓库，不因description缺某词判硬fail。主语言不等于全部语言。

401报告Token配置问题；403区分权限/主限流/二级限流；429或5xx有界退避；部分查询成功保留结果。所有请求保存SearchExecution与缓存/失败/预算记录，不记录Token。

README建议先确定commit，再按ref获取，失败允许commit=null并注明；缺README、404、超时、解码失败、过大分别标记，不伪造。建议上限1MiB/README，超限不默默截断。原文和清洗版本独立，固定API目的地，不抓README任意外链。搜索缓存建议5分钟、可变元数据15分钟，不可变SHA内容可复用；TTL可配置、保存原时间，不把旧缓存说成最新请求。

## 5.4 Chunk、向量与检索

按Markdown标题/段落优先切分，保留表格、代码、否定和原文映射。初始建议512 Token/Chunk、overlap64，超过模型上限再细分，不随意截断原文证据。每仓库最多100 Chunk、每Run最多1,000 Chunk；超额标覆盖范围不足并停止该仓库完整分析，不能宣称阅读全文。具体参数以少量产品样例和模型Token限制验证，不做课程参数消融。

显式Embedding向量，检查数目、维度、有限数值；缓存键包含模型/版本/预处理/文本hash。Collection按索引配置和模型版本隔离；query同时过滤repository_id与snapshot_id，并验证所属授权Run。共享公开README缓存不暴露用户需求/分析。

后续搜索快照索引流程：搜索专用MySQL IndexManifest为pending → 写README文件/Chroma → 校验Chunk数及hash → ready；失败保存错误，可幂等补齐，不宣称跨存储事务。使用稳定Chunk ID和upsert，旧快照索引不会混入新快照。

检索每需求Top-K初始5，关键词与向量结果统一EvidenceBundle，采用简单排名合并，不相加不同量纲分数。相似度只找相关片段，不证明支持，不直接成为总分。复杂融合和阈值调优后置。

## 5.5 硬约束与软评分

### 硬约束优先

- required：有明确支持才Pass，明确反证才Fail，其余Unknown。
- prohibited：有明确遵守禁止条件的证据才Pass，明确违反才Fail，未提及或冲突为Unknown。
- ALL：任一Fail则Fail，全Pass才Pass，其余Unknown。
- ANY：任一Pass则Pass，全Fail才Fail，其余Unknown。
- 不同硬约束组之间默认ALL；分组与modality须用户确认。没有硬项时N/A，不冒称“全部硬要求已满足”。

推荐分区：全硬项Pass进入“文档证据匹配”；无Fail但存在Unknown进入“待核查候选”；任一Fail不进入满足要求推荐，保留排除原因。没有硬项单独注明“未设置硬性条件”。README未提及只能Unknown/not_found，不能Fail；README缺失和分析失败单独标处理状态，不能假装真实不匹配。

### 确定性排序提案

先分区，再对Unknown区按硬项明确支持覆盖率排序；同覆盖率再比较适用软维度。七个维度仍保留：主题、必需功能、语言、技术、偏好功能、项目类型、活跃度/热度。

比赛建议语义匹配使用可枚举supported/uncertain/not_found映射，由Python计算；provisional映射1/0.5/0仅表示“文档支持程度”，不是实现概率或满足率。无需求的null维度N/A，不进分母；无证据的0必须同时显示not_found，不宣称“不实现”。未知项数量及分母公开。

正式维度权重仍须用户批准；可以批准等权作为明确命名的比赛初始配置，不得把未批准数字硬编码为标准。适用软维度按 `sum(w*s)/sum(w)` 归一，全部N/A时total_score=null。硬约束分区和覆盖率在加权分之前，Stars/活跃度仅语义分相同后的辅助排序，不能进入加权分掩盖必需功能。最后repository_id稳定裁决。保存评价时间、配置版本/哈希和各分项理由。

若评分政策未获批，该阶段是明确阻塞，不能用LLM生成总分绕过。面板呈现“文档匹配分”，不称实际项目质量或运行成功率。

## 5.6 简化Agentic工作流

全部使用Python服务函数和显式状态机：Planner → Retrieval → Analysis → Ranking → Draft → Review → 必要Repair。不同Prompt是同一模型的不同职责，不引入LangGraph/CrewAI/AutoGen、多Agent对话或自主工具循环。

Draft生成结构化claims，不改变仓库集合和Python排名。Review由确定性引用验证+同一LLM语义审查构成；审查模型仍可能出错，不承诺客观真值。

比赛限定最多一轮Citation Repair，且只在同仓库同快照重新检索。优先替换正确引用、删除无依据声明或改为明确未知。若新证据改变需求判断，最多一次受影响项重分析→Python重排→重生成→最终Review；此后不再进入Repair，仍有问题则删除/不推荐。全部调用共享20次/300秒/金额预算，不形成复杂闭环；不静默更新README或重新全网搜索。

每条最终推荐理由至少有一条真正支持其含义的原文证据；多个仓库比较声明必须分别有证据，不能借A证据描述B。局限与“尚未运行验证”的说明始终保留。

## 5.7 文件对象持久化与后续搜索MySQL边界

### 当前Phase1及通用用户状态：不使用数据库

会话、任务、NormalizedDocument、幂等映射及输入限速使用私有文件对象存储，不使用MySQL或SQLite；后续RequirementRevision、Run及费用配额也沿用非数据库职责，尚未实现。当前每会话的task元数据及完整NormalizedDocument保存在同一个JSON对象中，原始上传文件使用独立随机对象键。MAX_SESSION_TASKS默认100且可配置，超限明确拒绝，不静默裁剪历史；独立的聚合JSON字节数和存储总量上限仍是生产前待补验收项。文档所有字段、来源片段及原文映射按§3.2契约读取恢复，不以摘要或占位文档替代。

- 开发LocalStorage为后端持久目录，每对象使用跨进程文件锁，在同一锁内读当前版本、比较期望版本并原子替换；创建也要防覆盖。不用仅线程/进程内锁冒充CAS，临时写入文件与目标处于支持原子替换的同一文件系统。
- 生产使用私有S3，更新必须以ETag作IfMatch条件，创建必须IfNoneMatch防覆盖；目标服务、SDK及实际权限须验证条件写能力。缺失、不支持或失败均显式报错，不回退无条件覆盖、LocalStorage、内存或数据库。
- CAS冲突仅有界重试纯状态变更，重读后重新判断业务前提；不得在重试闭包中重放解析、模型调用、GitHub请求或其他外部副作用。
- 写入超时等结果未知时先读回核对版本、操作标识、内容及归属；不能盲重放、盲删文档或释放预留。无法确认时保留可核对状态并报告失败，不伪报成功。跨对象写入没有共同事务：先保存原件，再条件提交任务元数据；解析完成后把完整NormalizedDocument与完成状态一并写入会话JSON。仅可清理本请求生成且明确未被提交的原件，不能把未知提交当孤儿删除。
- sessionStorage保留当前标签页会话及恢复标识；刷新后通过既有API恢复任务列表、输入状态及完整文档。后端重启不自动续跑，中断/状态超时须可查；不把浏览器缓存当服务端持久化。

本地测试已验证跨进程锁/CAS、并发提交、未知写结果保留、会话限速和重建应用恢复；S3仅验证Stub请求契约，私有桶策略及真实条件写未验证。正常LocalStorage后端的浏览器五输入、来源预览、后端重启与页面刷新恢复已通过，原件哈希已核对；生产总存储配额和自动孤儿清理尚未实现。

### 后续GitHub项目搜索：真实MySQL

保留MySQL 8.4 LTS、InnoDB、utf8mb4、SQLAlchemy 2.x和Alembic，仅用于搜索执行记录、仓库元数据、候选命中关系、RepositorySnapshot及其索引清单等项目搜索相关数据。搜索关联仅保存必要标识，不复制用户原文或通用状态；用户侧SearchPlan、EvidenceBundle、Analysis、Ranking、Draft/Review/Repair等版本化产物继续为受归属约束的文件对象。

不创建sessions、tasks、documents、requirement_revisions、runs、quota_counters等通用表，也不以通用artifacts JSON表变相存入全部用户数据。具体搜索表在该阶段按查询需求确定，不在输入阶段建库或迁移。搜索索引的pending/ready状态只属搜索快照索引，不替代用户Run状态。

搜索表使用必要外键/唯一约束；GitHub ID用BIGINT，时间UTC，ID/hash区分大小写。连接池及超时有界，独立Session，死锁只重试安全事务，不重放外部调用。DDL可能隐式提交，不承诺全回滚；应用/迁移账号分离，Secret注入连接参数，远程验证TLS，数据库不向浏览器公开。

仅搜索阶段执行独立真实MySQL测试库的迁移、连接及集成测试，不以SQLite替代。输入环境无MySQL仍须可启动并完成五输入及恢复验收。备份覆盖会话聚合对象、文档、版本/运行/限额对象；接入搜索后再关联MySQL、README快照和Chroma。单独SQL dump或仅备份正文都不等于完整恢复。

## 5.8 普通产品测试与异常矩阵

| 场景 | 预期行为 | 测试证据 |
|---|---|---|
| 空输入/双文件/超限/坏PDF/DOCX/扫描件 | 明确4xx，无模型调用，临时文件清理 | 单元+API边界；5种正常输入 |
| JSON无效/缺12字段/证据不存在 | 一次有限修复，仍失败停在提取；不生成confirmed | 契约+服务Mock+至少一次真实LLM |
| GitHub限流/权限/超时/部分查询失败 | 区分错误，遵守预算；成功候选保留 | Mock响应头测试+真实搜索 |
| 无候选/无符合项 | 正常空结果及原因，不填假项目 | API+浏览器 |
| README缺失/超限/解码失败 | 独立采集状态，无功能Fail误判 | 单元+结果页面 |
| Embedding失败/维度错误/Chroma异常 | 不标索引ready；不冒充检索成功 | 单元+真实索引/重启测试 |
| 引用错误/跨仓库/跨快照/无支持 | 拒绝或一次修复；仍无支持删声明 | 引用定位与结果浏览器测试 |
| 对象存储失败/CAS冲突/未知写结果 | 明确失败不回退；仅纯状态可有界重试；未知结果先核对，不盲重放/盲删 | 跨进程锁、原子替换、S3 ETag条件写及故障测试，待验证 |
| 搜索数据库连接失败/池耗尽 | 搜索稳定503，无假成功；不影响无DB输入启动与健康 | 搜索阶段隔离真实MySQL集成 |
| 刷新/任务超时/后端重启/重复点击 | sessionStorage恢复现有任务及完整文档；中断不自动续跑，状态超时可查；后续Run为failed+RUN_INTERRUPTED | 无DB五输入、刷新和受控重启与幂等测试，待验证 |
| 两访客越权/伪造owner/过期token | 拒绝访问，不能读文档/结果/证据 | 两个独立浏览器会话正反例 |
| 额度耗尽/匿名会话重建/后续付费访问滥用 | 文件对象CAS限额、429；换会话不能绕过全站限制；付费入口另设闸门 | 安全边界功能测试，不做压垮服务的负载测试 |
| Markdown脚本/危险链接/提示注入 | 转义/清理，模型不执行资料指令 | 单元和浏览器 |

每阶段报告实际测试命令、环境、结果和未测项；Mock仅隔离测试，真实产品不能返回硬编码推荐。最终必须通过公开URL至少一次真实GitHub+README+deepseek-flash+Embedding的完整流程；前端变更启动开发服务器并浏览器验证，发布后再测真实域名和生产配置。D10课程指标不作为验收。

## 5.9 公开部署最小安全基线

### 访问与隔离

当前Phase1按最新输入方案优先使用匿名独立会话，不强加历史邀请码要求，也不由本轮改动站点Public权限配置。匿名输入仍须会话归属、限速及存储限额，不同访客不能共享任务空间；不实现注册/密码找回。服务端会话对象仅保存随机token的哈希及有效期等元数据，保持既有契约。

后续付费模型/搜索能力另设访问和费用闸门：一次性邀请码或等效匿名防滥用策略须结合评审规则另行批准。若选邀请码，需高熵、服务端哈希校验及原子单次兑换；历史24小时有效期等参数仍为待确认建议，不能当作当前Phase1已实现事实。不直接裸开付费入口。

跨源方案使用Authorization Bearer：token及恢复标识保存在当前标签页sessionStorage，不改为仅内存而丢失刷新恢复，不放URL/日志/浏览器localStorage；刷新后授权读取任务和完整文档，关闭标签页可能失去访问需提前说明。sessionStorage仍可被XSS读取，因此必须有严格CSP、依赖和Markdown安全；生产安全不靠隐藏API地址。服务器验证token后查询资源owner，客户端owner字段忽略。所有document/revision/run/evidence操作和幂等查询必须带owner约束，UUID本身不是授权。

外部FastAPI不信任浏览器提供的Qoder user_id或x-qoder-*；Sites登录状态不是可转发的已验证身份。本方案独立管理应用会话，避免伪造平台身份。管理员与用户入口分离，管理端不得暴露全量任务给匿名访客。

### Key、限额与外部调用

S3/GitHub/DeepSeek/Embedding/MySQL密钥只在实际后端Secret环境；不进入VITE_*、构建包、Git、前端请求或日志。Sites Secrets不能自动注入外部主机。固定上游域名，禁止用户输入任意base URL形成SSRF；不跟随README外链。严格CORS允许列表不是鉴权替代；仅信任已配置反向代理的转发IP头。

当前Phase1匿名会话、写操作速率及每会话task数量可配置，限速状态用持久对象CAS更新；独立读轮询限速、聚合JSON字节数及总存储配额尚未实现，列为生产前补充项。以下仅为后续付费阶段初始建议：每会话每分钟写操作5次、每小时最多3次Run、同会话最多1个活跃Run；全站最多2个运行中、队列最多10。会话兑换每IP每10分钟5次，付费运行每IP每小时10次作为辅助限制；读轮询每会话每分钟30次。结合全站金额上限及会话额度，不能靠换token无限调用。IP计数可短期加盐hash，不当作用户身份。达到额度返回429和重试提示。

### 上传、存储与清理

应用与入口统一10,000,000字节文件上限，multipart请求上限建议11MiB容纳字段开销；PDF30页、正文/补充字符限制仍生效。DOCX建议解压总量50MiB、条目1,000、最大压缩比100:1、解析时限10秒，需正常DOCX样例校验；这些是可调整安全配置。限制账户总存储和磁盘水位，文件格式实际内容校验，路径由服务器生成。

上传临时文件解析后finally清理，进程崩溃残留建议1小时过期；启动及每15分钟清扫过期临时文件，不删除仍有活动租约的任务文件。持久对象与临时上传须区分：写结果未知时不得在finally盲删已可能提交的文档，先核对会话聚合元数据、对象版本和引用，无法确认则保留待核对。用户正文和任务结果建议保留7天并在上传前告知；过期清理先通过CAS撤销访问/标记，再核对活动状态、关联/索引及独占对象，不能覆盖并发更新或误删仍引用的文档。共享公开README只有无其他Run引用才回收。保留脱敏运行统计；备份保留时长也需说明，删除主存储不等于瞬间清除备份。

LocalStorage持久目录、私有S3对象和Chroma目录均不公开，不映射成静态资源；证据读取经授权API，不能提供任意路径下载。只保存任务所需数据；提交前告知作业文本会发送到模型服务，禁止上传含不必要个人隐私/密钥的文件。

### 公开验收闸门

当前匿名输入须验证TLS、两会话隔离、持久对象限速及CAS原子性、Secret不泄露、XSS防护、错误脱敏、安全清理、私有对象存储、无DB五输入、刷新恢复和重启/超时状态可查。生产S3条件写缺失或失败不得静默回退。后续付费能力另须通过费用预留/结算、防滥用及所需搜索MySQL/Chroma私网和恢复验收，不能把旧邀请码或MySQL强加到当前输入。本地已验证项见§5.7，生产环境和后续能力仍需独立验收；公网主页可达不等于安全或可用验收通过。

---

# 第六部分：比赛排期、阶段文件与交付

## 6.1 优先级

P0：批准本版范围、部署拓扑与资源费用、API/Schema安全契约、模型/Embedding配置、调用预算及评分政策；阶段0先验证部署关键路径。

P1：真实端到端主流程、五种输入、确认修改、A/B/C搜索、证据链、Python排序、一次修复、公开安全、普通测试、公开部署、视频和参赛材料。

P2：D10及课程实验、OCR、源码验证、复杂查询扩展/融合、完整账号系统、多模型、复杂Agent循环、分布式队列、大规模优化。P2不占比赛最后冲刺时间。

## 6.2 倒排进度（单人后端，依赖顺序不变）

| 时间（2026，暂按UTC+8） | 阶段与目标 | 交付和验收闸门 |
|---|---|---|
| 11月21日 | 审核计划与比赛要求 | 确认独立后端可用、模型/预算、安全方式、时区和材料规格；本轮只文档 |
| 11月22日 | 阶段0：最小部署验证 | Sites连FastAPI；无DB输入启动；文件对象持久化/开发跨进程CAS/生产S3条件写、会话/CORS及恢复待验证；外部API按所需阶段验证 |
| 11月23日 | 阶段1：契约与输入到确认 | 保留当前Phase1 API/Schema；无MySQL/SQLite五输入、聚合task元数据及完整文档、刷新恢复/中断/超时待验证；后续真实提取、Revision编辑确认及来源校验目标保留 |
| 11月24日 | 阶段2：搜索与README | 首次接入搜索专用MySQL并完成真实迁移/隔离集成；A/B/C、GitHub分页/限额/去重/初筛、README快照与失败状态 |
| 11月25日 | 阶段3：证据与分析 | Chunk映射、真实Embedding/Chroma、EvidenceBundle、逐项分析、硬约束判断；不跨仓库/快照 |
| 11月26日 | 阶段4：端到端闭环 | Python评分、Draft/Review/最多一次Repair、推荐与证据页；至少一条真实链在目标部署环境跑通 |
| 11月27日 | 阶段5：公开安全与异常回归 | 两会话隔离、限额、清理、重启、所有§5.8异常；五输入浏览器验收；不增加新高级功能 |
| 11月28日 | 阶段6：公开候选版 | 验证Sites发布状态和访客访问；独立后端真实E2E；邀请他人测试；冻结主要功能；备份恢复演练 |
| 11月29日 | 阶段7：演示与材料 | 录真实操作视频、截图、产品/技术/Qoder使用说明、已知限制和提交草稿；复查无敏感信息 |
| 11月30日12:00前 | 发布冻结 | 仅修阻塞问题，核对公开URL、资源余额/保留期、录像播放和材料一致性 |
| 11月30日16:00前 | 内部提交目标 | 按核实后的官方入口完成提交并保存回执，预留2小时处理上传/网络问题 |
| 11月30日18:00前 | 最终目标 | 公开产品、视频、材料及有效提交状态齐全；不可把准备好文件等同已提交 |

上述是目标排期，不是保证已具备资源。阶段0失败优先解决，不同时铺开未验证基础上的所有模块。11月26日仍未闭环时，只保留必要UI、单模型、小候选预算，推迟美化和高级筛选；不得削减证据真实性、安全闸门或用Mock替代真实功能。任何继续删减五种输入等核心功能须单独获批。最晚11月28日报告仍未解决的上线阻塞，不能用“差不多完成”掩盖风险。

每阶段均先说明拟改文件、目标、验收，再获准实施。后端由一人维护；前端可按冻结API协作，不做多人后端职责拆分。

## 6.3 获批后的第一批文件（阶段0）

以下为历史第一批文件规划，经本次存储边界调整后保留；不是要求重新创建已有Phase1代码或Schema。本轮不创建或修改代码文件；后续应复用现有实现，仅补足获批且待验证的能力。

- `.gitignore`、`.env.example`：Secret/运行对象排除、后端/LocalStorage持久目录/私有S3/API base配置；MySQL仅后续搜索启用，不能成为输入必填配置。
- `backend/pyproject.toml`、依赖锁定文件、`app/main.py`、`core/settings.py`、`core/errors.py`、`core/logging.py`。
- `backend/app/api/health.py`、最小`api/sessions.py`和`core/security.py`；诊断不得开放任意查询/写文件。
- 现有文件对象存储、会话/任务/文档/限速模块：复用并验证持久目录、跨进程锁+原子替换CAS、私有S3 ETag条件写及无回退，不建会话/诊断数据库表。
- 部署/会话/输入集成测试：隔离对象存储资源，覆盖无DB启动、五输入、完整文档、刷新恢复、冲突及未知写结果；`database.py`、搜索表映射、Alembic迁移及真实MySQL测试仅搜索阶段按需实施。
- `frontend/package.json`、锁文件、Vite/TypeScript配置、`index.html`、`src/main.tsx`、最小页面及API客户端；仅`dist`供Sites发布。
- 外部后端确需的启动/部署配置在托管商确认后列出，不预建多套Docker/云配置；Sites描述符由平台工具生成，不手写。

阶段0结束后停用或移除临时诊断入口，保留受控测试脚本。后续逐步创建统一schemas、解析器、提取Prompt、GitHub/Embedding/VectorStore服务、工作流、评分/引用测试和页面，不一次生成空服务或假推荐接口。

## 6.4 最终比赛交付与验收

1. 稳定公开Sites前端及实际后端，两者连通；验证真实访客浏览器，不仅开发者登录状态。
2. 五种输入到需求确认、GitHub真实发现、README证据、Python排名、审核推荐可运行；空结果和依赖失败不崩溃、不伪造。
3. 证据面板能回到保存README原文；明确仅文档验证，未执行第三方仓库。
4. 公开安全与普通产品测试证据、已知限制、部署/运行说明和无密钥配置样例；单列无MySQL/SQLite五输入、sessionStorage刷新恢复、重启中断/状态超时可查、完整文档和聚合task持久化、跨进程CAS及S3条件写失败处理。文件对象备份恢复须包含会话/限额/版本/运行状态，后续搜索再关联真实MySQL/README/Chroma；本地输入验证见§5.7，生产及完整备份恢复验收仍待完成。
5. 演示视频展示真实流程、一个修改需求版本、引用定位和必要错误提示；可剪辑等待但标注，不冒充实时速度。
6. 参赛说明包含问题、目标用户、创新点、架构、Qoder实际使用记录、公开地址、演示视频及官方要求材料。格式、时长、仓库权限以核实后的官方规则为准，不虚构官方要求。
7. 提交回执和截止前URL可用性检查；云主机、模型余额、域名和持久卷至少覆盖评审期，具体评审期待官方确认。

详细操作清单见同目录 `ProjectPilot_Competition_Checklist.md`；所有未执行验收保持未勾选。

---

# 第七部分：待确认事项、依据与更新记录

## 7.1 审核优先项

| 优先项 | 已明确 | 本版建议/仍需确认 |
|---|---|---|
| 比赛范围 | StudyHack优先，D10不在本次范围 | 官方入口、规则、时区、评审期、视频/材料规格 |
| 部署 | 必须尽早验证Sites，输入不依赖数据库 | Sites前端+独立FastAPI规则；持久目录/私有S3条件写能力、费用与发布权限；MySQL资源后续搜索确认 |
| LLM | 用户倾向deepseek-flash；官方列出ID | 官方账号可用性、结构化模式、关闭不必要推理后的质量；首选不等于已实测 |
| Embedding | 一种模型，不复杂切换 | 托管BAAI/bge-m3的供应商/部署ID、维度、价格、可达性；11月22日前决定 |
| 数据库 | MySQL 8.4 LTS、SQLAlchemy、Alembic仅后续GitHub搜索；输入无MySQL/SQLite | 搜索阶段确认地址/权限、驱动、隔离测试库及真实迁移；不在聊天提供密码 |
| 文件对象 | 会话所有task元数据及完整文档聚合JSON，原件独立保存；开发跨进程锁+原子替换CAS，生产私有S3条件写 | 任务数可配置、本地CAS及故障测试通过；JSON大小/总存储上限、自动清理、完整备份恢复及真实S3仍待完成 |
| 安全 | 当前Phase1匿名独立会话优先；隔离、限速及刷新恢复保留 | 后续付费邀请码或等效防滥用另设闸门，不强加到当前输入 |
| 预算 | 必须有调用/时间/金额/存储上限 | 批准§5.2/5.3/5.9初始参数及人民币预算；实际价格配置 |
| RequirementItem | 12字段不变，modality/ALL/ANY/source/ID明确 | 批准禁止条件字段映射、单层逻辑和反证组合契约 |
| 排名 | Pass/Unknown/Fail分开，Stars不盖过硬条件 | 批准分区、文档支持映射、权重（等权仅建议）、展示措辞 |
| 数据保留 | 临时清理与用户任务隔离必须实现 | 建议会话24小时、正文/结果7天、备份保留及隐私告知 |

未解决部署或模型资源时可以审阅契约，但不能报告环境已可用；未批准评分权重时不发布伪确定性分数。

## 7.2 保留的约束与已处理冲突

- 历史SQLite→MySQL修订由最新用户确认进一步收窄：MySQL仅后续GitHub项目搜索数据，输入及通用用户状态使用文件对象，不使用MySQL/SQLite，也不以artifacts表偷存全部用户数据。
- 原D10最终交付被本轮明确调整为未来方向；没有删除产品本身的快照/证据与普通测试。
- 健康路由以`/api/v1/health`为准；Sites平台同名路径不意味着代理到了后端。
- interrupted使用failed+RUN_INTERRUPTED，不声称后台队列可靠恢复。
- 三态与反证需要明确组合契约；引用存在并不等于支持声明，语义Review和用户核查仍有必要。
- 文件对象CAS仅保证单对象条件更新，跨对象及后续MySQL/README/Chroma无共同事务；冲突只重试纯状态变更，未知写结果不能盲重放/盲删，需核对状态与引用。
- 当前Phase1 API及 `backend/app/schemas/documents.py` 契约完整保留；sessionStorage刷新恢复不等于后台重启续跑。匿名输入不附加旧邀请码门槛，付费能力另审。
- 本轮未读取赛事官方要求，不预设采用Sites前端就一定满足比赛规则。

## 7.3 历史只读资料依据与本轮契约核对

以下外部资料为历史2026-11-21读取记录，本轮未重新验证平台/账号状态。本轮保持 `backend/app/schemas/documents.py` 当前契约；本地存储实现的验证依据见§5.7，不替代真实云端验收。

- [Sites静态部署与保留路由](file:///D:/Qoder/.qoder-versions/0.5.17/resources/extensions/qoder.sites/dist/plugin/skills/sites-building/references/static-site.md)：静态dist、SPA、`/api`保留、Vite生产边界。
- [Sites Functions与Secrets](file:///D:/Qoder/.qoder-versions/0.5.17/resources/extensions/qoder.sites/dist/plugin/skills/sites-building/references/functions.md)：Edge/index.ts、无常驻后端假设、应用身份与Secret边界。
- [Sites发布流程](file:///D:/Qoder/.qoder-versions/0.5.17/resources/extensions/qoder.sites/dist/plugin/skills/sites-hosting/references/deployment.md)：构建/准备/发布/实际验证分开，工具不自动安装依赖或迁移数据库。
- [Sites Function后台契约](file:///D:/Qoder/.qoder-versions/0.5.17/resources/extensions/qoder.sites/dist/plugin/skills/sites-building/references/function-backend.md)：平台SDK与业务授权边界，不能推定为MySQL运行时。
- [DeepSeek模型列表](https://api-docs.deepseek.com/api/list-models)：列出deepseek-flash，需账号实际验证。
- [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)：协议、text.format和参数支持；不能依赖被忽略的参数实现预算控制。

## 7.4 更新记录

| 日期/版本 | 更新内容 | 保留与影响 |
|---|---|---|
| 2026-11-21 初始计划 | 完整产品/契约/API/七阶段计划 | 原任务书唯一产品基准 |
| 2026-11-21 MySQL修订 | SQLite改MySQL，加入SQLAlchemy/Alembic、池/事务/真实MySQL测试 | 未改任务书、未建库/迁移 |
| 2026-11-21 StudyHack修订（本版） | 首要目标改比赛；D10及固定语料/金标准/Baseline/V1/V2/V3/V4/课程指标全部移出当前阶段 | 保留普通产品测试和运行快照，不重新设计产品 |
| 同上 | 增加阶段0部署兼容矩阵与早期闸门；明确Sites前端与独立Python后端边界 | 不宣称Sites原生托管FastAPI/MySQL/Chroma，资源与规则仍待确认 |
| 同上 | 明确deepseek-flash首选、单Embedding提案、JSON校验、超时/重试/成本 | 未使用密钥、未付费调用，模型部署参数待批准 |
| 同上 | 完善RequirementItem、硬约束分区、软评分、引用原文追溯及单轮Repair | 12字段不变；权重未擅自确认为最终值 |
| 同上 | 增加公开会话/任务隔离、频率金额限额、清理与测试；简化关系表+版本化JSON | 无完整注册系统不代表无鉴权；新增安全接口须批准 |
| 同上 | 按11月30日18:00倒排，前置部署、11月26日闭环、29日视频材料、30日留提交缓冲 | 不以D10实验作为比赛交付，不承诺未经验证的时区和赛事条款 |
| 同上 | 新增ProjectPilot_Competition_Checklist.md | 所有实施/部署/提交项待执行，不虚报完成 |
| 最新输入持久化边界修订（本轮） | 按用户确认收窄MySQL到后续GitHub搜索；会话/聚合task/文档/版本/运行/限速改为文件对象职责；明确开发跨进程锁+原子替换CAS、生产私有S3 ETag条件写及未知结果核对 | 已修改输入实现、测试、配置及两份计划；保留API/Schema，72项本地测试、前端构建及浏览器五输入/重启刷新恢复通过；真实S3和生产配额待验收，MySQL移到搜索阶段；历史条目不代表旧通用表方案仍有效 |

本轮验证：`.venv/Scripts/python.exe -m pytest backend/tests -q` 为72 passed（1项第三方弃用警告）；前端 `npm run build` 通过。正常开发LocalStorage后端的浏览器五输入、来源预览、多文件拒绝及后端重启/页面刷新恢复通过，5份完整文档和原件哈希已核对。未连接真实S3、未部署或执行数据库迁移；生产配额、自动清理及后续搜索仍按阶段实施。

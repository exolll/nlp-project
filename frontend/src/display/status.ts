export type StatusTone = 'success' | 'warning' | 'danger' | 'neutral' | 'info'

export interface StatusDisplay {
  label: string
  tone: StatusTone
  raw: string
}

export type StatusCategory =
  | 'run' | 'plan' | 'search' | 'evidence' | 'retrieval'
  | 'deepPhase' | 'review' | 'verdict' | 'hardCheck' | 'reason'
  | 'repository' | 'coverage' | 'availability' | 'evidenceItem' | 'collection'
  | 'modality' | 'operator' | 'tier' | 'recommendation' | 'preferred'
  | 'rankingRule' | 'unitKind' | 'unitResult' | 'field'

type Row = Omit<StatusDisplay, 'raw'>
const e = (label: string, tone: StatusTone): Row => ({ label, tone })

const run: Record<string, Row> = {
  reserved: e('已受理', 'info'), queued: e('排队中', 'info'), running: e('进行中', 'info'),
  completed: e('已完成', 'success'), partial: e('部分完成', 'warning'), failed: e('失败', 'danger'),
  interrupted: e('已中断', 'warning'), not_started: e('未开始', 'neutral'),
}

const maps: Record<StatusCategory, Record<string, Row>> = {
  run,
  plan: {
    not_started: e('未开始', 'neutral'), queued: e('排队中', 'info'), running: e('规划中', 'info'),
    published: e('已发布', 'success'), partial: e('部分完成', 'warning'), failed: e('规划失败', 'danger'),
    interrupted: e('已中断', 'warning'),
  },
  search: {
    not_started: e('未开始', 'neutral'), queued: e('排队中', 'info'), running: e('搜索中', 'info'),
    completed: e('搜索完成', 'success'), partial: e('部分完成', 'warning'), failed: e('搜索失败', 'danger'),
    interrupted: e('已中断', 'warning'),
  },
  evidence: {
    not_started: e('未开始', 'neutral'), not_ready: e('未发布', 'neutral'), ready: e('已发布', 'success'),
    published: e('已发布', 'success'), partial: e('部分发布', 'warning'), failed: e('发布失败', 'danger'),
  },
  retrieval: {
    not_started: e('未生成', 'neutral'), running: e('正在生成', 'info'), ready: e('已就绪', 'success'),
    ok: e('检索完成', 'success'), partial: e('部分就绪', 'warning'), failed: e('生成失败', 'danger'),
    empty: e('没有可检索快照', 'neutral'),
  },
  deepPhase: {
    not_started: e('未授权', 'neutral'), running: e('正在核对', 'info'), ready: e('已发布', 'success'),
    failed: e('未完成', 'danger'), interrupted: e('已中断', 'warning'), blocked: e('完整性校验失败', 'danger'),
  },
  review: {
    running: e('正在核查', 'info'), completed: e('已完成', 'success'), partial: e('部分完成', 'warning'),
    failed: e('失败', 'danger'), unknown: e('结果未知', 'warning'),
  },
  verdict: {
    supports: e('有证据支持', 'success'), contradicts: e('有证据冲突', 'warning'), insufficient: e('信息不足', 'warning'),
  },
  hardCheck: {
    pass: e('通过', 'success'), fail: e('未通过', 'danger'), unknown: e('未知', 'warning'),
    not_applicable: e('不适用', 'neutral'),
  },
  reason: {
    supported_by_citation: e('有效引文支持', 'success'), conflicted_by_citation: e('有效引文显示冲突', 'warning'),
    not_found_in_provided_evidence: e('所提供证据中未找到', 'neutral'), citation_rejected: e('引文未通过校验', 'warning'),
    model_output_incomplete: e('模型输出不完整', 'warning'),
  },
  repository: {
    completed: e('已完成', 'success'), failed: e('失败', 'danger'), unknown: e('结果未知', 'warning'),
    skipped: e('已跳过', 'neutral'),
  },
  coverage: {
    full: e('README 全文', 'success'), truncated: e('证据覆盖不完整', 'warning'),
    non_utf8: e('非 UTF-8 文本', 'warning'), empty: e('README 为空', 'neutral'),
  },
  availability: {
    available: e('已保存快照可读', 'success'), snapshot: e('已保存快照', 'success'),
    missing: e('无可读快照', 'warning'), unavailable: e('不可读取', 'warning'),
  },
  evidenceItem: {
    mention_found: e('发现相关提及', 'warning'), unconfirmed: e('尚未核实', 'neutral'),
  },
  collection: {
    not_started: e('未采集', 'neutral'), completed: e('采集完成', 'success'),
    partial: e('部分采集', 'warning'), failed: e('采集失败', 'danger'), skipped: e('已跳过', 'neutral'),
  },
  modality: {
    required: e('必需项', 'info'), preferred: e('偏好项', 'neutral'), prohibited: e('禁止项', 'warning'),
    informational: e('参考信息', 'neutral'),
  },
  operator: {
    ALL: e('全部满足', 'neutral'), ANY: e('任一满足', 'neutral'), AND: e('全部满足', 'neutral'),
    not_applicable: e('不适用', 'neutral'),
  },
  tier: {
    meets_requirements: e('符合要求', 'success'), needs_review: e('待进一步核查', 'warning'),
    hard_conflict: e('存在硬约束冲突', 'danger'),
  },
  recommendation: {
    recommended: e('推荐', 'success'), needs_review: e('待进一步核查', 'warning'),
    not_recommended: e('不推荐', 'danger'),
  },
  preferred: {
    supports: e('有支持', 'success'), contradicts: e('有反证', 'warning'),
    insufficient: e('证据不足', 'warning'), mixed: e('支持与反证并存', 'warning'),
  },
  rankingRule: {
    'hard-tier-preferred-v1': e('硬约束优先 + 偏好证据（v1）', 'neutral'),
    'hard-tier-score-v2': e('硬约束优先 + 加权得分（v2）', 'neutral'),
  },
  unitKind: {
    required: e('必需项', 'neutral'), prohibited: e('禁止项', 'neutral'), logic_group: e('逻辑组', 'neutral'),
  },
  unitResult: {
    pass: e('通过', 'success'), fail: e('未通过', 'danger'), unknown: e('未知', 'warning'),
  },
  field: {
    project_topic: e('项目主题', 'neutral'), project_description: e('项目描述', 'neutral'),
    project_type: e('项目类型', 'neutral'), domain: e('领域', 'neutral'),
    required_features: e('必需功能', 'neutral'), preferred_features: e('偏好功能', 'neutral'),
    programming_languages: e('编程语言', 'neutral'), required_technologies: e('必需技术', 'neutral'),
    preferred_technologies: e('偏好技术', 'neutral'),
  },
}

export function statusOf(category: StatusCategory, value: string | null | undefined): StatusDisplay {
  const raw = value ?? ''
  if (!raw) return { label: '未提供', tone: 'neutral', raw: '' }
  const row = maps[category][raw]
  return row ? { ...row, raw } : { label: '未知状态', tone: 'neutral', raw }
}
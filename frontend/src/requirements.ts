import type { ApiError, NormalizedDocument } from './api'

export type SourceKind = 'assignment_document' | 'user_primary_text' | 'user_supplement' | 'user_revision'
export interface UncertainItem { field: string; requirement_item_ids: string[]; reason: string; question: string; evidence_ids: string[] }
export interface Evidence {
  evidence_id: string; field: string; original_text: string; source_kind: SourceKind; source_location: string
  requirement_item_id: string | null; segment_id: string | null; start_offset: number | null; end_offset: number | null
}
export interface RequirementJSON {
  project_topic: string | null
  project_description: string | null
  project_type: string | null
  domain: string | null
  required_features: string[] | null
  preferred_features: string[] | null
  programming_languages: string[] | null
  required_technologies: string[] | null
  preferred_technologies: string[] | null
  missing_information: string[]
  uncertain_information: UncertainItem[] | null
  source_evidence: Evidence[]
}
export interface RequirementItem {
  requirement_item_id: string; field: string; value_path: string; text: string
  modality: 'required' | 'preferred' | 'prohibited' | 'informational'
  source_kind: SourceKind; evidence_ids: string[]; logic_group_id: string | null
  operator: 'ALL' | 'ANY' | 'not_applicable'; supersedes_item_id: string | null
}
export type ItemEdit = Omit<RequirementItem, 'requirement_item_id'> & { requirement_item_id: string | null }
export interface ExtractionJob {
  extraction_id: string; status: 'queued' | 'extracting' | 'validating' | 'completed' | 'failed'
  error: ApiError | null; model: string; prompt_version: string; usage: Record<string, unknown> | null; created_at: string
  // Sanitised validator location; present only when the model output failed validation.
  diagnostic?: { validator: string; field: string | null } | null
}
export interface Revision {
  revision_id: string; task_id: string; revision_number: number; parent_revision_id: string | null
  status: 'extracted' | 'draft' | 'confirmed'; requirements: RequirementJSON; requirement_items: RequirementItem[]
  change_log: unknown; created_at: string; confirmed_at: string | null
}
export interface RequirementsResponse { extraction: ExtractionJob | null; revisions: Revision[]; latest_revision_id: string | null }
export interface ExtractionAttempt { key: string; state: 'unknown' | 'accepted' | 'completed' | 'failed'; baseline_id: string | null; extraction_id: string | null }
export function settleAttempt(saved: ExtractionAttempt | null, extraction: ExtractionJob | null): ExtractionAttempt | null {
  if (!saved || !extraction) return saved
  // GET exposes no key: an old job cannot settle a new request with unknown outcome.
  const matches = saved.extraction_id ? saved.extraction_id === extraction.extraction_id : extraction.extraction_id !== saved.baseline_id
  if (!matches) return saved
  return { ...saved, extraction_id: extraction.extraction_id, state: extraction.status === 'failed' ? 'failed' : extraction.status === 'completed' ? 'completed' : 'accepted' }
}
export function mayStartExtraction(job: ExtractionJob | null, attempt: ExtractionAttempt | null): boolean {
  if (isPending(job) || attempt?.state === 'unknown' || attempt?.state === 'accepted') return false
  return job ? job.status === 'failed' : !attempt || attempt.state === 'failed'
}
export const fields = [
  { key: 'project_topic', label: '项目主题', list: false },
  { key: 'project_description', label: '项目描述', list: false },
  { key: 'project_type', label: '项目类型', list: false },
  { key: 'domain', label: '应用领域', list: false },
  { key: 'required_features', label: '必需功能', list: true },
  { key: 'preferred_features', label: '偏好功能', list: true },
  { key: 'programming_languages', label: '编程语言', list: true },
  { key: 'required_technologies', label: '必需技术', list: true },
  { key: 'preferred_technologies', label: '偏好技术', list: true },
] as const
export type RequirementField = typeof fields[number]['key']
export interface Draft {
  requirements: RequirementJSON; items: ItemEdit[]; text: Record<RequirementField, string>
  missingText: string; reason: string; resolved: boolean; editing: boolean
  // Temporarily retain metadata when toggling a field to null and back.
  parked: Partial<Record<RequirementField, ItemEdit[]>>
}
export const lines = (value: string): string[] => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
export const isPending = (job: ExtractionJob | null): boolean => !!job && ['queued', 'extracting', 'validating'].includes(job.status)
export function createDraft(revision: Revision): Draft {
  return {
    requirements: structuredClone(revision.requirements), items: structuredClone(revision.requirement_items),
    text: Object.fromEntries(fields.map(({ key }) => [key, Array.isArray(revision.requirements[key]) ? revision.requirements[key].join('\n') : revision.requirements[key] ?? ''])) as Record<RequirementField, string>,
    missingText: revision.requirements.missing_information.join('\n'), reason: '', resolved: false,
    editing: revision.status !== 'confirmed', parked: {},
  }
}

// Reserve all exact text matches before matching changed rows by their previous index.
// This preserves identities on reorder, including duplicate text, rather than stealing
// an unchanged row's metadata for a newly inserted row.
export function reconcileItems(field: RequirementField, value: string | string[] | null, items: ItemEdit[]): ItemEdit[] {
  const previous = items.filter(item => item.field === field)
  const texts = value === null ? [] : Array.isArray(value) ? value : value.trim() ? [value] : []
  const path = (index: number) => Array.isArray(value) ? `${field}[${index}]` : field
  const used = new Set<number>()
  const matches = texts.map((text, index) => {
    const match = previous.findIndex((item, i) => !used.has(i) && item.text === text && item.value_path === path(index))
    if (match >= 0) used.add(match)
    return match
  })
  // Preserve duplicate rows at their original indices before using text-only matches.
  texts.forEach((text, index) => {
    if (matches[index] >= 0) return
    const match = previous.findIndex((item, i) => !used.has(i) && item.text === text)
    if (match >= 0) { matches[index] = match; used.add(match) }
  })
  const next = texts.map((text, index): ItemEdit => {
    let match = matches[index]
    if (match < 0) {
      match = previous.findIndex((item, i) => !used.has(i) && item.value_path === path(index))
      if (match >= 0) used.add(match)
    }
    const original = match >= 0 ? previous[match] : null
    // Do not synthesize evidence or new IDs. The server versions changed items.
    return original ? { ...original, field, value_path: path(index), text } : {
      requirement_item_id: null, field, value_path: path(index), text,
      modality: field.startsWith('required_') ? 'required' : field.startsWith('preferred_') ? 'preferred' : 'informational',
      source_kind: 'user_revision', evidence_ids: [], logic_group_id: null,
      operator: 'not_applicable', supersedes_item_id: null,
    }
  })
  return [...items.filter(item => item.field !== field), ...next]
}
export function updateField(draft: Draft, field: RequirementField, text: string, absent: boolean): Draft {
  const list = fields.find(item => item.key === field)!.list
  const value = absent ? null : list ? lines(text) : text
  const parked = { ...draft.parked }
  let items = draft.items
  if (absent && draft.requirements[field] !== null) parked[field] = items.filter(item => item.field === field)
  if (!absent && draft.requirements[field] === null && parked[field]) items = [...items.filter(item => item.field !== field), ...parked[field]!]
  return { ...draft, text: { ...draft.text, [field]: text }, requirements: { ...draft.requirements, [field]: value }, items: reconcileItems(field, value, items), resolved: false, parked }
}
function comparableItems(items: ItemEdit[]): string {
  return JSON.stringify([...items].sort((a, b) => a.field.localeCompare(b.field) || a.value_path.localeCompare(b.value_path) || (a.requirement_item_id ?? '').localeCompare(b.requirement_item_id ?? '')))
}
export function meaningChanged(draft: Draft, revision: Revision): boolean {
  return fields.some(({ key }) => JSON.stringify(draft.requirements[key]) !== JSON.stringify(revision.requirements[key])) || comparableItems(draft.items) !== comparableItems(revision.requirement_items)
}
export function revisionPayload(draft: Draft, revision: Revision) {
  return {
    requirements: {
      ...draft.requirements,
      missing_information: lines(draft.missingText),
      uncertain_information: draft.resolved && meaningChanged(draft, revision) ? null : revision.requirements.uncertain_information,
      // Original evidence is passed through unchanged; only the server may rebuild it.
      source_evidence: revision.requirements.source_evidence,
    },
    requirement_items: draft.items,
    change_reason: draft.reason.trim(),
  }
}
export function contentChanged(draft: Draft, revision: Revision): boolean {
  return meaningChanged(draft, revision) || JSON.stringify(revisionPayload(draft, revision).requirements) !== JSON.stringify(revision.requirements)
}
export function languageGroupIssue(items: ItemEdit[]): string | null {
  const languages = items.filter(item => item.field === 'programming_languages' && item.modality === 'required')
  if (languages.length < 2) return null
  if (languages.some(item => !item.logic_group_id?.trim() || item.operator === 'not_applicable')) return '多个必需编程语言需要明确同组 ALL / ANY，请逐项设置或使用语言组工具。'
  if (languages.some(item => item.logic_group_id !== languages[0].logic_group_id || item.operator !== languages[0].operator)) return '必需语言需要相同组名与一致的 ALL / ANY，请明确是全部使用还是任选其一。'
  return null
}
export function draftIssues(draft: Draft, revision: Revision): string[] {
  const issues: string[] = []
  for (const field of fields) {
    const value = draft.requirements[field.key]
    if (!field.list && value !== null && !(value as string).trim()) issues.push(`${field.label}：服务端要求非空文本；无信息请勾选“未提供”。`)
  }
  for (const item of draft.items) {
    if (item.field.startsWith('required_') && !['required', 'prohibited'].includes(item.modality)) issues.push(`${item.text}：必需字段只能标为“必须”或“禁止”；偏好请移到对应偏好字段。`)
    if (item.field.startsWith('preferred_') && item.modality !== 'preferred') issues.push(`${item.text}：偏好字段应标为“偏好”；硬约束请移到对应必需字段。`)
    if (fields.some(field => !field.list && field.key === item.field) && item.modality !== 'informational') issues.push(`${item.text}：主题、描述、类型、领域属于“信息说明”。`)
    if (item.operator === 'not_applicable' ? item.logic_group_id !== null : !item.logic_group_id?.trim()) issues.push(`${item.text}：ALL / ANY 必须填写逻辑组；选择“不适用”时请清空组名。`)
    if (item.logic_group_id && draft.items.some(other => other.logic_group_id === item.logic_group_id && other.operator !== item.operator)) issues.push(`逻辑组“${item.logic_group_id}”中的操作符需要一致。`)
  }
  if (!(draft.resolved && meaningChanged(draft, revision))) {
    const ids = new Set(draft.items.map(item => item.requirement_item_id))
    if (revision.requirements.uncertain_information?.some(item => item.requirement_item_ids.some(id => !ids.has(id)))) issues.push('删除的条目仍被歧义引用。请解决歧义并勾选核对框，或保留相关条目后保存。')
  }
  return [...new Set(issues)]
}
export function evidenceSlice(evidence: Evidence, document: NormalizedDocument): { segment: NormalizedDocument['source_segments'][number] | undefined; quote: string | null; matches: boolean } {
  const segment = document.source_segments.find(item => item.segment_id === evidence.segment_id)
  const points = segment ? Array.from(segment.original_content) : []
  const { start_offset: start, end_offset: end } = evidence
  const valid = !!segment && start !== null && end !== null && Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= points.length
  const quote = valid ? points.slice(start!, end!).join('') : null
  return { segment, quote, matches: quote !== null && quote === evidence.original_text }
}

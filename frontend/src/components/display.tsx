import { useState } from 'react'
import type { ReactNode } from 'react'
import type { StatusDisplay } from '../display/status'
import type { RequirementViewModel } from '../display/viewModels'

const toneClass: Record<StatusDisplay['tone'], string> = {
  success: 'success', warning: 'warn', danger: 'danger', neutral: 'neutral', info: 'info',
}

export function StatusBadge({ status }: { status: StatusDisplay }) {
  return <span className={`badge display-badge ${toneClass[status.tone]}`.trim()}>{status.label}</span>
}

export function SummaryCard({ eyebrow, title, statuses = [], description, children }: {
  eyebrow: string
  title: string
  statuses?: StatusDisplay[]
  description?: string
  children?: ReactNode
}) {
  return (
    <section className="summary-card">
      <div className="summary-head">
        <span className="summary-eyebrow">{eyebrow}</span>
        {statuses.length > 0 && (
          <div className="summary-statuses">{statuses.map((status, index) => <StatusBadge key={`${status.label}-${index}`} status={status} />)}</div>
        )}
      </div>
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {children}
    </section>
  )
}

export function MetricGrid({ items }: { items: { label: string; value: string | number; hint?: string }[] }) {
  return (
    <div className="metric-grid">
      {items.map(item => (
        <div className="metric-card" key={item.label}>
          <span>{item.label}</span>
          <strong>{item.value}</strong>
          {item.hint && <small>{item.hint}</small>}
        </div>
      ))}
    </div>
  )
}

export interface EvidenceCardModel {
  requirementRef: string
  lineStart: number
  lineEnd: number
  quote: string
  readmePath: string | null
}

export function EvidenceCard({ evidence }: { evidence: EvidenceCardModel }) {
  return (
    <article className="evidence-card">
      <p>
        {evidence.requirementRef} · 行 {evidence.lineStart}–{evidence.lineEnd}
        {evidence.readmePath && <> · <a href={evidence.readmePath} target="_blank" rel="noreferrer">查看 README 原文</a></>}
      </p>
      <pre className="source-quote">{evidence.quote}</pre>
    </article>
  )
}

export function RequirementMatchCard({ requirement, onOpenEvidence }: {
  requirement: RequirementViewModel
  onOpenEvidence?: (requirement: RequirementViewModel) => void
}) {
  return (
    <article className="requirement-match">
      <header>
        <strong>{requirement.ref} · {requirement.text}</strong>
        <StatusBadge status={requirement.verdict} />
      </header>
      <p className="muted">
        {requirement.modality.label} · {requirement.reason.label} · 引文 {requirement.citationCount} 条
        {requirement.rejectedCount > 0 && ` · 未通过校验 ${requirement.rejectedCount} 条`}
      </p>
      {requirement.modelOmitted && <p className="hint-text">模型遗漏，已按确定性规则补为信息不足。</p>}
      {requirement.citationCount > 0 && onOpenEvidence && (
        <button type="button" className="text-button" onClick={() => onOpenEvidence(requirement)}>查看证据（{requirement.citationCount}）</button>
      )}
    </article>
  )
}

function CopyValue({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      if (!navigator.clipboard) return
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return <button type="button" className="copy-value text-button" onClick={() => { void copy() }}>{copied ? '已复制' : '复制'}</button>
}

export function AuditDetails({ title = '技术审计信息', fields, children }: {
  title?: string
  fields: { label: string; value: string }[]
  children?: ReactNode
}) {
  return (
    <details className="technical-details">
      <summary>{title}</summary>
      {fields.length > 0 && (
        <dl>
          {fields.map((field, index) => (
            <div key={`${field.label}-${index}`}>
              <dt>{field.label}</dt>
              <dd><code>{field.value}</code><CopyValue value={field.value} /></dd>
            </div>
          ))}
        </dl>
      )}
      {children}
    </details>
  )
}

export function EmptyState({ title, description, children }: { title: string; description: string; children?: ReactNode }) {
  return (
    <div className="requirements-empty display-empty">
      <h3>{title}</h3>
      <p>{description}</p>
      {children}
    </div>
  )
}

export function TraceabilityPanel({ groups }: { groups: { title: string; items: string[] }[] }) {
  return (
    <section className="traceability-panel">
      {groups.map((group, groupIndex) => (
        <div key={`${group.title}-${groupIndex}`}>
          <h5>{group.title}</h5>
          <ul>{group.items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
        </div>
      ))}
    </section>
  )
}
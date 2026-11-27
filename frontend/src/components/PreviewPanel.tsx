import { codePoints } from '../api'
import type { NormalizedDocument } from '../api'

export default function PreviewPanel({ doc, tab, onTab, busy }: {
  doc: NormalizedDocument | null
  tab: 'text' | 'segments'
  onTab: (tab: 'text' | 'segments') => void
  busy: boolean
}) {
  return <section className="card preview-card" id="preview-panel" aria-labelledby="preview-title">
    <div className="card-title">
      <div><span className="section-index">02 / PREVIEW</span><h2 id="preview-title">标准化预览</h2></div>
      <span className={`badge ${doc ? 'success' : ''}`}>{doc ? '解析完成' : '等待材料'}</span>
    </div>
    <div className="tabs" role="tablist" aria-label="预览内容">
      <button type="button" role="tab" aria-selected={tab === 'text'} onClick={() => onTab('text')}>normalized_text</button>
      <button type="button" role="tab" aria-selected={tab === 'segments'} onClick={() => onTab('segments')}>source_segments</button>
    </div>
    {!doc
      ? <div className="empty">
          <div className="paper-icon" aria-hidden="true"><i /><i /><i /></div>
          <h3>{busy ? '正在整理你的材料' : '清晰的输入，从这里呈现'}</h3>
          <p>提交材料后，在这里查看标准化文本<br />与每一段内容的原始来源。</p>
          <span>真实解析 · 来源可追溯</span>
        </div>
      : <div className="preview-content" role="tabpanel">
          <div className="doc-meta">
            <strong>{doc.original_filename || '文本需求'}</strong>
            <span>{doc.character_count} 字符 · {doc.total_pages === null ? '文本材料' : `${doc.total_pages} 页`} · {doc.input_mode}</span>
          </div>
          {doc.warnings.map((warning, i) => <p className="warning" key={i}>{warning}</p>)}
          {tab === 'text'
            ? <pre>{doc.normalized_text}</pre>
            : doc.source_segments.map((segment, index) => <article className="segment" key={segment.segment_id}>
                <div>
                  <b>{String(index + 1).padStart(2, '0')} · {segment.source_kind === 'assignment_document' ? '任务文档' : segment.source_kind === 'user_primary_text' ? '主输入文本' : '补充文本'}</b>
                  <small>码点 {segment.start_offset}–{segment.end_offset}</small>
                </div>
                <p>{codePoints(doc.normalized_text).slice(segment.start_offset, segment.end_offset).join('')}</p>
                <details>
                  <summary>查看来源与原文</summary>
                  <pre>{segment.original_content}</pre>
                  <code>{JSON.stringify({ source_location: segment.source_location, location_detail: segment.location_detail }, null, 2)}</code>
                </details>
              </article>)}
        </div>}
  </section>
}
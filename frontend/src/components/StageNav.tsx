import { Fragment } from 'react'
import type { StageView } from '../adapters/stages'

export default function StageNav({ stages, viewing, onSelect, interactive = true }: {
  stages: StageView[]
  viewing: number
  onSelect: (stage: StageView) => void
  interactive?: boolean
}) {
  return <nav className="stage-nav" aria-label="项目阶段">
    {stages.map((stage, index) => {
      const blocked = !interactive || (!stage.done && !stage.current)
      return <Fragment key={stage.key}>
        <button
          type="button"
          className={`stage-node${stage.done ? ' done' : ''}${stage.current ? ' current' : ''}${viewing === stage.index ? ' viewing' : ''}${stage.stale ? ' stale' : ''}`}
          aria-current={stage.current && interactive ? 'step' : undefined}
          aria-disabled={blocked || undefined}
          aria-label={`阶段 ${stage.index} · ${stage.label}${stage.done ? '（已完成）' : stage.current ? '（当前）' : '（未开始）'}${stage.stale ? '（待更新）' : ''}`}
          title={!interactive ? '请先提供项目材料' : blocked ? '请先完成前面的阶段' : `切换到阶段 ${stage.index} · ${stage.label} 子界面`}
          onClick={() => { if (!blocked) onSelect(stage) }}
        >
          <span className="stage-circle" aria-hidden="true">{stage.done ? '✓' : stage.index}</span>
          <span className="stage-name">{stage.label}</span>
          {stage.stale && <span className="stage-flag">待更新</span>}
        </button>
        {index < stages.length - 1 && <span className={`stage-seg${stage.done ? ' done' : ''}`} aria-hidden="true" />}
      </Fragment>
    })}
  </nav>
}
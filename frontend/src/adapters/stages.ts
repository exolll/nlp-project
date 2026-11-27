export interface StageDescriptor { index: number; key: string; label: string }

export const stageList: readonly StageDescriptor[] = [
  { index: 1, key: 'input', label: '输入' },
  { index: 2, key: 'extraction', label: '需求拆解' },
  { index: 3, key: 'confirm', label: '需求确认' },
  { index: 4, key: 'screen', label: 'GitHub 初筛' },
  { index: 5, key: 'deep', label: '深度筛选' },
  { index: 6, key: 'result', label: '最终结果' },
]

export interface RunSignals {
  confirmedRevisionId: string
  evidencePublished: boolean
  deepReviewPublished: boolean
  rankingAvailable: boolean
}

export interface StageSignals {
  hasTask: boolean
  inputCompleted: boolean
  extractionCompleted: boolean
  confirmedRevisionId: string | null
  run: RunSignals | null
}

export interface StageView extends StageDescriptor {
  done: boolean
  reachable: boolean
  stale: boolean
  current: boolean
}

export function deriveStages(signals: StageSignals): StageView[] {
  const own = [
    signals.hasTask && signals.inputCompleted,
    signals.extractionCompleted,
    signals.confirmedRevisionId !== null,
    !!signals.run?.evidencePublished,
    !!signals.run?.deepReviewPublished,
    !!signals.run?.rankingAvailable,
  ]
  // 上游未完成时下游不能算完成，避免出现倒挂的进度。
  const done: boolean[] = []
  own.forEach((value, index) => { done.push(value && (index === 0 || done[index - 1])) })
  const staleRun = !!signals.run && !!signals.confirmedRevisionId
    && signals.run.confirmedRevisionId !== signals.confirmedRevisionId
  const firstOpen = done.findIndex(value => !value)
  const currentIndex = firstOpen === -1 ? done.length - 1 : firstOpen
  return stageList.map((stage, index) => ({
    ...stage,
    done: done[index],
    reachable: done[index],
    stale: staleRun && index >= 3,
    current: index === currentIndex,
  }))
}
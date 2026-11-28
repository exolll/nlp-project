export type TaskGroup = 'today' | 'yesterday' | 'week' | 'earlier'

export const groupLabels: Record<TaskGroup, string> = {
  today: '今天', yesterday: '昨天', week: '过去 7 天', earlier: '更早时间',
}

const DAY = 86_400_000

function startOfLocalDay(value: Date): number {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
}

export function taskGroup(createdAt: string, now: Date): TaskGroup {
  const created = new Date(createdAt)
  if (Number.isNaN(created.getTime())) return 'earlier'
  const days = Math.round((startOfLocalDay(now) - startOfLocalDay(created)) / DAY)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return days <= 7 ? 'week' : 'earlier'
}

export function groupTasks<T extends { created_at: string }>(
  tasks: T[], now: Date,
): { group: TaskGroup; label: string; tasks: T[] }[] {
  const buckets = new Map<TaskGroup, T[]>()
  for (const task of tasks) {
    const group = taskGroup(task.created_at, now)
    buckets.set(group, [...(buckets.get(group) ?? []), task])
  }
  return (['today', 'yesterday', 'week', 'earlier'] as const)
    .filter(group => buckets.has(group))
    .map(group => ({ group, label: groupLabels[group], tasks: buckets.get(group)! }))
}

export const uiStorageKey = 'projectpilot.ui.v1'

export interface TaskUiRecord {
  title: string | null
  // Last title derived from the server project_topic; used to tell manual edits apart.
  autoTitle: string | null
  titleEdited: boolean
  revisionId: string | null
  runId: string | null
  stage: number | null
  updatedAt: string
}

export interface UiState {
  version: 1
  tasks: Record<string, TaskUiRecord>
  last: { taskId: string; at: string } | null
}

export const emptyUi = (): UiState => ({ version: 1, tasks: {}, last: null })

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const textOrNull = (value: unknown): string | null => typeof value === 'string' && value ? value : null

function toRecord(value: unknown): TaskUiRecord | null {
  if (!isRecord(value)) return null
  const stage = typeof value.stage === 'number' && [1, 2, 3, 4, 5, 6].includes(value.stage) ? value.stage : null
  return {
    title: textOrNull(value.title),
    autoTitle: textOrNull(value.autoTitle),
    titleEdited: value.titleEdited === true,
    revisionId: textOrNull(value.revisionId),
    runId: textOrNull(value.runId),
    stage,
    updatedAt: typeof value.updatedAt === 'string' ? value.updatedAt : '',
  }
}

export function readUi(storage: Pick<Storage, 'getItem'>): { state: UiState; error: string } {
  let raw: string | null
  try { raw = storage.getItem(uiStorageKey) } catch { return { state: emptyUi(), error: '' } }
  if (!raw) return { state: emptyUi(), error: '' }
  try {
    const parsed = JSON.parse(raw)
    if (!isRecord(parsed) || parsed.version !== 1) throw new Error('version')
    const tasks: Record<string, TaskUiRecord> = {}
    if (isRecord(parsed.tasks)) {
      for (const [id, value] of Object.entries(parsed.tasks)) {
        const record = toRecord(value)
        if (record) tasks[id] = record
      }
    }
    const last = isRecord(parsed.last) && typeof parsed.last.taskId === 'string'
      ? { taskId: parsed.last.taskId, at: typeof parsed.last.at === 'string' ? parsed.last.at : '' }
      : null
    return { state: { version: 1, tasks, last }, error: '' }
  } catch {
    return { state: emptyUi(), error: '本地界面记录无法解析，已按空记录继续；不影响服务端数据。' }
  }
}

export function writeUi(storage: Pick<Storage, 'setItem'>, state: UiState): void {
  storage.setItem(uiStorageKey, JSON.stringify(state))
}

export function patchTask(state: UiState, taskId: string, patch: Partial<Omit<TaskUiRecord, 'updatedAt'>> & { touch?: boolean }): UiState {
  const previous = state.tasks[taskId]
  const { touch, ...rest } = patch
  const next: TaskUiRecord = {
    title: previous?.title ?? null, autoTitle: previous?.autoTitle ?? null,
    titleEdited: previous?.titleEdited ?? false,
    revisionId: previous?.revisionId ?? null, runId: previous?.runId ?? null,
    stage: previous?.stage ?? null, updatedAt: previous?.updatedAt ?? '',
    ...rest,
  }
  return {
    version: 1,
    tasks: { ...state.tasks, [taskId]: next },
    last: touch === false ? state.last : { taskId, at: new Date().toISOString() },
  }
}

export function removeTask(state: UiState, taskId: string): UiState {
  const tasks = { ...state.tasks }
  delete tasks[taskId]
  return { version: 1, tasks, last: state.last?.taskId === taskId ? null : state.last }
}

export const previewStorageKey = 'projectpilot.preview.v1'

export function readPreviewOpen(storage: Pick<Storage, 'getItem'>): boolean {
  try {
    const raw = storage.getItem(previewStorageKey)
    if (!raw) return false
    const value: unknown = JSON.parse(raw)
    // Default hidden: only an explicit, well-formed true reopens it.
    return isRecord(value) && value.version === 1 && value.open === true
  } catch { return false }
}

export function writePreviewOpen(storage: Pick<Storage, 'setItem'>, open: boolean): void {
  storage.setItem(previewStorageKey, JSON.stringify({ version: 1, open }))
}
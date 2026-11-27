import type { Task } from '../api'
import type { TaskUiRecord } from '../history'

export interface TaskSummary {
  id: string
  createdAt: string
  documentId: string | null
  status: string
  statusLabel: string
  title: string
  generatedTitle: string
  renamed: boolean
  inCurrentSession: boolean
}

const labels: Record<string, string> = {
  completed: '已完成', processing: '解析中', failed: '失败', empty: '待输入', created: '待输入', pending: '待处理',
}

export const taskStatusLabel = (status: string): string => labels[status] ?? status

export const generatedTitle = (taskId: string): string => `任务 ${taskId.slice(0, 8)}`

export function titleFor(record: TaskUiRecord | undefined, taskId: string): string {
  return record?.title?.trim() || record?.autoTitle?.trim() || generatedTitle(taskId)
}

// A manual title wins; an untouched title may follow the latest server project_topic.
export function titleEditedFor(record: TaskUiRecord | undefined, taskId: string, value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  return trimmed !== (record?.autoTitle?.trim() || generatedTitle(taskId))
}

export function topicPatch(record: TaskUiRecord | undefined, topic: string): Partial<Pick<TaskUiRecord, 'title' | 'autoTitle' | 'titleEdited'>> | null {
  const value = topic.trim().slice(0, 80)
  if (!value) return null
  if (record?.titleEdited) return record.autoTitle === value ? null : { autoTitle: value }
  if (record?.title === value && record?.autoTitle === value) return null
  return { title: value, autoTitle: value, titleEdited: false }
}

export function toTaskSummary(task: Task, record?: TaskUiRecord, inCurrentSession = true): TaskSummary {
  const base = generatedTitle(task.task_id)
  return {
    id: task.task_id,
    createdAt: task.created_at,
    documentId: task.document_id,
    status: task.status,
    statusLabel: taskStatusLabel(task.status),
    title: titleFor(record, task.task_id),
    generatedTitle: base,
    renamed: !!record?.titleEdited,
    inCurrentSession,
  }
}
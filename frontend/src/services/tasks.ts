import { request } from '../api'
import type { InputStatus, NormalizedDocument, Task } from '../api'

export const listTasks = () => request<{ tasks: Task[] }>('/tasks')

export const readInputStatus = (taskId: string, signal?: AbortSignal) =>
  request<InputStatus>(`/tasks/${encodeURIComponent(taskId)}/input/status`, 'GET', signal)

export const readInput = (taskId: string, signal?: AbortSignal) =>
  request<NormalizedDocument>(`/tasks/${encodeURIComponent(taskId)}/input`, 'GET', signal)
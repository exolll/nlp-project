import { request } from '../api'
import type { DeepReviewOperation, SearchRun } from '../search'

export const readRun = (taskId: string, runId: string, signal?: AbortSignal) =>
  request<SearchRun>(`/tasks/${encodeURIComponent(taskId)}/search-runs/${encodeURIComponent(runId)}`, 'GET', signal)

// 未授权时后端返回 404，调用方据此判定「尚未授权」，不是错误。
export const readDeepReview = (taskId: string, runId: string, signal?: AbortSignal) =>
  request<DeepReviewOperation>(`/tasks/${encodeURIComponent(taskId)}/search-runs/${encodeURIComponent(runId)}/deep-review`, 'GET', signal)
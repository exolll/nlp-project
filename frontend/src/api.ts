export interface ApiError { error_code: string; message: string; retryable: boolean; details: unknown; request_id: string }
export interface Config { max_file_bytes: number; max_pdf_pages: number; max_primary_characters: number; max_supplement_characters: number; storage_backend: string; environment: string }
export interface Task { task_id: string; created_at: string; status: string; document_id: string | null }
export interface InputStatus { task_id: string; document_id: string | null; status: 'empty' | 'processing' | 'completed' | 'failed'; error: ApiError | null }
export interface Segment { segment_id: string; source_type: 'file' | 'user_text'; source_kind: 'assignment_document' | 'user_primary_text' | 'user_supplement'; content: string; original_content: string; source_location: unknown; start_offset: number; end_offset: number; location_detail: Record<string, unknown> }
export interface NormalizedDocument { document_id: string; task_id: string; input_mode: 'pdf' | 'docx' | 'text' | 'pdf_text' | 'docx_text'; original_filename: string | null; mime_type: string; file_size: number; total_pages: number | null; extracted_text: string; supplemental_text: string | null; normalized_text: string; source_segments: Segment[]; content_hash: string; extraction_status: 'completed'; warnings: string[]; created_at: string; character_count: number }
export class Failure extends Error {
  constructor(public status: number, public info: ApiError) { super(info.message) }
}
const base = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')
const tokenKey = 'projectpilot.session'
// 用户模型 Key 仅保存在页面内存；刷新即清空，绝不写入浏览器存储、URL 或日志。
let llmApiKey = ''
const keyListeners = new Set<() => void>()
export function setLlmApiKey(value: string) { llmApiKey = value.trim(); keyListeners.forEach(fn => fn()) }
export function hasLlmApiKey(): boolean { return !!llmApiKey }
export function subscribeLlmKey(fn: () => void): () => void { keyListeners.add(fn); return () => { keyListeners.delete(fn) } }
// 仅四个计费 POST 端点携带 Key；普通 GET、上传、编辑与确认不携带。
const paidEndpoint = /\/(requirement-extractions|search-runs|deep-check|deep-review)$/
export function carriesLlmKey(path: string, method: string): boolean { return !!llmApiKey && method === 'POST' && paidEndpoint.test(path) }
export function session(): { access_token: string; expires_at: string } | null {
  try { return JSON.parse(sessionStorage.getItem(tokenKey) || 'null') } catch { return null }
}
function failure(status: number, body?: Partial<ApiError>): Failure {
  return new Failure(status, { error_code: body?.error_code || 'NETWORK_ERROR', message: status === 401 ? '会话已过期。请显式重新开始会话；现有历史不会自动清除。' : status === 503 ? body?.message || '文件存储或服务暂时不可用，操作未确认成功。请稍后检查任务状态。' : body?.message || (status >= 500 ? '服务暂时不可用，请确认后端已启动，再重新连接。' : status ? `请求失败（${status}）` : '网络连接中断，尚未确认请求结果。'), retryable: body?.retryable ?? status === 0, details: body?.details ?? null, request_id: body?.request_id || '' })
}
export async function request<T>(path: string, method = 'GET', signal?: AbortSignal, payload: unknown = {}, key?: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${base}${path}`, { method, signal, headers: { ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}), ...(key ? { 'Idempotency-Key': key } : {}), ...(carriesLlmKey(path, method) ? { 'X-LLM-API-Key': llmApiKey } : {}), ...(session()?.access_token ? { Authorization: `Bearer ${session()!.access_token}` } : {}) }, ...(method === 'POST' ? { body: JSON.stringify(payload) } : {}) })
  } catch (e) { if (signal?.aborted) throw e; throw failure(0) }
  const body = await response.json().catch(() => null)
  if (!response.ok) throw failure(response.status, body)
  if (body === null) throw failure(502, { message: '服务返回了无效 JSON。' })
  return body as T
}
export async function startSession() {
  const value = await request<{ access_token: string; expires_at: string }>('/sessions', 'POST')
  sessionStorage.setItem(tokenKey, JSON.stringify(value))
}
export function upload(task: string, key: string, file: File | null, text: string, progress: (value: number) => void): Promise<InputStatus> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${base}/tasks/${encodeURIComponent(task)}/input`)
    xhr.setRequestHeader('Authorization', `Bearer ${session()?.access_token || ''}`)
    xhr.setRequestHeader('Idempotency-Key', key)
    xhr.timeout = 120000
    xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)) }
    xhr.onload = () => {
      let body
      try { body = JSON.parse(xhr.responseText) } catch { reject(failure(xhr.status >= 400 ? xhr.status : 502, { message: '服务返回了无效 JSON，请先在历史中检查任务。' })); return }
      if (xhr.status !== 202) reject(failure(xhr.status, body))
      else resolve(body as InputStatus)
    }
    xhr.onerror = xhr.ontimeout = () => reject(failure(0))
    const form = new FormData()
    if (file) form.append('file', file)
    if (text.trim()) form.append('text', text)
    xhr.send(form)
  })
}
export const codePoints = (value: string) => Array.from(value)

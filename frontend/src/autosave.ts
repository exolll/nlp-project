// 真实的保存状态机：状态只由 save() 的实际结果驱动，不使用定时器伪造成功。
// pending 是待写入的新值；unsaved 是最近一次写入失败、保留供手动重试的值。
export type SaveStatus = 'idle' | 'saving' | 'saved' | 'failed'

export interface SaveDetail { message?: string; retryable?: boolean }

export interface AutosaveOptions<T> {
  save: (value: T) => void | Promise<void>
  onChange: (status: SaveStatus, detail: SaveDetail) => void
  delay?: number
  maxRetries?: number
  isRetryable?: (error: unknown) => boolean
}

export interface AutosaveController<T> {
  schedule(value: T): void
  flush(): Promise<boolean>
  retry(): Promise<boolean>
  cancel(): void
  status(): SaveStatus
  hasUnsaved(): boolean
}

export function saveErrorMessage(error: unknown): string {
  if (typeof DOMException !== 'undefined' && error instanceof DOMException
      && (error.name === 'QuotaExceededError' || error.name === 'SecurityError')) {
    return '浏览器本地存储不可用（空间已满或隐私模式），修改仍保留在页面中，请手动重试。'
  }
  return error instanceof Error && error.message ? error.message : '保存失败，请手动重试。'
}

const wait = (ms: number) => new Promise<void>(resolve => { setTimeout(resolve, ms) })

export function createAutosave<T>(options: AutosaveOptions<T>): AutosaveController<T> {
  const delay = options.delay ?? 1000
  const maxRetries = options.maxRetries ?? 0
  const isRetryable = options.isRetryable ?? (() => false)
  let timer: ReturnType<typeof setTimeout> | null = null
  let pending: { value: T } | null = null
  let unsaved: { value: T } | null = null
  let inflight: Promise<boolean> | null = null
  let state: SaveStatus = 'idle'
  let attempts = 0

  const clearTimer = () => { if (timer) { clearTimeout(timer); timer = null } }
  function publish(next: SaveStatus, detail: SaveDetail = {}) { state = next; options.onChange(next, detail) }

  async function write(): Promise<boolean> {
    while (pending) {
      const job = pending
      pending = null
      publish('saving')
      try {
        await options.save(job.value)
      } catch (error) {
        // A newer edit arrived while the failed write was in flight: it supersedes this value.
        if (pending) continue
        if (attempts < maxRetries && isRetryable(error)) {
          attempts += 1
          pending = job
          await wait(delay)
          continue
        }
        attempts = 0
        unsaved = job
        publish('failed', { message: saveErrorMessage(error), retryable: isRetryable(error) })
        return false
      }
      attempts = 0
      unsaved = null
      if (!pending) { publish('saved'); return true }
    }
    return state !== 'failed'
  }

  function kick(): Promise<boolean> {
    clearTimer()
    if (inflight) return inflight
    inflight = (async () => {
      const result = await write()
      inflight = null
      if (pending) void kick()
      return result
    })()
    return inflight
  }

  async function flush(): Promise<boolean> {
    clearTimer()
    if (inflight) await inflight
    if (!pending && unsaved) pending = unsaved
    if (!pending) return state !== 'failed'
    return kick()
  }

  async function retry(): Promise<boolean> {
    if (!pending && unsaved) pending = unsaved
    return flush()
  }

  return {
    schedule(value) {
      pending = { value }
      unsaved = null
      attempts = 0
      clearTimer()
      timer = setTimeout(() => { timer = null; void kick() }, delay)
    },
    flush,
    retry,
    cancel() { clearTimer() },
    status: () => state,
    hasUnsaved: () => !!pending || !!unsaved,
  }
}
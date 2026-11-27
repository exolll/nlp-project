export const draftStorageKey = 'projectpilot.input-draft.v1'

export interface DraftFileHint { name: string; size: number }

export interface InputDraft {
  version: 1
  draftId: string
  text: string
  fileHint: DraftFileHint | null
  updatedAt: string
}

export interface DraftReadResult { draft: InputDraft | null; error: string }

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

export function readDraft(storage: Pick<Storage, 'getItem'>): DraftReadResult {
  let raw: string | null
  try { raw = storage.getItem(draftStorageKey) } catch { return { draft: null, error: '' } }
  if (!raw) return { draft: null, error: '' }
  try {
    const value: unknown = JSON.parse(raw)
    if (!isRecord(value) || value.version !== 1 || typeof value.draftId !== 'string'
        || typeof value.text !== 'string' || typeof value.updatedAt !== 'string') throw new Error('shape')
    let fileHint: DraftFileHint | null = null
    if (value.fileHint !== null) {
      if (!isRecord(value.fileHint) || typeof value.fileHint.name !== 'string'
          || typeof value.fileHint.size !== 'number' || !Number.isFinite(value.fileHint.size)) throw new Error('file')
      fileHint = { name: value.fileHint.name, size: value.fileHint.size }
    }
    if (!value.text && !fileHint) return { draft: null, error: '' }
    return {
      draft: { version: 1, draftId: value.draftId, text: value.text, fileHint, updatedAt: value.updatedAt },
      error: '',
    }
  } catch {
    return { draft: null, error: '本地输入草稿无法解析，已忽略；不影响服务端已保存的任务。' }
  }
}

export function writeDraft(
  storage: Pick<Storage, 'setItem' | 'removeItem'>,
  value: { draftId: string; text: string; fileHint: DraftFileHint | null },
): void {
  if (!value.text && !value.fileHint) { storage.removeItem(draftStorageKey); return }
  const draft: InputDraft = {
    version: 1, draftId: value.draftId, text: value.text, fileHint: value.fileHint,
    updatedAt: new Date().toISOString(),
  }
  storage.setItem(draftStorageKey, JSON.stringify(draft))
}

export function clearDraft(storage: Pick<Storage, 'removeItem'>): void {
  storage.removeItem(draftStorageKey)
}
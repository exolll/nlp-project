import { Failure } from '../api'
import { latestProjectTopic } from '../requirements'
import { validateDeepReview } from '../search'
import { readDocumentRequirements } from '../services/requirements'
import { readDeepReview, readRun } from '../services/search'
import type { RunSignals, StageSignals } from './stages'

export interface StageSignalResult {
  signals: StageSignals
  errors: string[]
  projectTopic: string | null
}

export interface StageSignalInput {
  taskId: string
  documentId: string | null
  inputCompleted: boolean
  runId: string | null
  confirmedRevisionId: string | null
}

const isMissing = (error: unknown) => error instanceof Failure && (error.status === 404 || error.status === 401)

function message(error: unknown): string {
  if (error instanceof Failure) return error.info.message
  return error instanceof Error ? error.message : '阶段状态读取失败'
}

export async function loadStageSignals(input: StageSignalInput, signal?: AbortSignal): Promise<StageSignalResult> {
  const errors: string[] = []
  let extractionCompleted = false
  let confirmedRevisionId = input.confirmedRevisionId
  let projectTopic: string | null = null
  let run: RunSignals | null = null

  if (input.documentId) {
    try {
      const result = await readDocumentRequirements(input.documentId, signal)
      extractionCompleted = result.extraction?.status === 'completed'
      projectTopic = latestProjectTopic(result.revisions, result.latest_revision_id)
      const confirmed = result.revisions
        .filter(revision => revision.status === 'confirmed')
        .sort((a, b) => b.revision_number - a.revision_number)[0]
      confirmedRevisionId = confirmed?.revision_id ?? input.confirmedRevisionId
    } catch (error) {
      if (!signal?.aborted) errors.push(`需求版本读取失败：${message(error)}`)
    }
  }

  if (input.runId) {
    let confirmed = input.confirmedRevisionId
    let evidencePublished = false
    try {
      const result = await readRun(input.taskId, input.runId, signal)
      confirmed = result.confirmed_revision_id
      evidencePublished = result.evidence_status === 'published'
    } catch (error) {
      if (!signal?.aborted) errors.push(`搜索运行读取失败：${message(error)}`)
    }
    if (confirmed) {
      let deepReviewPublished = false
      let rankingAvailable = false
      try {
        const operation = await readDeepReview(input.taskId, input.runId, signal)
        const checked = validateDeepReview(operation, input.taskId, confirmed, input.runId)
        deepReviewPublished = !!checked.report
        rankingAvailable = !!checked.report?.ranking
      } catch (error) {
        if (!isMissing(error) && !signal?.aborted) errors.push(`深度核查读取失败：${message(error)}`)
      }
      run = { confirmedRevisionId: confirmed, evidencePublished, deepReviewPublished, rankingAvailable }
    }
  }

  return {
    signals: { hasTask: true, inputCompleted: input.inputCompleted, extractionCompleted, confirmedRevisionId, run },
    errors,
    projectTopic,
  }
}
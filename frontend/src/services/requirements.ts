import { request } from '../api'
import type { RequirementsResponse } from '../requirements'

export const readDocumentRequirements = (documentId: string, signal?: AbortSignal) =>
  request<RequirementsResponse>(`/documents/${encodeURIComponent(documentId)}/requirements`, 'GET', signal)
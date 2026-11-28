// 后端待实现能力的唯一开关点。契约细节见 docs/ProjectPilot_Frontend_Backend_Integration_Todo.md。
// 后端同学交付后，只需把对应 available 改为 true 并替换 services 实现，页面组件不动。
export type PendingKey =
  | 'taskTitle' | 'taskDelete' | 'runList' | 'sessionPersistence' | 'llmConfig' | 'llmTest' | 'configDetail'

export interface PendingCapability {
  available: boolean
  reason: string
}

export const capabilities: Record<PendingKey, PendingCapability> = {
  taskTitle: { available: false, reason: 'TaskResponse 无 title 字段，且不存在 PATCH /tasks/{id}' },
  taskDelete: { available: false, reason: '路由表中不存在任何 DELETE 方法' },
  runList: { available: false, reason: '无 GET /tasks/{id}/search-runs，运行只能按 run_id 单独寻址' },
  sessionPersistence: { available: false, reason: 'owner = sha256(token)，换 token 即换身份，重开浏览器必然新建会话' },
  llmConfig: { available: false, reason: 'base_url/model 由后端 Literal 固定，无保存/读取接口；API Key 已改为页面内存 + X-LLM-API-Key 请求头（见设置抽屉），服务端不保存' },
  llmTest: { available: false, reason: '无连接测试接口' },
  configDetail: { available: false, reason: 'GET /config 仅返回输入限制，不含模型信息' },
}

export interface TaskTitlePatch { title: string }

export interface LlmSettingsView {
  provider: string
  base_url: string
  model: string
  api_key_configured: boolean
  api_key_masked: string | null
}

export interface LlmSettingsUpdate {
  provider: string
  base_url: string
  model: string
  api_key: string
}

export interface LlmTestResult { ok: boolean; latency_ms: number; model: string }

export interface SearchRunSummary {
  run_id: string
  task_id: string
  confirmed_revision_id: string
  pipeline_status: string
  evidence_status: string | null
  created_at: string
}

export interface SearchRunList { runs: SearchRunSummary[] }
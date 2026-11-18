import type { components } from './api.generated'

export type Snapshot = components['schemas']['Snapshot']
export type Decision = components['schemas']['ApprovalDecision']
export type CreateRequest = components['schemas']['CreateRequest']
export type MessageRequest = components['schemas']['MessageRequest']
export type DecisionRequest = components['schemas']['DecisionRequest']

export class ApiError extends Error {
  constructor(readonly code: string) {
    super(code)
  }
}

const errorText: Record<string, string> = {
  cluster_unavailable: '暂时无法读取服务列表。',
  conversation_active: '已有调查正在进行。',
  conversation_finished: '这次调查已经结束。',
  idempotency_conflict: '请求内容发生变化，请重新提交。',
  invalid_request: '请检查填写的内容。',
  hash_mismatch: '提案已更新，请重新确认。',
  not_pending: '这份提案已有决定。',
  not_waiting: '这份提案当前不可审批。',
  not_found: '此调查或提案已不可用。',
  store_unavailable: '数据库暂时不可用。',
}

export function explainError(error: unknown): string {
  if (error instanceof ApiError) return errorText[error.code] ?? '请求暂时无法处理。'
  return '连接暂时不可用。请重试。'
}

export async function fetchApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  })
  const data = await response.json()
  if (!response.ok) throw new ApiError(data.code)
  return data as T
}

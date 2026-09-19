// 统一 fetch 客户端。
// - 相对路径 /api 请求，由 Vite 开发代理 / 生产反向代理转发到后端。
// - 统一解析 {data, meta} 信封与 {error:{code,message,details,traceId}} 错误模型。
// - 显式区分 502（UPSTREAM_UNAVAILABLE，下游不可用）与 503（SERVICE_DEGRADED，服务降级）：
//   二者都意味着「后端暂时做不到」，但语义不同，页面据此给出不同提示。

import type { Envelope, ErrorBody } from '../types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly traceId?: string
  readonly details?: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    traceId?: string,
    details?: Record<string, unknown>,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.traceId = traceId
    this.details = details
  }
}

const BASE = '' // 相对路径

function authHeaders(): HeadersInit {
  const token = import.meta.env.VITE_API_TOKEN
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function buildQuery(params?: object): string {
  if (!params) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) for (const item of v) sp.append(k, String(item))
    else sp.append(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, init)
  } catch (e) {
    throw new ApiError(0, 'NETWORK_ERROR', '无法连接后端服务', undefined, { cause: String(e) })
  }

  const text = await resp.text()
  let json: unknown = null
  if (text) {
    try {
      json = JSON.parse(text)
    } catch {
      json = null
    }
  }

  if (!resp.ok) {
    const err = (json as { error?: ErrorBody } | null)?.error
    throw new ApiError(
      resp.status,
      err?.code ?? 'INTERNAL_ERROR',
      err?.message ?? `HTTP ${resp.status}`,
      err?.traceId,
      err?.details,
    )
  }
  return json
}

/** GET 信封端点 → 返回完整 Envelope<T>（data + meta.page 供分页）。 */
export async function apiGet<T>(path: string, params?: object): Promise<Envelope<T>> {
  return (await request(`${path}${buildQuery(params)}`, { headers: authHeaders() })) as Envelope<T>
}

/** GET 直返端点（如 /api/health，非信封）→ 返回 T。 */
export async function apiGetDirect<T>(path: string): Promise<T> {
  return (await request(path, { headers: authHeaders() })) as T
}

/** POST 信封端点。`params` 会作为查询串（如批量告警的重复 ids）。 */
export async function apiPost<T>(
  path: string,
  body?: unknown,
  params?: object,
): Promise<Envelope<T>> {
  return (await request(`${path}${buildQuery(params)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  })) as Envelope<T>
}

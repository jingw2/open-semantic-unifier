export interface UserInfo {
  id: string
  email: string
  display_name: string
  role: 'admin' | 'editor' | 'viewer'
  workspace_id: string
  workspace_name?: string
}

export interface ProjectItem {
  id: string
  name: string
  slug: string
  description: string
  created_at?: string
}

export interface DataSourceItem {
  id: string
  name: string
  source_type: string
  table_name: string
  custom_sql: string
  profile: Record<string, any>
  routing: Record<string, any>
  created_at: string
}

export interface JobItem {
  id: string
  stage: string
  status: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED'
  progress: number
  stage_message: string
  result: Record<string, any>
  error_message?: string
  created_at?: string
}

export interface UnificationScheme {
  scheme_key?: string
  revision: number
  entity_type: string
  identity_granularity: string
  field_map: Record<string, string>
  normalizers: string[]
  strong_identifiers: string[]
  conflict_constraints: Array<{ field: string; action: 'veto_auto_merge' }>
  candidate_rules: Array<Record<string, unknown>>
  attribute_policies: Record<string, unknown>
  model_judgment_enabled?: boolean
  model_provider_id?: string | null
  model_failure_policy?: 'REVIEW' | 'FAIL'
  prompt_version?: string
}

export interface UnificationRun {
  run_id: string
  project_id: string
  input_version: string
  scheme_revision: string
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  attempt_count: number
  error_message: string | null
  last_interruption: string | null
  trace_id: string | null
  created_at: string
  updated_at: string
  result: {
    pairs?: Array<Record<string, unknown>>
    entities?: Record<string, Record<string, unknown>>
    [key: string]: unknown
  } | null
}

export interface UnificationSnapshot {
  snapshot_id: string
  version: number
  run_id: string | null
  parent_snapshot_id: string | null
  rollback_of_id: string | null
  actor_id: string
  trace_id: string | null
  created_at: string
  is_current: boolean
}

export async function downloadApi(path: string, filename: string): Promise<void> {
  const headers = new Headers()
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(path, { headers })
  if (response.status === 401) {
    clearAuth()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(data.detail || `下载失败 (${response.status})`)
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

const TOKEN_KEY = 'su_access_token'
const USER_KEY = 'su_user_info'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setAuth(token: string, user: UserInfo) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function getStoredUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export class ApiError extends Error {
  status: number
  detail: any

  constructor(message: string, status: number, detail: any) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export async function apiFetch<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {})
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(path, { ...options, headers })
  if (res.status === 401) {
    clearAuth()
    window.location.reload()
    throw new Error('登录已过期，请重新登录')
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = data.detail
    const message = typeof detail === 'string' ? detail : (detail?.message || `请求失败 (${res.status})`)
    throw new ApiError(message, res.status, detail)
  }
  return data as T
}

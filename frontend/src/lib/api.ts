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
    throw new Error(data.detail || `请求失败 (${res.status})`)
  }
  return data as T
}

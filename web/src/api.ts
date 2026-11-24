// Small typed client for the Context Layer API.
export type Role = 'owner' | 'admin' | 'viewer'
export interface User { id: number; email: string; name: string; role: Role; created: string; last_login: string | null; disabled: boolean }
export interface Agent {
  id: string; name: string; description: string; groups: string[]; capabilities: string[]; prefix: string
  created: string; created_by: string; last_used: string | null; calls: number; revoked: string | null
}
export interface Doc { name: string; size: number; modified: string; groups: string[]; passages: number; status: 'ready' | 'new' | 'changed' }
export interface Job { id: string; kind: string; label: string; status: 'running' | 'done' | 'failed'; started: string; finished: string | null; by: string; error: string | null; log: string[] }
export interface Event { id: number; time: string; actor_type: 'agent' | 'user'; actor: string; action: string; target: string; detail: Record<string, unknown>; ok: boolean; seconds: number | null }
export interface Source { id: string; source: string; pages: number[]; section: string; text: string; score?: number | null }
export interface Answer {
  question: string; route: string; answer: string | null; found: boolean | null; citations: string[]; sources: Source[]
  notes: string[]; timings_s: Record<string, number>; asked?: string
  data?: { request?: unknown; sql?: string; rows?: Record<string, unknown>[]; row_count?: number; assumptions?: string[]; data_warnings?: string[] }
  graph?: { entities: { name: string; type: string }[]; relations: { source: string; type: string; target: string; hop: number }[] }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

let onUnauthorized: () => void = () => {}
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn }

export async function api<T = any>(path: string, opts: { method?: string; json?: unknown; form?: FormData } = {}): Promise<T> {
  const headers: Record<string, string> = { 'X-CL': '1' }
  let body: BodyInit | undefined
  if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json) }
  if (opts.form) body = opts.form
  const method = opts.method ?? (body ? 'POST' : 'GET')
  const r = await fetch(`/api/v1${path}`, { method, headers, body, credentials: 'same-origin' })
  let data: any = null
  try { data = await r.json() } catch { /* empty body */ }
  if (r.status === 401 && !path.startsWith('/login') && !path.startsWith('/setup')) onUnauthorized()
  if (!r.ok) throw new ApiError(r.status, (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || `Request failed (${r.status})`)
  return data as T
}

export const MCP_URL = `${location.origin}/mcp`

export function ago(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`
  return new Date(iso).toLocaleDateString()
}

export const fmtBytes = (n: number) => n < 1024 ? `${n} B` : n < 2 ** 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 2 ** 20).toFixed(1)} MB`
export const can = (u: User | null, ...roles: Role[]) => !!u && roles.includes(u.role)

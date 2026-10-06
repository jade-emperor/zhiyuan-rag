/**
 * 后端地址、令牌、HTTP 薄封装。
 *
 * **这个 app 唯一的环境就是「后端地址」** —— 不假设同源、不假设同机、不假设由后端托管。
 * 默认值取本页 origin(后端托管时天然正确),也可以被用户改掉并记住(桌面壳就靠这个:
 * 壳里只有一份静态产物,连哪台后端由使用者填)。
 *
 * 令牌放 localStorage:**这是 M7.1 的选择,不是终局** —— XSS 能把令牌读走。
 * 更严的做法(内存 + httpOnly cookie 换票)要动后端,记在 docs/frontend.md §5。
 */

const BACKEND_KEY = 'project-rag.backend'
const TOKEN_KEY = 'project-rag.token'
const USER_KEY = 'project-rag.user'

/** 连不上/没配置时的统一出口:错误信息里必须带上「往哪里试过」。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function devDefaultBackend(): string {
  // 开发态(Vite 5173)与后端不同源,所以 dev 允许用 .env.development 指定默认值。
  // 生产构建里 import.meta.env.DEV 是 false,这一支不会被走到 —— 构建产物里没有写死的地址。
  const fromEnv = import.meta.env.VITE_DEFAULT_BACKEND as string | undefined
  return import.meta.env.DEV && fromEnv ? fromEnv : ''
}

export function defaultBackend(): string {
  const origin = typeof window === 'undefined' ? '' : window.location.origin
  // 桌面壳用 file:// 打开时 origin 是字符串 "null":那不是后端地址,不许当默认值用。
  if (!origin || origin === 'null') return devDefaultBackend()
  return origin
}

export function backendBase(): string {
  const stored = localStorage.getItem(BACKEND_KEY)?.trim()
  return stored && stored.length > 0 ? stored : defaultBackend()
}

export function setBackendBase(url: string): void {
  const trimmed = url.trim().replace(/\/+$/, '')
  if (trimmed) localStorage.setItem(BACKEND_KEY, trimmed)
  else localStorage.removeItem(BACKEND_KEY)
}

export function apiUrl(path: string): string {
  const base = backendBase()
  if (!base) throw new ApiError(0, '还没配置后端地址:请在左上角「后端」里填上 RAG 服务的地址')
  return `${base}${path}`
}

/** HTTP(S) -> WS(S):同一台后端、同一个前缀,只有协议不同。 */
export function wsUrl(path: string, token: string | null): string {
  const base = backendBase()
  if (!base) throw new ApiError(0, '还没配置后端地址:请在左上角「后端」里填上 RAG 服务的地址')
  const url = new URL(base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.pathname = path
  url.search = token ? `?token=${encodeURIComponent(token)}` : ''
  return url.toString()
}

export function token(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(value: string | null): void {
  if (value) localStorage.setItem(TOKEN_KEY, value)
  else localStorage.removeItem(TOKEN_KEY)
}

export function storedUsername(): string | null {
  return localStorage.getItem(USER_KEY)
}

export function setStoredUsername(value: string | null): void {
  if (value) localStorage.setItem(USER_KEY, value)
  else localStorage.removeItem(USER_KEY)
}

/** 401 时要做的事(跳登录页)由 app 注册进来 —— 这一层不认识路由。 */
let unauthorizedHandler: (() => void) | null = null

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

export function notifyUnauthorized(): void {
  setToken(null)
  unauthorizedHandler?.()
}

function detailOf(payload: unknown): string | null {
  if (typeof payload !== 'object' || payload === null) return null
  const detail = (payload as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  // FastAPI 的 422 是 [{loc, msg, type}, …]:把字段名与原因拼成人话,
  // 否则界面上只会显示「422」,而参数面板正是最可能填错的地方。
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        const loc = Array.isArray((item as { loc?: unknown[] }).loc)
          ? (item as { loc: unknown[] }).loc.filter((x) => x !== 'body').join('.')
          : ''
        const msg = (item as { msg?: string }).msg ?? ''
        return loc ? `${loc}: ${msg}` : msg
      })
      .filter((text) => text.length > 0)
    return parts.length > 0 ? parts.join(';') : null
  }
  return null
}

async function errorText(response: Response): Promise<string> {
  const raw = await response.text().catch(() => '')
  try {
    const detail = detailOf(JSON.parse(raw))
    if (detail) return detail
  } catch {
    /* 不是 JSON:原样返回截断的正文 */
  }
  const trimmed = raw.trim()
  return trimmed ? trimmed.slice(0, 200) : `HTTP ${response.status}`
}

export interface RequestOptions {
  /** false = 不带令牌(登录、capabilities、health)。 */
  auth?: boolean
  body?: unknown
  method?: string
  /** 原始 body(上传用):设了它就不再序列化 body。 */
  raw?: BodyInit
  headers?: Record<string, string>
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers)
  const auth = options.auth !== false
  if (auth) {
    const current = token()
    if (current) headers.set('Authorization', `Bearer ${current}`)
  }
  let body: BodyInit | undefined
  if (options.raw !== undefined) body = options.raw
  else if (options.body !== undefined) {
    body = JSON.stringify(options.body)
    headers.set('Content-Type', 'application/json')
  }
  let response: Response
  try {
    response = await fetch(apiUrl(path), { method: options.method ?? 'GET', headers, body })
  } catch (error) {
    // fetch 只在网络层失败时抛:把"往哪里试过"说出来,否则用户只看到「Failed to fetch」
    throw new ApiError(0, `连不上后端 ${backendBase()}:${(error as Error).message}`)
  }
  if (response.status === 401 && auth) {
    notifyUnauthorized()
    throw new ApiError(401, '登录已过期,请重新登录')
  }
  if (!response.ok) throw new ApiError(response.status, await errorText(response))
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/**
 * 接口清单 —— 每个函数对应后端一条路由,路径与 `src/project_rag/api/app.py` 一一对应。
 *
 * 路径**全写在这里**,不在组件里拼:业务接口一律在 `/api/v1` 下(后端有命名空间契约),
 * 拼错一个前缀只会在运行时变成 404,而写在一处就能被契约脚本静态检查。
 */

import { request } from './client'
import type {
  Capabilities,
  DeletedOut,
  HistoryOut,
  LoginOut,
  Me,
  QueryRequest,
  QueryResponse,
  SessionsOut,
  StreamFrame,
} from './types'

export const API_PREFIX = '/api/v1'

/** 流式问答那条 WebSocket 的路径(与 HTTP 同前缀,只有协议不同)。 */
export const STREAM_PATH = `${API_PREFIX}/stream`

export function capabilities(): Promise<Capabilities> {
  // 不含任何数据,所以**不带令牌**:没登录也要能拿到上传后缀(登录页的错误提示要用到)
  return request<Capabilities>(`${API_PREFIX}/capabilities`, { auth: false })
}

export function login(username: string, password: string): Promise<LoginOut> {
  return request<LoginOut>(`${API_PREFIX}/auth/login`, {
    method: 'POST',
    auth: false,
    body: { username, password },
  })
}

export function me(): Promise<Me> {
  return request<Me>(`${API_PREFIX}/auth/me`)
}

export function sources(): Promise<{ sources: string[] }> {
  return request<{ sources: string[] }>(`${API_PREFIX}/sources`)
}

export function sessions(limit = 50): Promise<SessionsOut> {
  return request<SessionsOut>(`${API_PREFIX}/sessions?limit=${limit}`)
}

export function createSession(): Promise<{ session_id: string }> {
  return request<{ session_id: string }>(`${API_PREFIX}/create_session`, { method: 'POST' })
}

export function history(sessionId: string): Promise<HistoryOut> {
  return request<HistoryOut>(`${API_PREFIX}/history/${encodeURIComponent(sessionId)}`)
}

export function clearSession(sessionId: string): Promise<DeletedOut> {
  return request<DeletedOut>(`${API_PREFIX}/history/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

/** 非流式问答。**M7.1 的界面走 WebSocket**;这条留给"不要流式"的调用方与排障。 */
export function query(payload: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>(`${API_PREFIX}/query`, { method: 'POST', body: payload })
}

export type { StreamFrame }

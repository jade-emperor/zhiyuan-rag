/**
 * 登录状态。**它是"谁的界面"的唯一来源**:员工与管理员看到的导航不同。
 *
 * 但要说清一件事:前端按角色隐藏入口**不是权限**。真正的权限在后端
 * (`require_admin` / `check_owner`),前端只是不给用户点一个必然 403 的按钮。
 * 这条在 docs/frontend.md §3 表格里写明过 —— 免得有人以为"藏起来 = 安全"。
 */

import { computed, reactive } from 'vue'

import { setStoredUsername, setToken, storedUsername, token } from '../api/client'
import * as api from '../api/endpoints'
import type { Me } from '../api/types'

const state = reactive({
  username: storedUsername() ?? '',
  user: null as Me | null,
  /** 已经问过后端「我是谁」吗 —— 路由守卫靠它决定要不要跳登录页。 */
  ready: false,
  loading: false,
  error: '',
})

export const isAdmin = computed(() => state.user?.is_admin === true)
export const isLoggedIn = computed(() => state.user !== null)

export function authState() {
  return state
}

export async function login(username: string, password: string): Promise<void> {
  state.loading = true
  state.error = ''
  try {
    const result = await api.login(username, password)
    setToken(result.token)
    setStoredUsername(result.username)
    state.username = result.username
    state.user = { username: result.username, role: result.role, is_admin: result.role === 'admin' }
    state.ready = true
  } catch (error) {
    state.error = (error as Error).message
    throw error
  } finally {
    state.loading = false
  }
}

/** 用已存的令牌问一次「我是谁」。令牌失效会清掉并回到未登录态。 */
export async function loadMe(): Promise<void> {
  if (!token()) {
    state.user = null
    state.ready = true
    return
  }
  state.loading = true
  try {
    state.user = await api.me()
    state.username = state.user.username
  } catch (error) {
    // 401 已经由 client 的 notifyUnauthorized 处理(清令牌 + 跳登录);
    // 其它错误(后端没起来)只提示,不要把人踢下线 —— 那是网络问题,不是身份问题。
    state.user = null
    state.error = (error as Error).message
  } finally {
    state.loading = false
    state.ready = true
  }
}

export function clearAuth(): void {
  setToken(null)
  setStoredUsername(null)
  state.user = null
  state.username = ''
}

export function logout(): void {
  clearAuth()
}

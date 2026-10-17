/**
 * 登录状态。**它是"谁的界面"的唯一来源**:员工、运维、管理员看到的导航不同。
 *
 * 但要说清一件事:前端按角色隐藏入口**不是权限**。真正的权限在后端
 * (`require_admin` / `require_reports` / `require_knowledge` / `check_owner`),
 * 前端只是不给用户点一个必然 403 的按钮 ——
 * 免得有人以为"藏起来 = 安全"。
 *
 * **页签按能力渲染,不按角色名**:后端 `/auth/me` 报三个布尔值
 * (`can_view_reports` / `can_use_knowledge` / `can_manage`),它们由 `domain/auth.py`
 * 的三张能力表算出来。前端自己写 `role === 'ops' || role === 'admin'` 就是第二份真源:
 * 后端加第四个角色时,这里会**静默**少一个页签(不报错,只是没人看得到那个页面)。
 */

import { computed, reactive } from 'vue'

import { setStoredUsername, setToken, storedUsername, token } from '../api/client'
import * as api from '../api/endpoints'
import type { Me, Role } from '../api/types'

const state = reactive({
  username: storedUsername() ?? '',
  user: null as Me | null,
  /** 已经问过后端「我是谁」吗 —— 路由守卫靠它决定要不要跳登录页。 */
  ready: false,
  loading: false,
  error: '',
})

/** 管理动作(上传/删除/建号/授权)的入口开关。 */
export const isAdmin = computed(() => state.user?.is_admin === true)
/** 报表(延迟 + 消耗)的入口开关:运维与管理员都看得到。 */
export const canViewReports = computed(() => state.user?.can_view_reports === true)
/** 对话与知识库清单的入口开关:员工与管理员。 */
export const canUseKnowledge = computed(() => state.user?.can_use_knowledge === true)
export const isLoggedIn = computed(() => state.user !== null)

/** 角色徽标上的那几个字。**只此一处**:写两处迟早有一处忘了加新角色。 */
const ROLE_LABELS: Record<Role, string> = {
  employee: '员工',
  ops: '运维',
  admin: '管理员',
}
export const roleLabel = computed(() =>
  state.user === null ? '' : (ROLE_LABELS[state.user.role] ?? state.user.role),
)

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
    // **权限只从 /auth/me 来**:登录响应里只有角色名,而"这个角色能干什么"的真源是
    // 后端 `domain/auth.py` 的三张能力表。在这里按角色名推三个布尔值,就是第二份真源 ——
    // 后端改了语义,前端会继续按老规矩显示页签(而且不报错)。
    await loadMe()
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

export async function logout(): Promise<void> {
  try {
    // 先让服务端撤销这枚令牌,再清本地。失败也继续清 —— 退不出去比"撤销没成功"更糟。
    await api.logout()
  } catch {
    /* 尽力而为:网络不通 / 令牌已过期都不该挡住退出 */
  }
  clearAuth()
}

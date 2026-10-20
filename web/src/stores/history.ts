/**
 * 「会话历史」页的状态：目录 + 选中那段会话的全部轮次。
 *
 * 两件刻意的事：
 *
 * 1. **目录与正文分两次拉**：`GET /sessions` 只给标题/轮数/时间，`GET /history/{id}`
 *    才给问答正文。一次把全部正文搬回来，翻十段会话就是十次大对象传输；
 * 2. **权限在后端**：`scope=all` 只有管理员能要（员工 403），正文能不能读由
 *    `check_owner` 判。前端不拿 `mine` 去决定"能不能打开"—— 它只用来画标签。
 */
import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { HistoryTurn, SessionSummary } from '../api/types'

const state = reactive({
  sessions: [] as SessionSummary[],
  /** `mine` = 只看自己的；`all` = 管理员视角。 */
  scope: 'mine' as 'mine' | 'all',
  query: '',
  sessionsLoading: false,
  sessionsError: '',
  selected: null as string | null,
  rounds: [] as HistoryTurn[],
  detailLoading: false,
  detailError: '',
  /** 正在清空的会话 ID（空串 = 没有在跑的动作）。 */
  busy: '',
})

export function historyState() {
  return state
}

export async function loadSessions(scope = state.scope): Promise<void> {
  state.scope = scope
  state.sessionsLoading = true
  try {
    const result = await api.sessions(100, scope)
    state.sessions = result.sessions
    state.sessionsError = ''
    // 选中的那段可能已经不在这一档里（切了 scope / 被别处删了）：退到第一段。
    const stillThere = result.sessions.some((item) => item.session_id === state.selected)
    if (!stillThere) {
      state.selected = result.sessions[0]?.session_id ?? null
      if (state.selected !== null) await loadDetail(state.selected)
      else state.rounds = []
    }
  } catch (error) {
    state.sessionsError = (error as Error).message
  } finally {
    state.sessionsLoading = false
  }
}

export async function loadDetail(sessionId: string): Promise<void> {
  state.selected = sessionId
  state.detailLoading = true
  state.detailError = ''
  state.rounds = []
  try {
    const result = await api.history(sessionId)
    // 回来的路上用户可能已经点了另一段：只认当前选中那段的结果。
    if (state.selected === sessionId) state.rounds = result.rounds
  } catch (error) {
    if (state.selected === sessionId) state.detailError = (error as Error).message
  } finally {
    if (state.selected === sessionId) state.detailLoading = false
  }
}

export async function setScope(scope: 'mine' | 'all'): Promise<void> {
  if (scope === state.scope) return
  state.selected = null
  state.rounds = []
  await loadSessions(scope)
}

export async function clearSession(sessionId: string): Promise<boolean> {
  state.busy = sessionId
  try {
    await api.clearSession(sessionId)
    state.sessions = state.sessions.filter((item) => item.session_id !== sessionId)
    if (state.selected === sessionId) {
      state.selected = null
      state.rounds = []
      const next = state.sessions[0]
      if (next !== undefined) await loadDetail(next.session_id)
    }
    return true
  } catch (error) {
    state.detailError = (error as Error).message
    return false
  } finally {
    state.busy = ''
  }
}

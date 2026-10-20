/**
 * 反馈看板的状态。
 *
 * 只有两个旋钮（窗口天数 / 是否只看没帮助），它们一起决定后端那次查询：
 * 前端不拿全量回来自己筛 —— 窗口是**服务端算的**，两边各算一份迟早对不上。
 */
import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { FeedbackListOut } from '../api/types'

type RatingFilter = '' | 'up' | 'down'

const state = reactive({
  summary: { up: 0, down: 0, total: 0, negative_rate: null as number | null },
  entries: [] as FeedbackListOut['entries'],
  days: 30,
  rating: '' as RatingFilter,
  loading: false,
  error: '',
  loaded: false,
})

export function feedbackState() {
  return state
}

export async function loadFeedback(days = state.days, rating = state.rating): Promise<void> {
  state.days = days
  state.rating = rating
  state.loading = true
  try {
    const result = await api.feedbackReport(100, rating, days)
    state.summary = result.summary
    state.entries = result.entries
    state.error = ''
    state.loaded = true
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.loading = false
  }
}

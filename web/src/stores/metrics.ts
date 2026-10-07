/**
 * 可观测看板的取数(M7.3)。
 *
 * 三条口径,与后端 `application/metrics.py` 的 docstring 一字对应:
 *
 * 1. **`null` 不是 0**:分位数样本不够时后端给 `null`(p95 要 n≥20、p99 要 n≥100),
 *    界面必须显示 `-` 并解释门槛 —— 折算成 0 会让人以为"很快",而那是最容易骗到自己的读法;
 * 2. **分段只算热行,最慢榜含冷启动**:看板上要**分开说**,否则"某段 p95 很高"与
 *    "用户真的等了很久"会被混成一件事;
 * 3. **比率要带分子分母**:只给一个百分比,读不出样本量(3/4 与 300/400 都是 75%)。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { MetricsOut } from '../api/types'
import { capabilitiesState } from './capabilities'

const state = reactive({
  window: '',
  top: 10,
  data: null as MetricsOut | null,
  loading: false,
  error: '',
  /** 渲染时刻(本机时间):报表是"取数那一刻"的快照,得让用户知道看的是什么时候的数。 */
  fetchedAt: '',
})

export function metricsState() {
  return state
}

/** 窗口与 top 的默认值来自 capabilities(前端不写死一份白名单)。 */
export function adoptMetricsCapabilities(): void {
  const caps = capabilitiesState().data
  if (caps === null) return
  if (state.window === '') state.window = caps.metrics.default_window
  state.top = Math.min(state.top, caps.metrics.top_max)
}

export async function loadMetrics(): Promise<void> {
  adoptMetricsCapabilities()
  if (state.window === '') {
    state.error = '还没读到 capabilities,不知道该请求哪个窗口'
    return
  }
  state.loading = true
  try {
    state.data = await api.metrics(state.window, state.top)
    state.error = ''
    state.fetchedAt = new Date().toLocaleTimeString()
  } catch (error) {
    state.error = (error as Error).message
    state.data = null
  } finally {
    state.loading = false
  }
}

export async function setWindow(value: string): Promise<void> {
  state.window = value
  await loadMetrics()
}

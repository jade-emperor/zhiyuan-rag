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
 *
 * 还有一条**取数上的**规矩:窗口名来自 capabilities,所以"没读到能力"时这一页取不了数 ——
 * 那时**必须再拉一次能力**(见 `loadMetrics`),不能只报一句「还没读到 capabilities」就停:
 * 后端重启/网络抖一下之后,用户点「刷新」得能自己恢复,否则这一页会一直停在死局里。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { CostOut, MetricsOut } from '../api/types'
import { capabilitiesState, loadCapabilities } from './capabilities'

const state = reactive({
  window: '',
  top: 10,
  data: null as MetricsOut | null,
  /**
   * 消耗那一半(P0-7)。**与延迟分开两个字段、一起取**:两半读的是同一批账本行,
   * 合成一个对象会让"这一半拿到了、那一半没拿到"这种事看不出来。
   */
  cost: null as CostOut | null,
  costError: '',
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
    // 「刷新」必须是能自救的:缺窗口名只有一种原因 —— 能力还没读到(守卫那条枪打空了,
    // 或后端当时正在重启)。不在这里补拉的话,后端回来了这一页也永远停在红字上。
    await loadCapabilities()
    adoptMetricsCapabilities()
  }
  if (state.window === '') {
    // 补拉之后还是没有:这时真正的原因在能力那一侧(连不上后端…),照实转述它,
    // 别用一句"还没读到 capabilities"把"连不上"盖掉 —— 两件事的话术不一样。
    state.error = capabilitiesState().error || '还没读到 capabilities,不知道该请求哪个窗口'
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
  }
  // 消耗单独 try:延迟报表拿到了就该显示,不该因为消耗那一半失败而一起清空。
  // 但**错误要各自留着** —— 一句"报表加载失败"盖住两个不同的原因,排障时最难查。
  try {
    state.cost = await api.cost(state.window)
    state.costError = ''
  } catch (error) {
    state.costError = (error as Error).message
    state.cost = null
  }
  state.loading = false
}

export async function setWindow(value: string): Promise<void> {
  state.window = value
  await loadMetrics()
}

/**
 * 检索参数面板的状态。
 *
 * 三条口径:
 *
 * 1. **默认值来自后端**(`capabilities.retrieval.defaults`),前端不写死 ——
 *    写死的话,改了 yaml 之后界面上的"默认"就是假的;
 * 2. 用户改过的值记在 localStorage,下次打开还在;**但"恢复默认"回到的是后端那份默认值**
 *    (不是某次写死在前端的快照);
 * 3. `mode` 与两个权重**不会同时发给后端**(后端会 422):模式已经决定了跑哪几路。
 *    见 `toQueryRequest`。
 */

import { reactive } from 'vue'

import type { FusionMode, RetrievalDefaults, RetrievalLimits, RetrievalMode } from '../api/types'

const STORAGE_KEY = 'project-rag.retrieval'

export interface RetrievalParams {
  mode: RetrievalMode
  fusion: FusionMode
  bm25_weight: number
  vector_weight: number
  rerank: boolean
  k: number
  max_context: number
  rrf_k: number
  /** 知识库(来源)过滤。**单选**。 */
  source_filter: string | null
}

/** 后端 capabilities 还没到之前先用这一份(与 config.yaml 当前值一致);到了就以后端为准。 */
const BOOTSTRAP: RetrievalParams = {
  mode: 'hybrid',
  fusion: 'rrf',
  bm25_weight: 0.5,
  vector_weight: 0.5,
  rerank: true,
  k: 5,
  max_context: 2,
  rrf_k: 60,
  source_filter: null,
}

function load(): Partial<RetrievalParams> | null {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as Partial<RetrievalParams>
  } catch {
    localStorage.removeItem(STORAGE_KEY)
    return null
  }
}

const stored = load()
const state = reactive<RetrievalParams>({ ...BOOTSTRAP, ...(stored ?? {}) })

/** 用户是否动过参数 —— 动过就不再被 capabilities 的默认值覆盖(否则刷新页面会把他的选择吃掉)。 */
let touched = stored !== null

export function paramsState(): RetrievalParams {
  return state
}

const limitsState = reactive<RetrievalLimits>({
  k_max: 50,
  max_context_max: 20,
  rrf_k_max: 1000,
})

export function limits(): RetrievalLimits {
  return limitsState
}

/** capabilities 到位时调用:记下上限;用户没动过就采用后端的默认值。 */
export function adoptCapabilities(defaults: RetrievalDefaults, capsLimits: RetrievalLimits): void {
  Object.assign(limitsState, capsLimits)
  if (touched) return
  adoptFrom(defaults, capsLimits)
}

export function restoreDefaults(): void {
  touched = false
  localStorage.removeItem(STORAGE_KEY)
  // 真正的"默认"在后端 capabilities 里(不在前端写死一份快照):
  // provider 还没注册(接口没回来)时退回 BOOTSTRAP,下一次 adoptCapabilities 会纠正。
  const defaults = defaultsProvider?.()
  if (defaults) {
    adoptFrom(defaults, limitsState)
    return
  }
  Object.assign(state, BOOTSTRAP)
}

let defaultsProvider: (() => RetrievalDefaults) | null = null

/** 由 `useCapabilities` 注册"去哪里取默认值" —— 参数面板因此不必再认识接口层。 */
export function registerDefaults(provider: () => RetrievalDefaults): void {
  defaultsProvider = provider
}

function adoptFrom(defaults: RetrievalDefaults, capsLimits: RetrievalLimits): void {
  state.mode = defaults.mode
  state.fusion = defaults.fusion
  state.bm25_weight = defaults.bm25_weight
  state.vector_weight = defaults.vector_weight
  state.rerank = defaults.rerank
  state.k = defaults.k
  state.max_context = defaults.max_context ?? capsLimits.max_context_max
  state.rrf_k = defaults.rrf_k
}

export function persist(): void {
  touched = true
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
}

/** 两路权重之和必须 > 0:全 0 = 一次什么都不检索的请求(后端也会 422)。 */
export function weightsOk(): boolean {
  if (state.mode !== 'hybrid') return true
  return state.bm25_weight + state.vector_weight > 0
}

export interface BuiltQuery {
  query: string
  session_id?: string
  source_filter: string | null
  k: number
  max_context: number
  rerank: boolean
  mode?: RetrievalMode
  fusion?: FusionMode
  bm25_weight?: number
  vector_weight?: number
  rrf_k?: number
}

/**
 * 把面板状态拼成一次提问。
 *
 * **这里是与后端那条校验规则对齐的地方**:`mode` 与权重不能同时出现在一个请求里。
 * 所以:选了单路(向量/关键字)就只发 `mode`;混合检索才发权重(单路时权重没有意义,
 * 界面上也藏起来了 —— 让用户调一个不生效的滑条比不给他调更糟)。
 */
export function toQueryRequest(query: string, sessionId: string | null): BuiltQuery {
  const payload: BuiltQuery = {
    query,
    source_filter: state.source_filter,
    k: state.k,
    max_context: state.max_context,
    rerank: state.rerank,
  }
  if (sessionId) payload.session_id = sessionId
  if (state.mode === 'hybrid') {
    payload.fusion = state.fusion
    payload.bm25_weight = round2(state.bm25_weight)
    payload.vector_weight = round2(state.vector_weight)
    payload.rrf_k = state.rrf_k
  } else {
    payload.mode = state.mode
  }
  return payload
}

function round2(value: number): number {
  return Math.round(value * 100) / 100
}

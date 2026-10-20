/**
 * 知识库来源清单。
 *
 * 侧栏、输入区、参数面板都要这份数据 —— 各自 `onMounted` 拉一次会打三枪，
 * 而且三处可能在不同时刻失败，界面会出现「这里能选、那里说没有」。这里只拉一次，
 * 失败原因也只有一份，刷新按钮统一。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'

const state = reactive({
  items: [] as string[],
  loading: false,
  error: '',
  loaded: false,
})

export function sourcesState() {
  return state
}

/** 输入:force（是否强制重拉）。行为:读 `/sources`，失败把原因留在 error。输出:列表。 */
export async function loadSources(force = false): Promise<string[]> {
  if (state.loaded && !force) return state.items
  state.loading = true
  try {
    const result = await api.sources()
    state.items = result.sources
    state.error = ''
    state.loaded = true
    return state.items
  } catch (error) {
    state.error = (error as Error).message
    return state.items
  } finally {
    state.loading = false
  }
}

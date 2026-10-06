/**
 * 后端能力(上传后缀 / 检索选项 / 上限 / 模型)。
 *
 * **前端一处都不写死这些**:上传框的 accept、参数面板的下拉项与滑条上限、
 * 模型名全从这里来。这个接口不需要登录,所以登录页也能拿到它 —— 后端地址填错时,
 * 第一个报错就发生在这一步,而且报的是"连不上",不是"密码不对"。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { Capabilities } from '../api/types'
import { adoptCapabilities, registerDefaults } from './params'

const state = reactive({
  data: null as Capabilities | null,
  loading: false,
  error: '',
})

export function capabilitiesState() {
  return state
}

export async function loadCapabilities(): Promise<Capabilities | null> {
  state.loading = true
  try {
    const data = await api.capabilities()
    state.data = data
    state.error = ''
    adoptCapabilities(data.retrieval.defaults, data.retrieval.limits)
    registerDefaults(() => data.retrieval.defaults)
    return data
  } catch (error) {
    state.error = (error as Error).message
    return null
  } finally {
    state.loading = false
  }
}

export function modelOptions(): string[] {
  return (state.data?.models ?? []).map((model) => model.id)
}

export function defaultModel(): string {
  const models = state.data?.models ?? []
  return (models.find((model) => model.default) ?? models[0])?.id ?? ''
}

/**
 * FAQ 管理台的状态。
 *
 * 两个刻意的选择：
 *
 * 1. **增改删之后按当前搜索词重拉**：服务端的清单是"过滤后的结果 + 过滤前的总数"，
 *    本地拼一条会让两个数对不上（搜"String"时新建一条别的问法，它不该出现在眼前，
 *    但总数该 +1）；
 * 2. **错误只留一处**（`state.error`）：重名 / 超长 / 网络断各是一句话，由后端或
 *    客户端给出来，页面照原样显示，不在这里改写。
 */
import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { FaqEntry } from '../api/types'

const state = reactive({
  entries: [] as FaqEntry[],
  /** 过滤前的总数（界面上的"共 N 条"用它，不是 `entries.length`）。 */
  total: 0,
  query: '',
  loading: false,
  error: '',
  /** 正在提交的动作（`create` / `update:<id>` / `delete:<id>`），用来置灰按钮。 */
  busy: '' as string,
  loaded: false,
})

export function faqState() {
  return state
}

export async function refreshFaq(query = state.query): Promise<void> {
  state.query = query
  state.loading = true
  try {
    const result = await api.faqEntries(query)
    state.entries = result.entries
    state.total = result.total
    state.error = ''
    state.loaded = true
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.loading = false
  }
}

async function run(action: string, work: () => Promise<void>): Promise<boolean> {
  state.busy = action
  try {
    await work()
    state.error = ''
    return true
  } catch (error) {
    state.error = (error as Error).message
    return false
  } finally {
    state.busy = ''
  }
}

export function createFaq(question: string, answer: string): Promise<boolean> {
  return run('create', async () => {
    await api.createFaq(question, answer)
    await refreshFaq()
  })
}

export function updateFaq(
  entryId: number,
  patch: { question?: string; answer?: string },
): Promise<boolean> {
  return run(`update:${entryId}`, async () => {
    await api.updateFaq(entryId, patch)
    await refreshFaq()
  })
}

export function deleteFaq(entryId: number): Promise<boolean> {
  return run(`delete:${entryId}`, async () => {
    await api.deleteFaq(entryId)
    await refreshFaq()
  })
}

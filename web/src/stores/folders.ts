/**
 * 知识库一级页面:目录清单。
 *
 * 三件刻意的事:
 *
 * 1. **读数来自后端的一次对账**(`GET /api/v1/folders`):份数 / 库内块数 / 需要处理的份数
 *    都是后端把登记表、磁盘、向量库对完才给的 —— 前端不拿 `documents.length` 自己数,
 *    那会漏掉语料目录里还没入库的文件;
 * 2. **未分类是一格,不是错误**:磁盘上有、登记表没有的文件只能是它(没有行可写)。
 *    所以它恒定出现在列表末尾,而且不许画成告警;
 * 3. **删除/改名之后立刻重拉**:目录的计数是从文档算出来的,本地改一个名字不会让计数变;
 *    让后端重算一遍,两个页面才不会各说一套。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { FolderRecord, FolderSummary } from '../api/types'

const EMPTY_SUMMARY: FolderSummary = { folders: 0, used: 0, unfiled: 0 }

const state = reactive({
  folders: [] as FolderRecord[],
  unfiled: null as FolderRecord | null,
  summary: { ...EMPTY_SUMMARY },
  /** 上一次成功刷新的时刻("HH:MM:SS")—— 空串 = 还没刷过。 */
  refreshedAt: '',
  loading: false,
  error: '',
  /** 正在提交的目录操作(空串 = 没有在跑的动作)。用它把按钮置灰,而不是靠一个全局忙碌态。 */
  busy: '' as string,
})

export function foldersState() {
  return state
}

export async function refreshFolders(): Promise<void> {
  state.loading = true
  try {
    const result = await api.folders()
    state.folders = result.folders
    state.unfiled = result.unfiled
    state.summary = result.summary
    state.refreshedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    state.error = ''
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.loading = false
  }
}

async function run(action: string, work: () => Promise<void>): Promise<void> {
  state.busy = action
  try {
    await work()
    state.error = ''
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.busy = ''
  }
}

/** 新建目录。失败时错误留在 `state.error` 里(名字不合法 / 重名是两种不同的下一步)。 */
export async function createFolder(name: string, description: string): Promise<FolderRecord | null> {
  let created: FolderRecord | null = null
  await run('create', async () => {
    created = await api.createFolder(name, description)
    await refreshFolders()
  })
  return created
}

export async function renameFolder(
  folderId: string,
  name: string,
  description: string,
): Promise<void> {
  await run(`rename:${folderId}`, async () => {
    await api.updateFolder(folderId, { name, description })
    await refreshFolders()
  })
}

/** 删目录。后端只让删**空**目录 —— 里面还有文档时回 409,话术里带份数。 */
export async function deleteFolder(folderId: string): Promise<void> {
  await run(`delete:${folderId}`, async () => {
    await api.deleteFolder(folderId)
    await refreshFolders()
  })
}

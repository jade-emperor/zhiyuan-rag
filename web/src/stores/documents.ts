/**
 * 知识库二级面页的数据:一个目录里的清单、上传、删除、预览、挪目录。
 *
 * **`scope` 是这一页的目录**(空串 = 全部清单)。二级页面把它设成当前目录,于是
 * 轮询、删除后的重拉、上传的落点都自动是那一格 —— 三处各传一个目录参数的话,
 * 迟早有一处漏掉,而症状是"删完那份还在"(它重拉的是全部清单)。
 *
 * 四件刻意的事:
 *
 * 1. **清单是"三处对账"的结果,不是登记表的一遍朗读**:后端每次都拿登记表、磁盘、
 *    向量库对一遍,给每行一个 `state`(`ok` / `changed` / `not_indexed` / `file_missing` /
 *    `unregistered`)。前端**只显示 `state`**,不自己由 `status` 推"已入库" ——
 *    那正是"集合清空了卡片还写 677 块"的成因;
 * 2. **拿到 202 不算完**:上传接口是异步入库,状态要从清单里看。所以只要还有
 *    `pending` / `processing` 的文档就**自动轮询**(3 秒一次),全部落定就停 ——
 *    一直轮询会白烧请求,不轮询则用户永远看到"处理中";
 * 3. **删除后如实转述后端的结果**(删了几块 / 文件在不在 / 语料源材料留没留 / 登记行在不在):
 *    删除是幂等的,重复点不该报错,而"其实什么都没删"这件事必须让用户看见;
 * 4. **预览按父块折叠**(后端做的):界面上同时显示"入库 N 块(子块)"与"预览 M 段(父块)",
 *    两个数不一样是正常的 —— 只显示一个数会让人以为丢数据。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type {
  DeletedDocument,
  DocumentPreview,
  DocumentRecord,
  DocumentSummary,
  FolderRecord,
} from '../api/types'

export interface UploadTask {
  name: string
  size: number
  state: 'uploading' | 'submitted' | 'failed'
  detail: string
}

/** 空汇总:还没拉到清单时用它,而不是编一堆 0 当"一切正常"。 */
const EMPTY_SUMMARY: DocumentSummary = {
  total: 0,
  ok: 0,
  changed: 0,
  not_indexed: 0,
  file_missing: 0,
  unregistered: 0,
  in_flight: 0,
}

const state = reactive({
  /** 这一页的目录:空串 = 全部清单;`unfiled` = 未分类。 */
  scope: '' as string,
  /** `scope` 那一格的目录信息(名字/说明/计数),来自 `GET /folders/{id}`;全部清单时是 null。 */
  folder: null as FolderRecord | null,
  docs: [] as DocumentRecord[],
  summary: { ...EMPTY_SUMMARY },
  uploadsDir: '',
  corpusDir: '',
  /** 上一次成功刷新的时刻("HH:MM:SS")—— 空串 = 还没刷过。页面是不是过期了,用户要看得出来。 */
  refreshedAt: '',
  loading: false,
  error: '',
  uploads: [] as UploadTask[],
  preview: null as DocumentPreview | null,
  previewFor: '' as string,
  previewLoading: false,
  previewError: '',
  deleting: '' as string,
  lastDeleted: null as DeletedDocument | null,
  /** 批量删除的回执(逐份的结果会被合成一行"删了几份、清了几块")。 */
  lastBatch: null as { documents: number; chunks: number } | null,
  polling: false,
})

const POLL_MS = 3000
let timer: number | null = null

export function documentsState() {
  return state
}

/** 还有文档在入库吗 —— 有就该继续轮询。 */
function hasActive(): boolean {
  return state.docs.some((doc) => doc.status === 'pending' || doc.status === 'processing')
}

function syncPolling(): void {
  const wanted = hasActive()
  if (wanted && timer === null) {
    timer = window.setInterval(() => {
      void refreshDocuments()
    }, POLL_MS)
    state.polling = true
  } else if (!wanted && timer !== null) {
    window.clearInterval(timer)
    timer = null
    state.polling = false
  }
}

export function stopPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
  state.polling = false
}

/**
 * 切到某一格(空串 = 全部清单)。**换目录必须先清掉旧的清单**:不清的话,新目录的请求
 * 回来之前界面上是上一个目录的文档 —— 而"点进去看到的是别人的文件"比空屏糟得多。
 */
export function setScope(folderId: string): void {
  if (state.scope === folderId) return
  state.scope = folderId
  state.docs = []
  state.folder = null
  state.lastDeleted = null
  state.lastBatch = null
  state.error = ''
}

export async function refreshDocuments(): Promise<void> {
  state.loading = true
  try {
    if (state.scope === '') {
      const result = await api.documents()
      state.docs = result.documents
      state.summary = result.summary
      state.uploadsDir = result.uploads_dir
      state.corpusDir = result.corpus_dir
    } else {
      const detail = await api.folderDetail(state.scope)
      state.docs = detail.documents
      state.summary = detail.summary
      state.folder = detail.folder
    }
    state.refreshedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    state.error = ''
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.loading = false
    syncPolling()
  }
}

/**
 * 上传一批文件(**逐个串行**)。为什么串行:后端的入库任务是进程内串行的
 * (嵌入是 CPU 密集的,并发跑只会两边都慢十倍),一次提交一堆只会让队列更长;
 * 而且串行时每个文件的状态是一一对应的,用户看得懂。
 */
export async function uploadFiles(files: File[], folder = ''): Promise<void> {
  for (const file of files) {
    const task: UploadTask = {
      name: file.name,
      size: file.size,
      state: 'uploading',
      detail: '上传中…',
    }
    state.uploads.unshift(task)
    try {
      await api.uploadDocument(file, folder)
      task.state = 'submitted'
      task.detail = '已提交,后台入库中(状态见下方列表)'
    } catch (error) {
      task.state = 'failed'
      task.detail = (error as Error).message
    }
  }
  await refreshDocuments()
}

export async function removeDocument(documentId: string): Promise<void> {
  state.deleting = documentId
  try {
    const outcome = await api.deleteDocument(documentId)
    state.lastDeleted = outcome
    state.lastBatch = null
    state.error = ''
    if (state.previewFor === documentId) closePreview()
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.deleting = ''
    await refreshDocuments()
  }
}

/**
 * 批量删除。**逐份串行**发:后端的删除是"标记 → 删向量 → 删文件 → 删行"四步,
 * 并发跑只会让每一步都更慢,而且失败时说不清是哪一份。中途失败的份数如实报出来 ——
 * 静默跳过会让用户以为删干净了。
 */
export async function removeDocuments(documentIds: string[]): Promise<void> {
  if (documentIds.length === 0) return
  let done = 0
  let chunks = 0
  const failed: string[] = []
  for (const documentId of documentIds) {
    state.deleting = documentId
    try {
      const outcome = await api.deleteDocument(documentId)
      done += 1
      chunks += outcome.deleted_chunks
      if (state.previewFor === documentId) closePreview()
    } catch (error) {
      failed.push(`${documentId}:${(error as Error).message}`)
    }
  }
  state.deleting = ''
  state.lastDeleted = null
  state.lastBatch = { documents: done, chunks }
  state.error = failed.length > 0 ? `有 ${failed.length} 份没删成 —— ${failed[0]}` : ''
  await refreshDocuments()
}

/** 把一份文档挪到另一个目录(空串 = 未分类)。只动归属,不重切、不重灌。 */
export async function moveDocument(documentId: string, folderId: string): Promise<void> {
  state.deleting = documentId
  try {
    await api.moveDocument(documentId, folderId)
    state.error = ''
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.deleting = ''
    await refreshDocuments()
  }
}

export async function openPreview(documentId: string): Promise<void> {
  state.previewFor = documentId
  state.preview = null
  state.previewError = ''
  state.previewLoading = true
  try {
    state.preview = await api.documentPreview(documentId)
  } catch (error) {
    state.previewError = (error as Error).message
  } finally {
    state.previewLoading = false
  }
}

export function closePreview(): void {
  state.previewFor = ''
  state.preview = null
  state.previewError = ''
}

export function clearUploads(): void {
  state.uploads = []
}

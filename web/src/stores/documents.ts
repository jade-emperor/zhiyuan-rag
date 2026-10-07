/**
 * 知识库管理(M7.2)：清单、上传、删除、预览。
 *
 * 三件刻意的事:
 *
 * 1. **拿到 202 不算完**:上传接口是异步入库,状态要从清单里看。所以只要还有
 *    `pending` / `processing` 的文档就**自动轮询**(3 秒一次),全部落定就停 ——
 *    一直轮询会白烧请求,不轮询则用户永远看到"处理中";
 * 2. **删除后如实转述后端的结果**(删了几块 / 文件在不在 / 登记行在不在):
 *    删除是幂等的,重复点不该报错,而"其实什么都没删"这件事必须让用户看见;
 * 3. **预览按父块折叠**(后端做的):界面上同时显示"入库 N 块(子块)"与"预览 M 段(父块)",
 *    两个数不一样是正常的 —— 只显示一个数会让人以为丢数据。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { DeletedDocument, DocumentPreview, DocumentRecord } from '../api/types'

export interface UploadTask {
  name: string
  size: number
  state: 'uploading' | 'submitted' | 'failed'
  detail: string
}

const state = reactive({
  docs: [] as DocumentRecord[],
  uploadsDir: '',
  loading: false,
  error: '',
  uploads: [] as UploadTask[],
  preview: null as DocumentPreview | null,
  previewFor: '' as string,
  previewLoading: false,
  previewError: '',
  deleting: '' as string,
  lastDeleted: null as DeletedDocument | null,
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

export async function refreshDocuments(): Promise<void> {
  state.loading = true
  try {
    const result = await api.documents()
    state.docs = result.documents
    state.uploadsDir = result.uploads_dir
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
export async function uploadFiles(files: File[]): Promise<void> {
  for (const file of files) {
    const task: UploadTask = {
      name: file.name,
      size: file.size,
      state: 'uploading',
      detail: '上传中…',
    }
    state.uploads.unshift(task)
    try {
      await api.uploadDocument(file)
      task.state = 'submitted'
      task.detail = '已提交,后台入库中(状态见下方卡片)'
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
    state.error = ''
    if (state.previewFor === documentId) closePreview()
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

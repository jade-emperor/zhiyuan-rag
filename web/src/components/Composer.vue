<script setup lang="ts">
/**
 * 输入区：自适应高度的组合输入框。
 *
 * 规则：
 * - Enter 发送、Shift+Enter 换行；**中文输入法组词时的 Enter 不发送**（`isComposing`）；
 * - 生成中发送按钮变成「停止」：断开这条 WS，后端会把这一轮记成 aborted（报表里看得见）；
 * - 附件走的是**知识库上传接口**（`POST /documents`）：后端没有聊天附件字段。
 *   所以附件一旦上传就是"入库中的资料"，本次提问不一定立刻能检索到 —— 文案如实说明；
 * - 上传是管理员动作（后端 `require_admin`）。员工看不到回形针，也不接受拖拽。
 */
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'

import * as api from '../api/endpoints'
import { isAdmin } from '../stores/auth'
import { capabilitiesState } from '../stores/capabilities'
import { chatState, send, stop, type ChatAttachment } from '../stores/chat'
import { paramsState, persist, weightsOk } from '../stores/params'
import { loadSources, sourcesState } from '../stores/sources'
import { notify, uiState } from '../stores/ui'
import { humanBytes } from '../utils/format'
import Icon from './Icon.vue'

interface PendingAttachment extends ChatAttachment {
  uploading: boolean
}

const chat = chatState()
const params = paramsState()
const caps = capabilitiesState()
const sources = sourcesState()
const ui = uiState()

const draft = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const pending = ref<PendingAttachment[]>([])
const dragging = ref(false)
const localError = ref('')
let attachCounter = 0

const uploading = computed(() => pending.value.some((item) => item.uploading))

const weightsValid = computed(() => weightsOk())

const canSend = computed(
  () =>
    draft.value.trim().length > 0 &&
    !chat.generating &&
    !uploading.value &&
    weightsValid.value,
)

const readyAttachments = computed(() =>
  pending.value.filter((item) => !item.uploading && item.status === 'ready'),
)

const accept = computed(() => (caps.data?.suffixes ?? []).join(','))

const scopeLabel = computed(() => params.source_filter ?? '全部知识库')

function autoGrow(): void {
  const element = textarea.value
  if (element === null) return
  element.style.height = 'auto'
  element.style.height = `${Math.min(element.scrollHeight, 200)}px`
}

watch(draft, () => {
  void nextTick(autoGrow)
})

function focus(): void {
  textarea.value?.focus()
}

function setDraft(text: string): void {
  draft.value = text
  void nextTick(() => {
    focus()
    autoGrow()
  })
}

defineExpose({ focus, setDraft })

async function submit(): Promise<void> {
  if (!canSend.value) return
  if (uploading.value) {
    localError.value = '附件上传中'
    return
  }
  const text = draft.value
  const attachments: ChatAttachment[] = readyAttachments.value.map((item) => ({
    id: item.id,
    name: item.name,
    size: item.size,
    previewUrl: item.previewUrl,
    status: item.status,
    detail: item.detail,
  }))
  const failed = pending.value.filter((item) => item.status === 'failed')
  for (const item of failed) {
    if (item.previewUrl !== null) URL.revokeObjectURL(item.previewUrl)
  }
  draft.value = ''
  pending.value = []
  localError.value = ''
  await nextTick(autoGrow)
  if (failed.length > 0) {
    notify(`${failed.length} 个附件失败`, 'error')
  }
  await send(text, attachments)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  void submit()
}

function pickFiles(): void {
  localError.value = ''
  fileInput.value?.click()
}

function onPicked(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.files !== null) void acceptFiles(Array.from(input.files))
  input.value = ''
}

function isImage(file: File): boolean {
  return file.type.startsWith('image/')
}

function validate(file: File): string | null {
  const suffixes = caps.data?.suffixes ?? []
  if (suffixes.length > 0) {
    const lower = file.name.toLowerCase()
    if (!suffixes.some((suffix) => lower.endsWith(suffix.toLowerCase()))) {
      return `不支持的类型 ${file.name}`
    }
  }
  const maxMb = caps.data?.max_upload_mb
  if (maxMb !== undefined && file.size > maxMb * 1024 * 1024) {
    return `文件过大（≤ ${maxMb} MB）`
  }
  return null
}

async function acceptFiles(files: File[]): Promise<void> {
  if (!isAdmin.value) {
    notify('无上传权限', 'error')
    return
  }
  if (files.length === 0) return
  localError.value = ''
  for (const file of files) {
    const invalid = validate(file)
    if (invalid !== null) {
      localError.value = invalid
      continue
    }
    // 必须是 reactive：push 进数组的是**代理**，之后若改原始对象，界面不会更新
    const item = reactive<PendingAttachment>({
      id: ++attachCounter,
      name: file.name,
      size: file.size,
      previewUrl: isImage(file) ? URL.createObjectURL(file) : null,
      status: 'ready',
      detail: '上传中…',
      uploading: true,
    })
    pending.value.push(item)
    try {
      await api.uploadDocument(file)
      item.uploading = false
      item.detail = '已提交 · 入库中'
    } catch (failure) {
      item.uploading = false
      item.status = 'failed'
      item.detail = (failure as Error).message
    }
  }
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  if (!isAdmin.value) {
    notify('无上传权限', 'error')
    return
  }
  const files = event.dataTransfer?.files
  if (files !== undefined && files !== null && files.length > 0) {
    void acceptFiles(Array.from(files))
  }
}

function onDragOver(): void {
  if (isAdmin.value) dragging.value = true
}

function onDragLeave(event: DragEvent): void {
  const related = event.relatedTarget
  if (related instanceof Node && (event.currentTarget as HTMLElement).contains(related)) return
  dragging.value = false
}

function remove(item: PendingAttachment): void {
  pending.value = pending.value.filter((entry) => entry.id !== item.id)
  if (item.previewUrl !== null) URL.revokeObjectURL(item.previewUrl)
}

onBeforeUnmount(() => {
  // 只回收"还没发出去"的缩略图；已进入消息的那份要留着给消息用
  for (const item of pending.value) {
    if (item.previewUrl !== null) URL.revokeObjectURL(item.previewUrl)
  }
})

void loadSources()
</script>

<template>
  <div
    class="composer"
    @dragover.prevent="onDragOver"
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div v-if="ui.mobile" class="mobile-scope">
      <Icon name="book" :size="13" />
      <select v-model="params.source_filter" aria-label="知识库范围" @change="persist">
        <option :value="null">全部知识库</option>
        <option v-for="name in sources.items" :key="name" :value="name">{{ name }}</option>
      </select>
    </div>

    <div v-if="pending.length > 0" class="pending">
      <div
        v-for="item in pending"
        :key="item.id"
        class="pending-item"
        :class="{ failed: item.status === 'failed' }"
      >
        <img
          v-if="item.previewUrl !== null"
          :src="item.previewUrl"
          :alt="item.name"
          class="pending-thumb"
        />
        <span v-else class="pending-file"><Icon name="file" :size="14" /></span>
        <div class="pending-text">
          <span class="pending-name" :title="item.name">{{ item.name }}</span>
          <span class="pending-meta">
            {{ humanBytes(item.size) }} · {{ item.detail }}
          </span>
        </div>
        <span v-if="item.uploading" class="spin small-spin"></span>
        <button
          class="icon-btn"
          type="button"
          title="移除附件"
          aria-label="移除附件"
          @click="remove(item)"
        >
          <Icon name="close" :size="13" />
        </button>
      </div>
    </div>

    <div class="box" :class="{ dragging }">
      <textarea
        ref="textarea"
        v-model="draft"
        rows="1"
        placeholder="输入问题…"
        aria-label="输入问题"
        @keydown="onKeydown"
        @input="autoGrow"
      ></textarea>

      <div class="toolbar">
        <div class="toolbar-left">
          <template v-if="isAdmin">
            <input
              ref="fileInput"
              class="hidden"
              type="file"
              multiple
              :accept="accept"
              @change="onPicked"
            />
            <button
              class="icon-btn"
              type="button"
              title="添加附件（上传到知识库）"
              aria-label="添加附件"
              @click="pickFiles"
            >
              <Icon name="paperclip" :size="17" />
            </button>
          </template>
          <label v-if="!ui.mobile" class="scope">
            <Icon name="book" :size="12" />
            <select v-model="params.source_filter" aria-label="知识库范围" @change="persist">
              <option :value="null">全部知识库</option>
              <option v-for="name in sources.items" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
          <span class="faint scope-label" :title="`当前检索范围：${scopeLabel}`">
            {{ scopeLabel }}
          </span>
        </div>

        <div class="toolbar-right">
          <span class="faint hint">{{ uploading ? '上传中' : 'Enter 发送' }}</span>
          <button v-if="chat.generating" class="danger solid stop" type="button" @click="stop">
            <Icon name="stop" :size="13" />
            停止
          </button>
          <button
            v-else
            class="primary send"
            type="button"
            :disabled="!canSend"
            :title="
              !weightsValid
                ? '两路权重不能同时为 0'
                : canSend
                  ? '发送'
                  : '先输入一个问题'
            "
            @click="submit"
          >
            <Icon name="send" :size="14" />
            发送
          </button>
        </div>
      </div>

      <div v-if="dragging" class="drop-hint">
        <Icon name="paperclip" :size="16" />
        拖拽上传
      </div>
    </div>

    <p v-if="!weightsValid" class="alert warn small">
      权重不能同时为 0
    </p>
    <p v-if="localError" class="alert error small">{{ localError }}</p>
    <p v-if="chat.notice" class="alert error small">{{ chat.notice }}</p>
    <p v-if="caps.error" class="faint caps-error">
      能力读取失败：{{ caps.error }}
    </p>
  </div>
</template>

<style scoped>
.composer {
  flex: none;
  padding: 6px clamp(12px, 3vw, 40px) 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: calc(var(--content-max) + 80px);
  width: 100%;
  margin: 0 auto;
}

.mobile-scope {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-faint);
}

.mobile-scope select {
  height: 28px;
  font-size: 12px;
}

.pending {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pending-item {
  display: flex;
  align-items: center;
  gap: 7px;
  max-width: 300px;
  padding: 5px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.pending-item.failed {
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
}

.pending-thumb {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-xs);
  object-fit: cover;
  flex: none;
}

.pending-file {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-xs);
  background: var(--bg-sunken);
  color: var(--text-faint);
  flex: none;
}

.pending-text {
  min-width: 0;
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.pending-name {
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pending-meta {
  font-size: 10px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pending-item.failed .pending-meta {
  color: var(--danger);
}

.small-spin {
  width: 11px;
  height: 11px;
}

.hidden {
  display: none;
}

.box {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 8px 6px;
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xs);
  transition: border-color 0.12s ease, box-shadow 0.12s ease;
}

.box:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border-strong));
  box-shadow: 0 0 0 3px var(--accent-ring);
}

.box.dragging {
  border-style: dashed;
  border-color: var(--accent);
  background: var(--accent-soft);
}

textarea {
  border: none;
  background: transparent;
  padding: 4px 6px;
  min-height: 24px;
  max-height: 200px;
  line-height: 1.6;
  resize: none;
  box-shadow: none;
  overflow-y: auto;
}

textarea:focus {
  border: none;
  box-shadow: none;
  outline: none;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.scope {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 6px;
  height: 26px;
  border-radius: var(--radius-sm);
  color: var(--text-faint);
}

.scope:hover {
  background: var(--bg-hover);
}

.scope select {
  height: 24px;
  width: auto;
  max-width: 132px;
  border: none;
  background: transparent;
  padding: 0 2px;
  font-size: 11.5px;
  color: var(--text-muted);
  box-shadow: none;
  cursor: pointer;
}

.scope select:focus {
  box-shadow: none;
  border: none;
}

.scope-label {
  display: none;
}

.hint {
  margin-right: 2px;
  white-space: nowrap;
}

.send,
.stop {
  height: 34px;
  padding: 0 13px;
  border-radius: var(--radius-sm);
}

.drop-hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--accent-soft) 88%, transparent);
  color: var(--accent);
  font-size: 13px;
  pointer-events: none;
}

.small {
  font-size: 12px;
}

.caps-error {
  margin: 0;
  padding: 0 4px;
}

@media (max-width: 768px) {
  .composer {
    padding: 4px 8px 10px;
  }

  .scope {
    display: none;
  }

  .scope-label {
    display: inline;
    max-width: 120px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding: 0 4px;
  }

  .hint {
    display: none;
  }

  .send,
  .stop {
    height: 32px;
    padding: 0 10px;
  }

  .pending-item {
    max-width: 100%;
  }
}
</style>

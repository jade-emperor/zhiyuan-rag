<script setup lang="ts">
/**
 * 消息流。
 *
 * 四种"不是正常答案"的状态各自有话术，**不合并成一句"出错了"**：
 * 拒答（知识库里没有）、截断（模型没说完）、用户停止、传输错误 ——
 * 它们的下一步动作完全不同（换问法 / 看完整答案 / 重问 / 查后端）。
 * 限流又是第五种：它是**兜底话术**，不是错误，所以用 info 语气而不是红字。
 *
 * 渲染：答案走自研 Markdown（`utils/markdown.ts`），全部先转义再拼标签；
 * 只有渲染器产出的白名单 HTML 会进 `v-html`。行内 `[n]` 变成真按钮，点开右栏原文。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import type { Citation } from '../api/types'
import { isAdmin } from '../stores/auth'
import {
  chatState,
  regenerateLast,
  setFeedback,
  stop,
  type ChatMessage,
} from '../stores/chat'
import { copyText, notify, selectCitation, uiState } from '../stores/ui'
import { assessConfidence, type ConfidenceAssessment } from '../utils/confidence'
import { fileName, pageLabel, shorten } from '../utils/citations'
import { escapeHtml } from '../utils/highlight'
import { renderMarkdown } from '../utils/markdown'
import ChatWelcome from './ChatWelcome.vue'
import CitationBlock from './CitationBlock.vue'
import Icon from './Icon.vue'
import MessageActions from './MessageActions.vue'

const props = defineProps<{ messages: ChatMessage[] }>()
const emit = defineEmits<{ 'focus-composer': []; suggest: [text: string] }>()

const chat = chatState()
const ui = uiState()
const router = useRouter()

interface Rendered {
  key: string
  html: string
  codes: string[]
}

const cache = new Map<number, Rendered>()
const copiedAnswer = ref<number | null>(null)
/** 默认展开；记录"用户主动收起"的那几条，避免展开一条把别的也收掉。 */
const collapsed = reactive(new Set<number>())
const uploadHint = reactive(new Set<number>())
const now = ref(Date.now())
const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)
const tip = ref<{ x: number; y: number; citation: Citation } | null>(null)

let timer: number | null = null
let copyTimer: number | null = null

const channelLabel: Record<string, string> = {
  faq: 'FAQ',
  rag: '知识库',
  greeting: '寒暄',
  // 限流:回的是**兜底话术**,不是错误 —— 所以用 warn 语气提示"稍后再试",
  // 而不是红字(红字会让人以为系统坏了,然后去点重试按钮点得更快)。
  throttled: '稍后重试',
}

/** 兜底话术的徽章配色:只有限流那一档是暖色,其余是中性蓝。 */
function badgeTone(message: ChatMessage): string {
  return message.channel === 'throttled' ? 'tag warn' : 'tag info'
}

function badge(message: ChatMessage): string {
  if (message.channel === null) return ''
  return channelLabel[message.channel] ?? message.channel
}

function signature(message: ChatMessage): string {
  return `${message.text}\u0000${message.citations.map((item) => `${item.index}:${item.score}`).join(',')}`
}

/** 输入:一条消息。行为:按文本与引用签名缓存渲染结果（流式时只有最后一条会重算）。输出:HTML 与代码块原文。 */
function body(message: ChatMessage): Rendered {
  const key = signature(message)
  const hit = cache.get(message.id)
  if (hit !== undefined && hit.key === key) return hit
  try {
    const result = renderMarkdown(message.text, { citations: message.citations })
    const rendered: Rendered = { key, html: result.html, codes: result.codes }
    cache.set(message.id, rendered)
    return rendered
  } catch {
    // 渲染器不该抛；真抛了也不能让整条消息流白屏
    const escaped = escapeHtml(message.text).replace(/\n/g, '<br />')
    const fallback: Rendered = { key, html: escaped, codes: [] }
    cache.set(message.id, fallback)
    return fallback
  }
}

function confidenceOf(message: ChatMessage): ConfidenceAssessment {
  return assessConfidence({
    channel: message.channel,
    refused: message.refused,
    citations: message.citations,
    mode: message.request?.mode ?? undefined,
    fusion: message.request?.fusion,
  })
}

function isExpanded(message: ChatMessage): boolean {
  return !collapsed.has(message.id)
}

function toggleCitations(message: ChatMessage): void {
  if (collapsed.has(message.id)) collapsed.delete(message.id)
  else collapsed.add(message.id)
}

const lastAssistantId = computed(() => {
  for (let i = props.messages.length - 1; i >= 0; i -= 1) {
    if (props.messages[i].role === 'assistant') return props.messages[i].id
  }
  return null
})

function canRegenerate(message: ChatMessage): boolean {
  return (
    !chat.generating &&
    !message.streaming &&
    message.role === 'assistant' &&
    message.id === lastAssistantId.value
  )
}

function regenerateTitle(message: ChatMessage): string {
  if (message.id !== lastAssistantId.value) return '仅最后一轮可重新生成'
  if (chat.generating) return '生成中'
  return '重新生成'
}

function stamp(message: ChatMessage): string {
  if (message.createdAt <= 0) return ''
  return new Date(message.createdAt).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

function afterSeconds(message: ChatMessage): number {
  if (message.createdAt <= 0) return 0
  return Math.max(0, Math.floor((now.value - message.createdAt) / 1000))
}

async function copyAnswer(message: ChatMessage): Promise<void> {
  const ok = await copyText(message.text)
  if (ok) {
    copiedAnswer.value = message.id
    if (copyTimer !== null) window.clearTimeout(copyTimer)
    copyTimer = window.setTimeout(() => {
      copiedAnswer.value = null
      copyTimer = null
    }, 1600)
    notify('已复制回答', 'success')
  } else {
    notify('复制失败：浏览器不允许访问剪贴板，请手动选择文本', 'error')
  }
}

function onBodyClick(event: MouseEvent, message: ChatMessage): void {
  const target = event.target
  if (!(target instanceof Element)) return

  const codeButton = target.closest<HTMLElement>('[data-code]')
  if (codeButton !== null) {
    const index = Number(codeButton.dataset.code)
    const text = body(message).codes[index]
    if (text === undefined) return
    void copyText(text).then((ok) => {
      if (!ok) {
        notify('复制失败：浏览器不允许访问剪贴板', 'error')
        return
      }
      codeButton.textContent = '已复制'
      window.setTimeout(() => {
        if (codeButton.isConnected) codeButton.textContent = '复制'
      }, 1500)
      notify('已复制代码', 'success')
    })
    return
  }

  const citeButton = target.closest<HTMLElement>('[data-cite]')
  if (citeButton !== null) {
    const index = Number(citeButton.dataset.cite)
    const citation = message.citations.find((item) => item.index === index)
    if (citation === undefined) return
    tip.value = null
    selectCitation(message.id, citation)
  }
}

function showTip(event: Event, message: ChatMessage): void {
  const target = event.target
  if (!(target instanceof Element)) return
  const citeButton = target.closest<HTMLElement>('[data-cite]')
  if (citeButton === null) return
  const index = Number(citeButton.dataset.cite)
  const citation = message.citations.find((item) => item.index === index)
  if (citation === undefined) return
  const rect = citeButton.getBoundingClientRect()
  const width = 288
  const x = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))
  const y = Math.min(rect.bottom + 8, window.innerHeight - 132)
  tip.value = { x, y, citation }
}

function hideTip(event: Event): void {
  const target = event.target
  if (target instanceof Element && target.closest('[data-cite]') !== null) tip.value = null
}

function openCitation(message: ChatMessage, citation: Citation): void {
  selectCitation(message.id, citation)
}

function askUpload(message: ChatMessage): void {
  if (isAdmin.value) {
    void router.push({ name: 'knowledge' })
    return
  }
  uploadHint.add(message.id)
}

function retryMessage(): void {
  void regenerateLast()
}

function onScroll(): void {
  const element = scroller.value
  if (element === null) return
  atBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 120
  tip.value = null
}

function scrollToBottom(): void {
  const element = scroller.value
  if (element === null) return
  element.scrollTop = element.scrollHeight
  atBottom.value = true
}

watch(
  () => {
    const last = props.messages[props.messages.length - 1]
    if (last === undefined) return ''
    return `${last.id}:${last.text.length}:${last.citations.length}:${last.streaming}:${last.stopped}`
  },
  async () => {
    if (!atBottom.value) return
    await nextTick()
    scrollToBottom()
  },
)

watch(
  () => props.messages.length,
  async () => {
    if (props.messages.length === 0) return
    await nextTick()
    scrollToBottom()
  },
)

onMounted(() => {
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 500)
  scrollToBottom()
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
  if (copyTimer !== null) window.clearTimeout(copyTimer)
})
</script>

<template>
  <div class="messages-wrap">
    <div ref="scroller" class="messages" role="log" aria-label="对话消息" @scroll="onScroll">
      <p class="sr-only" aria-live="polite">
        {{ chat.generating ? '正在生成回答' : '已生成完毕' }}
      </p>

      <div v-if="chat.loadingHistory && messages.length === 0" class="skeleton-list" aria-hidden="true">
        <div
          v-for="index in 3"
          :key="index"
          class="skeleton skeleton-row"
          :style="{ width: index === 2 ? '62%' : '84%' }"
        ></div>
      </div>

      <ChatWelcome
        v-else-if="messages.length === 0"
        @suggest="emit('suggest', $event)"
      />

      <template v-for="message in messages" :key="message.id">
        <!-- 用户消息：右对齐、浅色气泡；附件是上传到知识库后的快照 -->
        <div v-if="message.role === 'user'" class="msg user">
          <div class="user-stack">
            <div v-if="message.attachments.length > 0" class="attachments">
              <div
                v-for="attachment in message.attachments"
                :key="attachment.id"
                class="attachment"
                :class="{ failed: attachment.status === 'failed' }"
                :title="attachment.detail"
              >
                <img
                  v-if="attachment.previewUrl !== null"
                  :src="attachment.previewUrl"
                  :alt="attachment.name"
                  class="thumb"
                />
                <span v-else class="file-icon"><Icon name="file" :size="15" /></span>
                <span class="attachment-name" :title="attachment.name">{{ attachment.name }}</span>
                <span class="attachment-state">
                  {{ attachment.status === 'failed' ? '上传失败' : '入库中' }}
                </span>
              </div>
            </div>
            <div class="user-bubble">{{ message.text }}</div>
            <span v-if="stamp(message)" class="msg-time">{{ stamp(message) }}</span>
          </div>
        </div>

        <!-- AI 消息：左对齐、无气泡、接近文档阅读 -->
        <div v-else class="msg assistant">
          <div class="avatar" aria-hidden="true">
            <Icon name="sparkles" :size="15" />
          </div>
          <div class="ai-body">
            <div class="ai-head">
              <span class="ai-name">RAG Assistant</span>
              <span v-if="badge(message)" :class="badgeTone(message)">{{ badge(message) }}</span>
              <span v-if="message.regenerated" class="tag soft">已重新生成</span>
              <span v-if="stamp(message)" class="faint">{{ stamp(message) }}</span>
            </div>

            <div
              v-if="confidenceOf(message).level === 'medium' && !message.streaming"
              class="confidence-medium"
              :title="confidenceOf(message).detail"
            >
              <Icon name="alert" :size="13" />
              AI 建议
            </div>

            <!-- v-html 的内容只来自 renderMarkdown：全文先转义、标签白名单、链接白名单。
                 后端原文不会被当成 HTML 直接插入。 -->
            <div
              v-if="message.text.length > 0"
              class="md"
              v-html="body(message).html"
              @click="onBodyClick($event, message)"
              @mouseover="showTip($event, message)"
              @mouseout="hideTip"
              @focusin="showTip($event, message)"
              @focusout="hideTip"
            ></div>

            <div v-if="message.streaming && message.text.length === 0" class="waiting">
              <span class="spin"></span>
              <span>
                {{
                  message.citations.length > 0
                    ? `已找到 ${message.citations.length} 条依据 · 生成中`
                    : '正在检索资料…'
                }}
              </span>
              <span class="faint">{{ afterSeconds(message) }}s</span>
            </div>

            <span
              v-if="message.streaming && message.text.length > 0"
              class="caret"
              aria-hidden="true"
            ></span>

            <div
              v-if="!message.streaming && (message.refused || confidenceOf(message).level === 'low')"
              class="low-confidence"
            >
              <div class="low-head">
                <span class="low-icon"><Icon name="alert" :size="16" /></span>
                <strong>知识库中未找到足够信息</strong>
              </div>
              <p class="low-text">
                {{
                  message.refused || confidenceOf(message).basis === 'refused'
                    ? '没有可用资料。'
                    : '匹配度偏低，建议核实。'
                }}
              </p>
              <div class="low-actions">
                <button class="primary" type="button" @click="emit('focus-composer')">
                  换个问法
                </button>
                <button class="ghost" type="button" @click="askUpload(message)">上传相关文档</button>
              </div>
              <p v-if="uploadHint.has(message.id)" class="faint low-note">
                无上传权限。
              </p>
            </div>

            <p v-if="message.channel === 'throttled'" class="alert info">
              这不是出错 —— 稍微慢一点再问。
            </p>
            <p v-if="message.truncated" class="alert error">
              回答被截断。
            </p>
            <p v-if="message.stopped" class="alert info">已停止生成。</p>
            <p v-if="message.error" class="alert error">
              {{ message.error }}
              <button
                v-if="canRegenerate(message)"
                class="ghost retry"
                type="button"
                @click="retryMessage"
              >
                重试
              </button>
            </p>

            <CitationBlock
              :message="message"
              :assessment="confidenceOf(message)"
              :expanded="isExpanded(message)"
              :selected-index="
                ui.selected !== null && ui.selected.messageId === message.id
                  ? ui.selected.citation.index
                  : null
              "
              @toggle="toggleCitations(message)"
              @open="openCitation(message, $event)"
            />

            <MessageActions
              v-if="!message.streaming"
              :message="message"
              :can-regenerate="canRegenerate(message)"
              :regenerate-title="regenerateTitle(message)"
              :copied="copiedAnswer === message.id"
              @copy="copyAnswer(message)"
              @regenerate="retryMessage"
              @feedback="setFeedback(message.id, $event)"
              @citations="toggleCitations(message)"
            />

            <div v-if="message.streaming" class="streaming-actions">
              <span class="faint">生成中…</span>
              <button class="danger" type="button" @click="stop">停止生成</button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <button
      v-if="!atBottom && messages.length > 0"
      class="jump"
      type="button"
      title="回到最新消息"
      aria-label="回到最新消息"
      @click="scrollToBottom"
    >
      <Icon name="arrow-down" :size="15" />
      最新
    </button>

    <div
      v-if="tip !== null"
      class="cite-tip"
      role="tooltip"
      :style="{ left: `${tip.x}px`, top: `${tip.y}px` }"
    >
      <div class="tip-head">
        <span class="tip-index">[{{ tip.citation.index }}]</span>
        <span class="tip-file" :title="tip.citation.doc_id">{{ fileName(tip.citation) }}</span>
      </div>
      <div class="tip-meta">
        <span>{{ tip.citation.source }}</span>
        <span>·</span>
        <span>{{ pageLabel(tip.citation) }}</span>
      </div>
      <p class="tip-preview">{{ shorten(tip.citation.preview, 130) }}</p>
      <p class="tip-hint">点击查看</p>
    </div>
  </div>
</template>

<style scoped>
.messages-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 24px clamp(16px, 3vw, 40px) 8px;
  display: flex;
  flex-direction: column;
  gap: 26px;
  scroll-behavior: smooth;
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-top: 16px;
}

.skeleton-row {
  height: 18px;
}

/* ── 欢迎 / 空状态 ─────────────────────────────────────────────────── */
/* ── 消息通用 ─────────────────────────────────────────────────────── */
.msg {
  display: flex;
  gap: 10px;
  width: 100%;
  max-width: var(--content-max);
  margin: 0 auto;
}

.msg.user {
  justify-content: flex-end;
}

.user-stack {
  max-width: min(560px, 84%);
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}

.user-bubble {
  padding: 9px 13px;
  border-radius: var(--radius) var(--radius) 2px var(--radius);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  color: var(--text);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.msg-time {
  padding-right: 4px;
  font-size: 10.5px;
  color: var(--text-faint);
}

.attachments {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}

.attachment {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-size: 11.5px;
  color: var(--text-muted);
}

.attachment.failed {
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
}

.thumb {
  width: 28px;
  height: 28px;
  object-fit: cover;
  border-radius: var(--radius-xs);
  flex: none;
}

.file-icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-xs);
  background: var(--bg-sunken);
  color: var(--text-faint);
  flex: none;
}

.attachment-name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-state {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-faint);
  white-space: nowrap;
}

.attachment.failed .attachment-state {
  color: var(--danger);
}

/* ── AI 消息 ──────────────────────────────────────────────────────── */
.msg.assistant {
  align-items: flex-start;
}

.avatar {
  width: 26px;
  height: 26px;
  margin-top: 2px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--accent);
}

.ai-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ai-head {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  min-height: 20px;
}

.ai-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
}

.confidence-medium {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: var(--radius-xs);
  background: var(--warn-soft);
  border: 1px solid var(--warn-border);
  color: var(--warn);
  font-size: 11px;
  font-weight: 500;
  cursor: help;
}

.waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 13px;
  padding: 6px 0;
}

.caret {
  display: inline-block;
  width: 2px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--accent);
  animation: blink 1s step-start infinite;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

/* ── 低置信度卡片 ──────────────────────────────────────────────────── */
.low-confidence {
  border: 1px solid var(--warn-border);
  border-radius: var(--radius);
  background: var(--warn-soft);
  padding: 11px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.low-head {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--warn);
  font-size: 13px;
}

.low-icon {
  display: grid;
  place-items: center;
}

.low-text {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-muted);
}

.low-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.low-actions button {
  height: 30px;
  font-size: 12px;
}

.low-note {
  margin: 0;
}

.retry {
  margin-left: 6px;
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
}

/* ── 引用 ──────────────────────────────────────────────────────────── */
/* ── 操作栏 ────────────────────────────────────────────────────────── */
.streaming-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.streaming-actions button {
  height: 28px;
  font-size: 12px;
}

.streaming-actions button.danger {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
}

.streaming-actions button.danger:hover:not(:disabled) {
  background: var(--danger-soft);
}

/* ── 回到底部 ──────────────────────────────────────────────────────── */
.jump {
  position: absolute;
  left: 50%;
  bottom: 10px;
  transform: translateX(-50%);
  height: 28px;
  padding: 0 10px;
  gap: 4px;
  font-size: 11.5px;
  border-radius: var(--radius-full);
  background: var(--bg);
  box-shadow: var(--shadow-md);
  z-index: var(--z-sticky);
}

/* ── 行内引用浮层 ───────────────────────────────────────────────────── */
.cite-tip {
  position: fixed;
  z-index: var(--z-tooltip);
  width: 288px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.tip-head {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.tip-index {
  flex: none;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 10.5px;
  font-weight: 600;
}

.tip-file {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
}

.tip-meta {
  display: flex;
  gap: 4px;
  color: var(--text-faint);
  font-size: 10.5px;
}

.tip-preview {
  margin: 2px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.tip-hint {
  margin: 2px 0 0;
  color: var(--accent);
  font-size: 10.5px;
}

@media (max-width: 768px) {
  .messages {
    padding: 16px 12px 8px;
    gap: 20px;
  }

  .user-stack {
    max-width: 88%;
  }

  .cite-tip {
    display: none;
  }
}
</style>

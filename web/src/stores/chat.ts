/**
 * 会话与消息。
 *
 * **会话是"首次提问时才建"的**：点「新对话」只清空本地，不调后端 ——
 * 否则左侧列表会堆满一堆 0 轮的空会话（后端也只列有轮次的会话）。
 *
 * 帧的处理有一处刻意的选择：**`done` 帧里的 `answer` 是权威文本**，不是把 token
 * 拼起来的结果。两者本应一致，但"本应"不是契约 —— 万一不一致，以服务端给的完整
 * 答案为准，排版与复制出来的内容才是后端真正生成的那份。
 *
 * 这一版为三栏工作台补了四件事，都是**只用现有后端**能做的：
 * 1. 每条消息记下当时的检索参数快照（重新生成 / 置信度解释要用）；
 * 2. 会话别名（重命名）存 localStorage —— 后端没有 rename 接口，界面如实标成"本机名称"；
 * 3. 点赞/点踩**落到后端**（带这一轮的问题与答案快照）：刷新后按钮状态回到未选，
 *    但记录已经在库里（同一个键是覆盖写，重复点不会让计数翻倍）；
 * 4. 「重新生成」只能重跑**最后一轮**：会话是追加式的，重跑旧轮会让历史出现两份。
 *    按钮在生成中禁用，旧轮的消息上禁用并给出原因。
 */

import { reactive } from 'vue'

import { notifyUnauthorized } from '../api/client'
import * as api from '../api/endpoints'
import { ChatSocket } from '../api/stream'
import type { Channel, Citation, SessionSummary, StreamFrame } from '../api/types'
import { toQueryRequest, type BuiltQuery } from './params'
import { closePanel } from './ui'

export type Feedback = 'up' | 'down'

/** 用户消息里的一个附件。附件走知识库上传接口，不是聊天接口（后端没有聊天附件字段）。 */
export interface ChatAttachment {
  id: number
  name: string
  size: number
  /** 图片的本地缩略图；非图片为 null。 */
  previewUrl: string | null
  /** ready = 已提交入库；failed = 上传失败（仍显示在消息里，不静默丢）。 */
  status: 'ready' | 'failed'
  detail: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  text: string
  citations: Citation[]
  channel: Channel | null
  refused: boolean
  truncated: boolean
  /** 用户按了「停止生成」：与"服务端截断"是两回事，提示语也不同。 */
  stopped: boolean
  error: string | null
  /** 还在等这一轮结束（用于画光标与禁用输入）。 */
  streaming: boolean
  /** 这一条是什么时候出现的（本地时间；历史消息用 0）。 */
  createdAt: number
  /** 提问时的检索参数快照；历史消息没有（旧账没存）。 */
  request: BuiltQuery | null
  /** 这一轮反馈：写入后端；刷新页面后按钮状态回到未选（记录仍在库里）。 */
  feedback: Feedback | null
  /** 用户消息的附件快照。 */
  attachments: ChatAttachment[]
  /** 这一条是「重新生成」产出的。 */
  regenerated: boolean
}

export interface SessionGroup {
  label: string
  items: SessionSummary[]
}

const ALIAS_KEY = 'project-rag.session-aliases'

function loadAliases(): Record<string, string> {
  try {
    const raw = localStorage.getItem(ALIAS_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (typeof parsed !== 'object' || parsed === null) return {}
    const result: Record<string, string> = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === 'string' && value.trim().length > 0) result[key] = value.trim()
    }
    return result
  } catch {
    localStorage.removeItem(ALIAS_KEY)
    return {}
  }
}

const aliases = reactive<Record<string, string>>(loadAliases())

const state = reactive({
  sessions: [] as SessionSummary[],
  currentSessionId: null as string | null,
  messages: [] as ChatMessage[],
  generating: false,
  loadingHistory: false,
  /** 会话列表相关的错误（与问答错误分开显示：一个是"列表拉不到"，一个是"这轮答不了"）。 */
  sessionsError: '',
  notice: '',
  /** 正在提交反馈的那条消息（按钮置灰用；空 = 没有在跑的动作）。 */
  feedbackPending: null as number | null,
})

let counter = 0
let socket: ChatSocket | null = null

export function chatState() {
  return state
}

function releaseAttachments(messages: readonly ChatMessage[]): void {
  for (const message of messages) {
    for (const attachment of message.attachments) {
      if (attachment.previewUrl !== null) URL.revokeObjectURL(attachment.previewUrl)
    }
  }
}

function blankMessage(role: ChatMessage['role']): Omit<ChatMessage, 'id'> {
  return {
    role,
    text: '',
    citations: [],
    channel: null,
    refused: false,
    truncated: false,
    stopped: false,
    error: null,
    streaming: false,
    createdAt: Date.now(),
    request: null,
    feedback: null,
    attachments: [],
    regenerated: false,
  }
}

function push(message: Omit<ChatMessage, 'id'>): ChatMessage {
  const full: ChatMessage = { id: ++counter, ...message }
  state.messages.push(full)
  return full
}

function assistantMessage(): ChatMessage {
  return push({ ...blankMessage('assistant'), streaming: true })
}

function lastIndexOf(role: ChatMessage['role']): number {
  for (let i = state.messages.length - 1; i >= 0; i -= 1) {
    if (state.messages[i].role === role) return i
  }
  return -1
}

function ensureSocket(): ChatSocket {
  if (socket) return socket
  socket = new ChatSocket({
    onFrame: handleFrame,
    onClose: (event) => {
      // 1008 = 未认证:后端在 close 之前已经发过一帧 error,这里只负责把令牌状态纠正过来
      if (event.code === 1008) {
        state.notice = '登录已失效,请重新登录'
        notifyUnauthorized()
      }
      if (state.generating) failCurrent('连接中断,这一轮没有收完')
    },
  })
  return socket
}

function currentStreaming(): ChatMessage | null {
  for (let i = state.messages.length - 1; i >= 0; i -= 1) {
    const message = state.messages[i]
    if (message.role === 'assistant' && message.streaming) return message
  }
  return null
}

function handleFrame(frame: StreamFrame): void {
  const message = currentStreaming()
  if (frame.type === 'sources') {
    // 依据先到就先显示 —— 实测:sources 帧 1.5s 到、第一个字 5s 才到,
    // 用户在等的这几秒里不该盯着空白。
    if (message && frame.citations.length > 0) message.citations = frame.citations
    return
  }
  if (frame.type === 'token') {
    if (message) message.text += frame.text
    return
  }
  if (frame.type === 'done') {
    if (message) {
      message.text = frame.answer.length > 0 ? frame.answer : message.text
      message.channel = frame.channel
      message.refused = frame.refused
      message.truncated = frame.truncated
      if (message.citations.length === 0) message.citations = frame.citations
      message.streaming = false
    }
    if (frame.session_id) state.currentSessionId = frame.session_id
    state.generating = false
    void refreshSessions()
    return
  }
  // error:后端刻意"回一帧但不断连接"(请求体不合法 / 越权都是这种),所以只显示不重连
  if (message) {
    message.error = frame.detail
    message.streaming = false
  } else {
    state.notice = frame.detail
  }
  state.generating = false
}

function failCurrent(detail: string): void {
  const message = currentStreaming()
  if (message) {
    message.error = detail
    message.streaming = false
  } else {
    state.notice = detail
  }
  state.generating = false
}

export async function refreshSessions(): Promise<void> {
  try {
    const result = await api.sessions()
    state.sessions = result.sessions
    state.sessionsError = ''
  } catch (error) {
    state.sessionsError = (error as Error).message
  }
}

/** 会话在界面上的名字：本机别名优先，其次后端标题，最后兜底。 */
export function sessionTitle(session: SessionSummary): string {
  return aliases[session.session_id] ?? (session.title?.trim() || '未命名会话')
}

export function sessionTitleOf(sessionId: string | null): string {
  if (sessionId === null) return '新对话'
  const found = state.sessions.find((item) => item.session_id === sessionId)
  return found === undefined ? '当前对话' : sessionTitle(found)
}

export function currentSessionTitle(): string {
  return sessionTitleOf(state.currentSessionId)
}

/** 输入:会话 ID 与新名字。行为:空名字 = 删除本机别名；写 localStorage。输出:无。 */
export function renameSession(sessionId: string, title: string): void {
  const trimmed = title.trim()
  if (trimmed.length === 0) delete aliases[sessionId]
  else aliases[sessionId] = trimmed
  localStorage.setItem(ALIAS_KEY, JSON.stringify(aliases))
}

function dayStart(offsetDays = 0): number {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - offsetDays).getTime()
}

/** 输入:搜索词。行为:按别名/标题过滤，再按最近活动分组。输出:分组后的会话。 */
export function groupedSessions(query = ''): SessionGroup[] {
  const needle = query.trim().toLowerCase()
  const filtered = state.sessions.filter((item) => {
    if (needle.length === 0) return true
    return sessionTitle(item).toLowerCase().includes(needle)
  })
  const buckets: SessionGroup[] = [
    { label: '今天', items: [] },
    { label: '昨天', items: [] },
    { label: '近 7 天', items: [] },
    { label: '更早', items: [] },
  ]
  const today = dayStart()
  const yesterday = dayStart(1)
  const week = dayStart(7)
  for (const item of filtered) {
    const time = new Date(item.updated_at).getTime()
    if (Number.isNaN(time) || time < week) buckets[3].items.push(item)
    else if (time >= today) buckets[0].items.push(item)
    else if (time >= yesterday) buckets[1].items.push(item)
    else buckets[2].items.push(item)
  }
  return buckets.filter((bucket) => bucket.items.length > 0)
}

export function newChat(): void {
  stop()
  releaseAttachments(state.messages)
  state.currentSessionId = null
  state.messages = []
  state.notice = ''
  closePanel()
}

export async function openSession(sessionId: string): Promise<void> {
  if (state.generating) stop()
  state.loadingHistory = true
  state.notice = ''
  try {
    const result = await api.history(sessionId)
    releaseAttachments(state.messages)
    state.currentSessionId = sessionId
    state.messages = []
    closePanel()
    for (const turn of result.rounds) {
      push({
        ...blankMessage('user'),
        text: turn.question,
        createdAt: 0,
      })
      push({
        ...blankMessage('assistant'),
        text: turn.answer,
        // 引用跟着答案一起回来(当轮那张卡片的快照,存在会话表里)。
        // 老账没有这一段 → 空数组:那一轮的出处当时没留下,不编。
        citations: turn.citations ?? [],
        createdAt: 0,
      })
    }
  } catch (error) {
    state.notice = (error as Error).message
  } finally {
    state.loadingHistory = false
  }
}

export async function removeSession(sessionId: string): Promise<void> {
  try {
    await api.clearSession(sessionId)
  } catch (error) {
    state.notice = (error as Error).message
    return
  }
  if (aliases[sessionId] !== undefined) {
    delete aliases[sessionId]
    localStorage.setItem(ALIAS_KEY, JSON.stringify(aliases))
  }
  if (state.currentSessionId === sessionId) newChat()
  await refreshSessions()
}

/** 提问。附件只是用户消息上的快照：它们已由输入区走知识库上传接口提交。 */
export async function send(text: string, attachments: ChatAttachment[] = []): Promise<void> {
  const query = text.trim()
  if (query.length === 0 || state.generating) return
  state.notice = ''
  push({
    ...blankMessage('user'),
    text: query,
    attachments: [...attachments],
  })
  const assistant = assistantMessage()
  state.generating = true
  try {
    if (state.currentSessionId === null) {
      // 归属在**创建那一刻**绑定:所以必须先建会话、再拿它的 ID 提问
      const created = await api.createSession()
      state.currentSessionId = created.session_id
    }
    const payload = toQueryRequest(query, state.currentSessionId)
    assistant.request = payload
    await ensureSocket().send(payload)
  } catch (error) {
    failCurrent((error as Error).message)
  }
}

/**
 * 重新生成最后一轮：用**当时保存的检索参数**再问一次。
 *
 * 后端会话是追加式的，所以这会在历史里多记一轮 —— 界面上替换当前答案，
 * 刷新后能看到两条。这是后端限制，不在这里伪造"没多记"。
 */
export async function regenerateLast(): Promise<boolean> {
  if (state.generating) return false
  const assistantIndex = lastIndexOf('assistant')
  const userIndex = lastIndexOf('user')
  if (assistantIndex < 0 || userIndex < 0 || userIndex > assistantIndex) return false
  const assistant = state.messages[assistantIndex]
  const user = state.messages[userIndex]
  state.notice = ''
  assistant.text = ''
  assistant.citations = []
  assistant.channel = null
  assistant.refused = false
  assistant.truncated = false
  assistant.stopped = false
  assistant.error = null
  assistant.feedback = null
  assistant.createdAt = Date.now()
  assistant.streaming = true
  assistant.regenerated = true
  state.generating = true
  try {
    if (state.currentSessionId === null) {
      const created = await api.createSession()
      state.currentSessionId = created.session_id
    }
    const payload: BuiltQuery =
      assistant.request === null
        ? toQueryRequest(user.text, state.currentSessionId)
        : { ...assistant.request, session_id: state.currentSessionId }
    assistant.request = payload
    await ensureSocket().send(payload)
    return true
  } catch (error) {
    failCurrent((error as Error).message)
    return false
  }
}

/**
 * 给一轮回答打分（再点同一个按钮 = 撤销）。
 *
 * 两个刻意的处理：
 *
 * 1. **乐观更新 + 失败回滚**：按钮先变，写库失败再退回原状态并把原因写进 `notice` ——
 *    让用户对着一个转圈等 pymysql，比让他立刻看到结果更糟；
 * 2. **问题取自这条回答前面最近的那条用户消息**：中间件里没有"这一轮"的 id，
 *    而反馈的快照必须是**用户当时问的那句**（不是会话标题、也不是下一句）。
 */
export async function setFeedback(messageId: number, value: Feedback): Promise<void> {
  const index = state.messages.findIndex((item) => item.id === messageId)
  if (index < 0) return
  const message = state.messages[index]
  const sessionId = state.currentSessionId
  if (sessionId === null) {
    state.notice = '这段对话还没绑定会话，暂时不能反馈'
    return
  }
  const question = state.messages
    .slice(0, index)
    .reverse()
    .find((item) => item.role === 'user')
  if (question === undefined) return

  const previous = message.feedback
  const next = previous === value ? null : value
  message.feedback = next
  state.feedbackPending = messageId
  try {
    await api.submitFeedback({
      session_id: sessionId,
      question: question.text,
      answer: message.text,
      rating: next ?? 'none',
      channel: message.channel ?? '',
    })
  } catch (error) {
    message.feedback = previous
    state.notice = `反馈没记上：${(error as Error).message}`
  } finally {
    state.feedbackPending = null
  }
}

export function stop(): void {
  if (!state.generating) return
  socket?.close()
  const message = currentStreaming()
  if (message) {
    message.streaming = false
    message.stopped = true
  }
  state.generating = false
}

export function resetChat(): void {
  socket?.close()
  socket = null
  releaseAttachments(state.messages)
  state.sessions = []
  state.currentSessionId = null
  state.messages = []
  state.generating = false
  state.notice = ''
  state.sessionsError = ''
  closePanel()
}

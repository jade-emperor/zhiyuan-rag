/**
 * 会话与消息。
 *
 * **会话是"首次提问时才建"的**:点「New Chat」只清空本地,不调后端 ——
 * 否则左侧列表会堆满一堆 0 轮的空会话(后端那边也刻意只列有轮次的会话)。
 *
 * 帧的处理有一处刻意的选择:**`done` 帧里的 `answer` 是权威文本**,不是把 token 拼起来的
 * 结果。两者本应一致,但"本应"不是契约 —— 万一不一致,以服务端给的完整答案为准,
 * 排版与复制出来的内容才是后端真正生成的那份。
 */

import { reactive } from 'vue'

import { notifyUnauthorized } from '../api/client'
import * as api from '../api/endpoints'
import { ChatSocket } from '../api/stream'
import type { Channel, Citation, SessionSummary, StreamFrame } from '../api/types'
import { toQueryRequest } from './params'

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  text: string
  citations: Citation[]
  channel: Channel | null
  refused: boolean
  truncated: boolean
  /** 用户按了「停止生成」:与"服务端截断"是两回事,提示语也不同。 */
  stopped: boolean
  error: string | null
  /** 还在等这一轮结束(用于画光标与禁用输入)。 */
  streaming: boolean
}

const state = reactive({
  sessions: [] as SessionSummary[],
  currentSessionId: null as string | null,
  messages: [] as ChatMessage[],
  generating: false,
  loadingHistory: false,
  /** 会话列表相关的错误(与问答错误分开显示:一个是"列表拉不到",一个是"这轮答不了")。 */
  sessionsError: '',
  notice: '',
})

let counter = 0
let socket: ChatSocket | null = null

export function chatState() {
  return state
}

function push(message: Omit<ChatMessage, 'id'>): ChatMessage {
  const full: ChatMessage = { id: ++counter, ...message }
  state.messages.push(full)
  return full
}

function assistantMessage(): ChatMessage {
  return push({
    role: 'assistant',
    text: '',
    citations: [],
    channel: null,
    refused: false,
    truncated: false,
    stopped: false,
    error: null,
    streaming: true,
  })
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

export function newChat(): void {
  stop()
  state.currentSessionId = null
  state.messages = []
  state.notice = ''
}

export async function openSession(sessionId: string): Promise<void> {
  if (state.generating) stop()
  state.loadingHistory = true
  state.notice = ''
  try {
    const result = await api.history(sessionId)
    state.currentSessionId = sessionId
    state.messages = []
    for (const turn of result.rounds) {
      push({
        role: 'user',
        text: turn.question,
        citations: [],
        channel: null,
        refused: false,
        truncated: false,
        stopped: false,
        error: null,
        streaming: false,
      })
      push({
        role: 'assistant',
        text: turn.answer,
        // 引用跟着答案一起回来(当轮那张卡片的快照,存在会话表里)。
        // 老账没有这一段 → 空数组:那一轮的出处当时没留下,不编。
        citations: turn.citations ?? [],
        channel: null,
        refused: false,
        truncated: false,
        stopped: false,
        error: null,
        streaming: false,
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
  if (state.currentSessionId === sessionId) newChat()
  await refreshSessions()
}

export async function send(text: string): Promise<void> {
  const query = text.trim()
  if (query.length === 0 || state.generating) return
  state.notice = ''
  push({
    role: 'user',
    text: query,
    citations: [],
    channel: null,
    refused: false,
    truncated: false,
    stopped: false,
    error: null,
    streaming: false,
  })
  assistantMessage()
  state.generating = true
  try {
    if (state.currentSessionId === null) {
      // 归属在**创建那一刻**绑定:所以必须先建会话、再拿它的 ID 提问
      const created = await api.createSession()
      state.currentSessionId = created.session_id
    }
    await ensureSocket().send(toQueryRequest(query, state.currentSessionId))
  } catch (error) {
    failCurrent((error as Error).message)
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
  state.sessions = []
  state.currentSessionId = null
  state.messages = []
  state.generating = false
  state.notice = ''
  state.sessionsError = ''
}

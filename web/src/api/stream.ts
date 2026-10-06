/**
 * 流式问答的那条 WebSocket。
 *
 * 三条与后端对齐的事实(写在 `api/app.py` 的 `stream()` docstring 里,这里复述关键处):
 *
 * 1. 令牌只能走 **query**(`?token=`):浏览器不能给 WebSocket 设自定义头;
 * 2. 服务端会在**认证失败时先回一帧 `error` 再 close(1008)** —— 光看 close 分不清
 *    "令牌过期"和"服务挂了",所以这里两种都要处理:error 帧显示原因,1008 触发重新登录;
 * 3. 畸形请求体也回 `error` 帧但**不断连接** —— 所以收到 error 不等于连接坏了,不要重连。
 */

import { token, wsUrl } from './client'
import { STREAM_PATH } from './endpoints'
import type { StreamFrame } from './types'

export interface StreamHandlers {
  onFrame: (frame: StreamFrame) => void
  onOpen?: () => void
  onClose?: (event: CloseEvent) => void
}

/** 1008 = policy violation:后端用它表示"未认证"。 */
const POLICY_VIOLATION = 1008

export class ChatSocket {
  private socket: WebSocket | null = null
  private opening: Promise<void> | null = null
  private queue: string[] = []

  constructor(private readonly handlers: StreamHandlers) {}

  get isOpen(): boolean {
    return this.socket !== null && this.socket.readyState === WebSocket.OPEN
  }

  /** 确保连接可用(已开就直接返回)。失败会抛 —— 调用方要把这条错误显示给用户。 */
  async open(): Promise<void> {
    if (this.isOpen) return
    if (this.opening) return this.opening
    this.opening = new Promise<void>((resolve, reject) => {
      let socket: WebSocket
      try {
        socket = new WebSocket(wsUrl(STREAM_PATH, token()))
      } catch (error) {
        this.opening = null
        reject(error instanceof Error ? error : new Error(String(error)))
        return
      }
      let settled = false
      socket.onopen = () => {
        settled = true
        this.opening = null
        this.handlers.onOpen?.()
        this.flush()
        resolve()
      }
      socket.onmessage = (event: MessageEvent<string>) => {
        let frame: StreamFrame
        try {
          frame = JSON.parse(event.data) as StreamFrame
        } catch {
          // 服务端只发 JSON;真收到非 JSON 说明对面不是这个后端(常见于后端地址填成了别的服务)
          this.handlers.onFrame({
            type: 'error',
            detail: `收到不是 JSON 的帧:${String(event.data).slice(0, 120)}`,
          })
          return
        }
        this.handlers.onFrame(frame)
      }
      socket.onerror = () => {
        if (!settled) {
          settled = true
          this.opening = null
          reject(new Error('流式连接建立失败(后端地址或网络不通)'))
        }
      }
      socket.onclose = (event: CloseEvent) => {
        this.socket = null
        this.opening = null
        if (!settled) {
          settled = true
          reject(
            new Error(
              event.code === POLICY_VIOLATION
                ? '未认证:令牌无效或已过期,请重新登录'
                : `流式连接被关闭(code=${event.code})`,
            ),
          )
        }
        this.handlers.onClose?.(event)
      }
      this.socket = socket
    })
    return this.opening
  }

  /** 发一条提问。连接没开就先开(开了才发 —— 早期实现里 send 到 CONNECTING 的 socket 会抛)。 */
  async send(payload: unknown): Promise<void> {
    const text = JSON.stringify(payload)
    if (!this.isOpen) {
      await this.open()
    }
    if (this.isOpen) this.socket?.send(text)
    else this.queue.push(text)
  }

  /** 主动断开(「停止生成」用它:服务端会把这一轮记成 aborted)。 */
  close(): void {
    this.queue = []
    this.socket?.close(1000)
    this.socket = null
    this.opening = null
  }

  private flush(): void {
    const pending = this.queue
    this.queue = []
    for (const text of pending) this.socket?.send(text)
  }
}

/**
 * 布局与浮层状态：左侧栏折叠、移动端抽屉、右侧引用面板、参数抽屉、轻提示。
 *
 * 为什么单独一个 store：`MessageList`（点行内引用）、`ChatTopBar`（开关面板）、
 * `Composer`（低置信度时请求聚焦）都要改同一份界面状态，用 props 逐层传会变成
 * 一条谁都记不全的链。这里只放**界面状态**，不放业务数据（会话与消息在 chat store）。
 */

import { computed, reactive } from 'vue'

import type { Citation } from '../api/types'

export interface SelectedCitation {
  messageId: number
  citation: Citation
}

const SIDEBAR_KEY = 'project-rag.sidebar-collapsed'

type ToastTone = 'info' | 'success' | 'error'

const state = reactive({
  /** 桌面端：侧栏折叠成 0 宽；移动端：抽屉是否打开。 */
  sidebarCollapsed: localStorage.getItem(SIDEBAR_KEY) === '1',
  drawerOpen: false,
  /** 右侧引用面板。 */
  panelOpen: false,
  selected: null as SelectedCitation | null,
  /** 检索设置抽屉。 */
  settingsOpen: false,
  /** 1440 三栏；≤1180 侧栏与右栏改为浮层；≤768 侧栏抽屉 + 右栏底部弹层。 */
  compact: false,
  mobile: false,
  toast: '',
  toastTone: 'info' as ToastTone,
})

let toastTimer: number | null = null
let mediaCompact: MediaQueryList | null = null
let mediaMobile: MediaQueryList | null = null

export function uiState() {
  return state
}

export const toastActive = computed(() => state.toast.length > 0)

/** 1440 以下不给主区再被两栏挤：侧栏收起、右栏浮层。 */
function syncMedia(): void {
  state.compact = mediaCompact?.matches ?? false
  state.mobile = mediaMobile?.matches ?? false
  if (!state.compact) state.drawerOpen = false
}

/** 在应用挂载时调用一次（main.ts）。 */
export function initUi(): void {
  mediaCompact = window.matchMedia('(max-width: 1180px)')
  mediaMobile = window.matchMedia('(max-width: 768px)')
  syncMedia()
  mediaCompact.addEventListener('change', syncMedia)
  mediaMobile.addEventListener('change', syncMedia)
}

export function toggleSidebar(): void {
  if (state.compact) {
    state.drawerOpen = !state.drawerOpen
    return
  }
  state.sidebarCollapsed = !state.sidebarCollapsed
  localStorage.setItem(SIDEBAR_KEY, state.sidebarCollapsed ? '1' : '0')
}

export function closeDrawer(): void {
  state.drawerOpen = false
}

export function openPanel(): void {
  state.panelOpen = true
  state.settingsOpen = false
  state.drawerOpen = false
}

export function closePanel(): void {
  state.panelOpen = false
  state.selected = null
}

export function togglePanel(): void {
  if (state.panelOpen) closePanel()
  else openPanel()
}

/** 点行内引用 / 引用卡片的「查看原文」：选中并打开右栏。 */
export function selectCitation(messageId: number, citation: Citation): void {
  state.selected = { messageId, citation }
  state.panelOpen = true
  state.settingsOpen = false
  state.drawerOpen = false
}

export function openSettings(): void {
  state.settingsOpen = true
  state.panelOpen = false
  state.drawerOpen = false
}

export function closeSettings(): void {
  state.settingsOpen = false
}

export function notify(message: string, tone: ToastTone = 'info'): void {
  state.toast = message
  state.toastTone = tone
  if (toastTimer !== null) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => {
    state.toast = ''
    toastTimer = null
  }, 2600)
}

/** 浏览器剪贴板不可用（非 HTTPS / 旧内核）时的兜底：execCommand 只用于这一处。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }
}

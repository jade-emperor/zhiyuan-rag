<script setup lang="ts">
/**
 * 聊天工作台：三栏 + 两档响应式。
 *
 * 1440px：侧栏 260 / 主区自适应 / 引用面板 320（可关）。
 * ≤1180px：侧栏与引用面板改成覆盖式抽屉（主区不被挤成一条缝）。
 * ≤768px：侧栏是左侧抽屉，引用面板是底部弹层，参数是底部抽屉。
 *
 * 这里只管**布局与开关**；消息、引用、参数各自在自己的组件里。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ChatSidebar from '../components/ChatSidebar.vue'
import ChatTopBar from '../components/ChatTopBar.vue'
import CitationPanel from '../components/CitationPanel.vue'
import Composer from '../components/Composer.vue'
import ConfigPanel from '../components/ConfigPanel.vue'
import MessageList from '../components/MessageList.vue'
import { chatState } from '../stores/chat'
import { capabilitiesState, loadCapabilities } from '../stores/capabilities'
import { loadSources } from '../stores/sources'
import { closeDrawer, closePanel, closeSettings, uiState } from '../stores/ui'
import Icon from '../components/Icon.vue'

const chat = chatState()
const caps = capabilitiesState()
const ui = uiState()
const composer = ref<InstanceType<typeof Composer> | null>(null)

function focusComposer(): void {
  if (ui.compact) closeDrawer()
  composer.value?.focus()
}

function useSuggestion(text: string): void {
  composer.value?.setDraft(text)
  if (ui.compact) closeDrawer()
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  if (ui.settingsOpen) closeSettings()
  else if (ui.panelOpen) closePanel()
  else if (ui.drawerOpen) closeDrawer()
}

watch(
  () => chat.messages.length,
  (length) => {
    if (length === 0) closePanel()
  },
)

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  if (caps.data === null) await loadCapabilities()
  // 侧栏与参数面板共用同一份来源清单；这里显式拉一次，失败由组件各自显示
  await loadSources()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="workspace">
    <div v-if="!ui.compact && !ui.sidebarCollapsed" class="sidebar-pane">
      <ChatSidebar />
    </div>

    <template v-if="ui.compact">
      <div v-if="ui.drawerOpen" class="backdrop" @click="closeDrawer"></div>
      <div class="drawer-left" :class="{ open: ui.drawerOpen }" :aria-hidden="!ui.drawerOpen" :inert="!ui.drawerOpen">
        <ChatSidebar />
      </div>
    </template>

    <main class="main">
      <ChatTopBar />
      <MessageList
        :messages="chat.messages"
        @focus-composer="focusComposer"
        @suggest="useSuggestion"
      />
      <Composer ref="composer" />
    </main>

    <div v-if="!ui.compact && ui.panelOpen" class="panel-pane">
      <CitationPanel />
    </div>

    <template v-if="ui.compact">
      <div v-if="ui.panelOpen" class="backdrop" @click="closePanel"></div>
      <div
        class="panel-overlay"
        :class="[ui.mobile ? 'sheet' : 'drawer-right', { open: ui.panelOpen }]"
        :aria-hidden="!ui.panelOpen" :inert="!ui.panelOpen"
      >
        <CitationPanel />
      </div>
    </template>

    <template v-if="ui.settingsOpen">
      <div class="backdrop" @click="closeSettings"></div>
      <div class="settings-overlay" :class="{ open: ui.settingsOpen }" role="dialog" aria-label="检索设置">
        <header class="settings-head">
          <div>
            <h2>检索偏好</h2>
            <p>调整检索方式与回答依据的范围。</p>
          </div>
          <button
            class="icon-btn"
            type="button"
            title="关闭设置"
            aria-label="关闭设置"
            @click="closeSettings"
          >
            <Icon name="close" :size="16" />
          </button>
        </header>
        <div class="settings-body">
          <ConfigPanel embedded />
        </div>
      </div>
    </template>

    <Transition name="toast">
      <div v-if="ui.toast" class="toast" :class="ui.toastTone" role="status">
        {{ ui.toast }}
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.workspace {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
  background: var(--bg-muted);
}

.sidebar-pane {
  width: var(--sidebar);
  flex: none;
  min-height: 0;
}

.main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(840px 360px at 70% 0%, color-mix(in srgb, var(--accent) 4%, transparent), transparent 70%),
    var(--bg);
  border-left: 1px solid var(--border);
}

.panel-pane {
  width: var(--panel);
  flex: none;
  min-height: 0;
}

/* ── 覆盖式抽屉 ────────────────────────────────────────────────────── */
.backdrop {
  position: fixed;
  inset: 0;
  z-index: var(--z-drawer);
  background: var(--overlay);
  animation: fade 0.16s ease;
}

@keyframes fade {
  from {
    opacity: 0;
  }
}

.drawer-left {
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  width: min(300px, 86vw);
  z-index: calc(var(--z-drawer) + 1);
  transform: translateX(-102%);
  transition: transform 0.18s ease;
  box-shadow: var(--shadow-lg);
}

.drawer-left.open {
  transform: translateX(0);
}

.panel-overlay {
  position: fixed;
  z-index: calc(var(--z-drawer) + 1);
  background: var(--bg);
  box-shadow: var(--shadow-lg);
  transition: transform 0.18s ease;
  display: flex;
  flex-direction: column;
}

.drawer-right {
  top: 0;
  right: 0;
  bottom: 0;
  width: min(360px, 92vw);
  transform: translateX(102%);
}

.drawer-right.open {
  transform: translateX(0);
}

.sheet {
  left: 0;
  right: 0;
  bottom: 0;
  height: min(78vh, 640px);
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
  transform: translateY(102%);
}

.sheet.open {
  transform: translateY(0);
}

.settings-overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: calc(var(--z-drawer) + 1);
  width: min(380px, 94vw);
  background: var(--bg);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  transform: translateX(102%);
  transition: transform 0.18s ease;
}

.settings-overlay.open {
  transform: translateX(0);
}

.settings-head {
  flex: none;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
}

.settings-head h2 {
  font-size: 14px;
}

.settings-head p {
  margin: 3px 0 0;
  color: var(--text-faint);
  font-size: 11px;
}

.settings-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px 20px;
}

.toast {
  position: fixed;
  left: 50%;
  top: calc(var(--header-h) + var(--topbar-h) + 10px);
  transform: translateX(-50%);
  z-index: var(--z-toast);
  padding: 8px 14px;
  border-radius: var(--radius-full);
  background: var(--text);
  color: var(--bg);
  font-size: 12px;
  box-shadow: var(--shadow-lg);
  max-width: min(92vw, 420px);
  text-align: center;
}

.toast.error {
  background: var(--danger);
  color: #fff;
}

.toast.success {
  background: var(--success);
  color: #fff;
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translate(-50%, 6px);
}

@media (max-width: 768px) {
  .settings-overlay {
    top: auto;
    left: 0;
    right: 0;
    bottom: 0;
    width: 100%;
    height: min(86vh, 720px);
    border-left: none;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    transform: translateY(102%);
  }

  .settings-overlay.open {
    transform: translateY(0);
  }
}
</style>

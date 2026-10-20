<script setup lang="ts">
/**
 * 对话历史：按时间分组、可搜索、可重命名、可删除。
 *
 * 两条与后端对齐的边界，界面必须如实表达：
 * 1. **重命名只存本机**（localStorage）：后端没有 rename 接口。所以 tooltip 与提示都写
 *    「本机名称」，不假装它改到了服务端；
 * 2. **删除 = 清空这段会话的历史**（`DELETE /history/{id}`），不可恢复 ——
 *    用行内二次确认，不用 `window.confirm`。
 */
import { computed, ref } from 'vue'

import { humanTime } from '../utils/format'
import type { SessionSummary } from '../api/types'
import {
  chatState,
  groupedSessions,
  openSession,
  refreshSessions,
  removeSession,
  renameSession,
  sessionTitle,
} from '../stores/chat'
import { closeDrawer, notify, uiState } from '../stores/ui'
import Icon from './Icon.vue'

const props = defineProps<{ query: string }>()

const chat = chatState()
const ui = uiState()

const refreshing = ref(false)
const editingId = ref<string | null>(null)
const draftTitle = ref('')
const confirmingId = ref<string | null>(null)
const busyId = ref<string | null>(null)

const groups = computed(() => groupedSessions(props.query))
const searching = computed(() => props.query.trim().length > 0)

async function reload(): Promise<void> {
  refreshing.value = true
  try {
    await refreshSessions()
  } finally {
    refreshing.value = false
  }
}

void reload()

async function pick(item: SessionSummary): Promise<void> {
  if (item.session_id === chat.currentSessionId) {
    if (ui.compact) closeDrawer()
    return
  }
  await openSession(item.session_id)
  if (ui.compact) closeDrawer()
}

function startRename(item: SessionSummary): void {
  confirmingId.value = null
  editingId.value = item.session_id
  draftTitle.value = sessionTitle(item)
}

function commitRename(): void {
  if (editingId.value === null) return
  renameSession(editingId.value, draftTitle.value)
  editingId.value = null
  notify('已重命名（仅保存在本机）', 'success')
}

function cancelRename(): void {
  editingId.value = null
  draftTitle.value = ''
}

function askRemove(item: SessionSummary): void {
  editingId.value = null
  confirmingId.value = item.session_id
}

async function confirmRemove(sessionId: string): Promise<void> {
  busyId.value = sessionId
  const before = chat.currentSessionId
  await removeSession(sessionId)
  busyId.value = null
  confirmingId.value = null
  if (chat.sessionsError.length === 0) {
    notify(before === sessionId ? '会话已清空' : '会话已删除', 'success')
  }
}
</script>

<template>
  <div class="history">
    <div class="history-head">
      <span class="label">对话</span>
      <button
        class="icon-btn"
        type="button"
        title="刷新对话列表"
        aria-label="刷新对话列表"
        :disabled="refreshing"
        @click="reload"
      >
        <Icon name="refresh" :size="14" />
      </button>
    </div>

    <div v-if="refreshing && chat.sessions.length === 0" class="skeletons" aria-hidden="true">
      <div v-for="index in 3" :key="index" class="skeleton row-skeleton"></div>
    </div>

    <p v-else-if="chat.sessionsError" class="alert error small">
      {{ chat.sessionsError }}
      <button class="ghost retry" type="button" @click="reload">重试</button>
    </p>

    <p v-else-if="groups.length === 0" class="empty">
      <Icon :name="searching ? 'search' : 'message'" :size="18" />
      <span>{{ searching ? '没有匹配的对话' : '暂无对话' }}</span>
    </p>

    <nav v-else class="groups" aria-label="对话历史">
      <section v-for="group in groups" :key="group.label" class="group">
        <h4 class="group-label">{{ group.label }}</h4>
        <div
          v-for="item in group.items"
          :key="item.session_id"
          class="item"
          :class="{ active: item.session_id === chat.currentSessionId }"
        >
          <template v-if="editingId === item.session_id">
            <form class="rename" @submit.prevent="commitRename">
              <input
                v-model="draftTitle"
                type="text"
                maxlength="80"
                autofocus
                aria-label="会话名称"
                @keydown.esc="cancelRename"
              />
              <button class="icon-btn" type="submit" title="保存名称" aria-label="保存名称">
                <Icon name="check" :size="14" />
              </button>
              <button
                class="icon-btn"
                type="button"
                title="取消"
                aria-label="取消重命名"
                @click="cancelRename"
              >
                <Icon name="close" :size="14" />
              </button>
            </form>
          </template>

          <template v-else>
            <button class="item-main" type="button" @click="pick(item)">
              <span class="title" :title="sessionTitle(item)">{{ sessionTitle(item) }}</span>
              <span class="meta">{{ item.rounds }} 轮 · {{ humanTime(item.updated_at) }}</span>
            </button>
            <div class="actions">
              <button
                class="icon-btn"
                type="button"
                title="重命名（仅保存在本机）"
                aria-label="重命名"
                @click.stop="startRename(item)"
              >
                <Icon name="edit" :size="14" />
              </button>
              <button
                class="icon-btn danger-hover"
                type="button"
                title="删除这段会话（清空后端历史，不可恢复）"
                aria-label="删除会话"
                @click.stop="askRemove(item)"
              >
                <Icon name="trash" :size="14" />
              </button>
            </div>
          </template>

          <div v-if="confirmingId === item.session_id" class="confirm">
            <p>删除「{{ sessionTitle(item) }}」？</p>
            <div class="confirm-actions">
              <button
                class="danger solid"
                type="button"
                :disabled="busyId === item.session_id"
                @click.stop="confirmRemove(item.session_id)"
              >
                {{ busyId === item.session_id ? '删除中…' : '删除' }}
              </button>
              <button class="ghost" type="button" @click.stop="confirmingId = null">取消</button>
            </div>
          </div>
        </div>
      </section>
    </nav>

  </div>
</template>

<style scoped>
.history {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: hidden;
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 8px 4px 10px;
}

.label {
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--text-faint);
  text-transform: uppercase;
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
}

.row-skeleton {
  height: 44px;
}

.groups {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.group-label {
  margin: 10px 6px 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-faint);
}

.item {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: var(--radius-sm);
  padding: 2px;
}

.item:hover,
.item:focus-within {
  background: var(--bg-hover);
}

.item.active {
  background: var(--accent-soft);
}

.item.active .title {
  color: var(--accent);
  font-weight: 600;
}

.item-main {
  flex: 1;
  min-width: 0;
  height: auto;
  padding: 6px 8px;
  border: none;
  background: transparent;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  text-align: left;
  border-radius: var(--radius-sm);
}

.item-main:hover:not(:disabled) {
  background: transparent;
}

.title {
  max-width: 100%;
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  font-size: 11px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.actions {
  display: flex;
  gap: 0;
  padding-right: 2px;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.item:hover .actions,
.item:focus-within .actions {
  opacity: 1;
}

.danger-hover:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-soft);
}

.rename {
  display: flex;
  align-items: center;
  gap: 2px;
  width: 100%;
  padding: 2px;
}

.rename input {
  height: 28px;
  font-size: 12.5px;
}

.confirm {
  position: absolute;
  left: 4px;
  right: 4px;
  top: calc(100% + 4px);
  z-index: var(--z-sticky);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.confirm p {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.45;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.confirm-actions button {
  height: 26px;
  font-size: 12px;
}

.empty {
  margin: 12px 8px;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--text-faint);
  font-size: 12.5px;
  line-height: 1.55;
}

.small {
  font-size: 12px;
}

.retry {
  margin-left: 4px;
  height: 22px;
  padding: 0 6px;
  font-size: 11px;
}

.storage-hint {
  margin: 0;
  padding: 8px 10px;
  border-top: 1px solid var(--border-subtle);
  color: var(--text-faint);
  font-size: 11px;
}
</style>

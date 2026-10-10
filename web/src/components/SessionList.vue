<script setup lang="ts">
/**
 * 左侧会话列表。
 *
 * 「+ 新会话」**只清空本地**,不调后端:会话是首次提问时才创建的 ——
 * 否则每点一次就多一个 0 轮的空会话(后端也只列有轮次的会话,两边口径一致)。
 */
import { onMounted } from 'vue'

import { chatState, newChat, openSession, refreshSessions, removeSession } from '../stores/chat'

const chat = chatState()

onMounted(() => {
  void refreshSessions()
})

function stamp(value: string): string {
  // 后端给的是 "YYYY-MM-DDThh:mm:ss"(本机时间,不带时区):只显示到分钟就够了
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(
    date.getMinutes(),
  ).padStart(2, '0')}`
}

async function confirmRemove(sessionId: string, title: string): Promise<void> {
  const label = title || '这段会话'
  if (!window.confirm(`删除「${label}」?清空后不可恢复。`)) return
  await removeSession(sessionId)
}
</script>

<template>
  <aside class="sidebar">
    <button class="primary new" type="button" @click="newChat">+ 新会话</button>

    <p v-if="chat.sessionsError" class="alert error small">{{ chat.sessionsError }}</p>

    <div class="list">
      <p v-if="chat.sessions.length === 0 && !chat.sessionsError" class="faint empty">
        还没有会话。提问一次就会出现在这里。
      </p>
      <div
        v-for="item in chat.sessions"
        :key="item.session_id"
        class="item"
        :class="{ active: item.session_id === chat.currentSessionId }"
        @click="openSession(item.session_id)"
      >
        <div class="item-main">
          <div class="title">{{ item.title || '新会话' }}</div>
          <div class="faint">{{ item.rounds }} 轮 · {{ stamp(item.updated_at) }}</div>
        </div>
        <button
          class="ghost remove"
          type="button"
          title="删除这段会话"
          @click.stop="confirmRemove(item.session_id, item.title)"
        >
          ×
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar);
  flex: none;
  border-right: 1px solid var(--border);
  background: var(--bg);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}

.new {
  width: 100%;
}

.list {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
}

.empty {
  padding: 8px 4px;
}

.item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.item:hover {
  background: var(--bg-muted);
}

.item.active {
  background: var(--bg-sunken);
}

.item-main {
  flex: 1;
  min-width: 0;
}

.title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.remove {
  color: var(--text-faint);
  padding: 2px 8px;
  font-size: 16px;
  line-height: 1;
}

.remove:hover {
  color: var(--danger);
  background: var(--danger-soft);
}

.small {
  font-size: 12px;
}
</style>

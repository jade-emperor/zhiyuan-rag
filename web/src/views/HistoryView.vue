<script setup lang="ts">
/**
 * 会话历史：左边是数据库里的会话目录，右边是选中那段会话的全部轮次。
 *
 * 三条口径：
 *
 * 1. **谁能看什么由后端定**：员工只能列自己的（`scope=mine`），管理员可以切到
 *    `scope=all` 看全部并带上归属人；正文能不能读是 `check_owner` 判的 ——
 *    前端不拿 `mine` 当权限用；
 * 2. **正文按轮次渲染，引用跟着那一轮**：历史里存的是**当轮**的引用快照，
 *    不是"现在重新检索一遍"的结果（老账没有引用时照实说"出处没留下"）；
 * 3. **删除 = 清空这段会话的历史**，不可恢复 —— 行内二次确认，不用 `window.confirm`。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { HistoryTurn, SessionSummary } from '../api/types'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import PageShell from '../components/ui/PageShell.vue'
import { isAdmin } from '../stores/auth'
import { openSession } from '../stores/chat'
import {
  clearSession,
  historyState,
  loadDetail,
  loadSessions,
  setScope,
} from '../stores/history'
import { humanTime } from '../utils/format'
import { renderMarkdown } from '../utils/markdown'

const route = useRoute()
const router = useRouter()
const history = historyState()

const query = ref('')
const confirming = ref(false)

const scopeToggleVisible = computed(() => isAdmin.value)

const visible = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return history.sessions
  return history.sessions.filter(
    (item) =>
      item.title.toLowerCase().includes(needle) ||
      item.owner.toLowerCase().includes(needle) ||
      item.session_id.toLowerCase().includes(needle),
  )
})

const selected = computed(
  () => history.sessions.find((item) => item.session_id === history.selected) ?? null,
)

function label(item: SessionSummary): string {
  return item.title.trim() || '新会话'
}

function body(turn: HistoryTurn): string {
  return renderMarkdown(turn.answer, { citations: turn.citations }).html
}

function pageText(page: number | null): string {
  return page === null ? '页码未知' : `第 ${page} 页`
}

async function pick(item: SessionSummary): Promise<void> {
  confirming.value = false
  await loadDetail(item.session_id)
  void router.replace({ name: 'history', params: { session: item.session_id } })
}

async function remove(): Promise<void> {
  if (history.selected === null) return
  const ok = await clearSession(history.selected)
  if (ok) {
    confirming.value = false
    void router.replace({ name: 'history', params: {} })
  }
}

async function resume(): Promise<void> {
  if (history.selected === null) return
  await openSession(history.selected)
  void router.push({ name: 'chat' })
}

async function changeScope(scope: 'mine' | 'all'): Promise<void> {
  confirming.value = false
  await setScope(scope)
}

onMounted(async () => {
  await loadSessions(isAdmin.value ? 'all' : 'mine')
  const wanted = typeof route.params.session === 'string' ? route.params.session : ''
  if (wanted && history.sessions.some((item) => item.session_id === wanted)) {
    await loadDetail(wanted)
  }
})

// 浏览器前进/后退会改 URL 而对到同一页（组件不重挂）：跟着 URL 换正文。
watch(
  () => route.params.session,
  (value) => {
    const wanted = typeof value === 'string' ? value : ''
    if (wanted && wanted !== history.selected) void loadDetail(wanted)
  },
)
</script>

<template>
  <PageShell
    title="会话历史"
    :meta="`${history.sessions.length} 段`"
    description="数据库里的问答记录。员工只看自己的；管理员可以切换到「全部」并看到归属人，用来复盘拒答、改写与引用是否正常。"
  >
    <template #actions>
      <button class="ghost" type="button" :disabled="history.sessionsLoading" @click="loadSessions()">
        <Icon name="refresh" :size="15" />
        刷新
      </button>
    </template>

    <div class="layout">
      <aside class="card pane list-pane">
        <div class="pane-toolbar">
          <label class="search">
            <Icon name="search" :size="14" />
            <input v-model="query" type="search" placeholder="搜索标题、归属人或会话 ID" aria-label="搜索会话" />
          </label>
          <div v-if="scopeToggleVisible" class="seg" role="group" aria-label="会话范围">
            <button
              type="button"
              :class="{ on: history.scope === 'mine' }"
              :disabled="history.sessionsLoading"
              @click="changeScope('mine')"
            >
              我的
            </button>
            <button
              type="button"
              :class="{ on: history.scope === 'all' }"
              :disabled="history.sessionsLoading"
              @click="changeScope('all')"
            >
              全部
            </button>
          </div>
        </div>

        <p v-if="history.sessionsError" class="alert error">
          {{ history.sessionsError }}
          <button class="ghost retry" type="button" @click="loadSessions()">重试</button>
        </p>

        <div v-else-if="history.sessionsLoading" class="skeletons" aria-hidden="true">
          <div v-for="index in 4" :key="index" class="skeleton row-skeleton"></div>
        </div>

        <EmptyState
          v-else-if="visible.length === 0"
          :title="query ? '没有匹配的会话' : '还没有会话'"
          :hint="
            query
              ? '换个词试试，或者清空搜索。'
              : history.scope === 'all'
                ? '还没有任何账号问过问题。'
                : '去「对话」里问第一个问题，这里就会出现。'
          "
        />

        <nav v-else class="session-list" aria-label="会话列表">
          <button
            v-for="item in visible"
            :key="item.session_id"
            type="button"
            class="session"
            :class="{ active: item.session_id === history.selected }"
            @click="pick(item)"
          >
            <span class="session-title">{{ label(item) }}</span>
            <span class="session-meta">
              <span v-if="history.scope === 'all'" class="owner" :class="{ mine: item.mine }">
                {{ item.mine ? '我' : item.owner || '账号已删除' }}
              </span>
              <span>{{ item.rounds }} 轮</span>
              <span>·</span>
              <span>{{ humanTime(item.updated_at) }}</span>
            </span>
          </button>
        </nav>
      </aside>

      <section class="card pane detail-pane">
        <template v-if="selected">
          <header class="detail-head">
            <div class="detail-title">
              <h2>{{ label(selected) }}</h2>
              <p class="faint">
                {{ selected.rounds }} 轮 · 建于 {{ humanTime(selected.created_at) }} · 最近
                {{ humanTime(selected.updated_at) }}
                <template v-if="history.scope === 'all'">
                  · 归属 {{ selected.mine ? '我' : selected.owner || '账号已删除' }}
                </template>
              </p>
            </div>
            <div class="detail-actions">
              <button class="ghost" type="button" @click="resume">
                <Icon name="message" :size="14" />
                继续对话
              </button>
              <button class="ghost danger-hover" type="button" @click="confirming = true">
                <Icon name="trash" :size="14" />
                清空
              </button>
            </div>
          </header>

          <div v-if="confirming" class="confirm">
            <p>清空「{{ label(selected) }}」的全部问答？这一步不可恢复。</p>
            <div class="confirm-actions">
              <button class="ghost" type="button" @click="confirming = false">取消</button>
              <button
                class="danger solid"
                type="button"
                :disabled="history.busy === selected.session_id"
                @click="remove"
              >
                {{ history.busy === selected.session_id ? '清空中…' : '清空' }}
              </button>
            </div>
          </div>

          <p v-if="history.detailError" class="alert error">{{ history.detailError }}</p>

          <div v-if="history.detailLoading" class="skeletons" aria-hidden="true">
            <div v-for="index in 3" :key="index" class="skeleton block-skeleton"></div>
          </div>

          <EmptyState
            v-else-if="history.rounds.length === 0"
            title="这段会话里还没有问答"
            hint="可能是刚建好还没提问，或者历史被清空过。"
          />

          <ol v-else class="turns">
            <li v-for="(turn, index) in history.rounds" :key="index" class="turn">
              <p class="question">
                <span class="mark">Q</span>
                <span>{{ turn.question }}</span>
              </p>
              <div class="answer">
                <span class="mark">A</span>
                <div class="answer-body md" v-html="body(turn)"></div>
              </div>
              <div v-if="turn.citations.length > 0" class="citations">
                <span class="cite-label"><Icon name="quote" :size="12" /> 出处</span>
                <span
                  v-for="citation in turn.citations"
                  :key="citation.index"
                  class="cite"
                  :title="citation.preview"
                >
                  [{{ citation.index }}] {{ citation.source }} · {{ pageText(citation.page) }}
                </span>
              </div>
              <p v-else class="citations missing">这一轮的出处没有留下（早于引用快照那次改动的老账）</p>
            </li>
          </ol>
        </template>

        <EmptyState
          v-else
          title="选一段会话"
          hint="左边列的是数据库里的会话目录；点一段就能看到当时的问答与引用。"
        />
      </section>
    </div>
  </PageShell>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: minmax(260px, 340px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0;
  overflow: hidden;
}

.list-pane {
  position: sticky;
  top: calc(var(--header-h) + 14px);
  max-height: calc(100vh - var(--header-h) - 96px);
}

.pane-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-bottom: 1px solid var(--border-subtle);
}

.search {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  height: 30px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-faint);
}

.search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-ring);
}

.search input {
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 12.5px;
}

.search input:focus {
  outline: none;
  box-shadow: none;
}

.seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  flex: none;
}

.seg button {
  height: 28px;
  padding: 0 9px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
}

.seg button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}

.session-list {
  overflow-y: auto;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.session {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 8px 10px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
  height: auto;
}

.session:hover:not(:disabled) {
  background: var(--bg-hover);
}

.session.active {
  background: var(--accent-soft);
}

.session.active .session-title {
  color: var(--accent);
}

.session-title {
  max-width: 100%;
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.owner {
  padding: 0 5px;
  border-radius: 4px;
  background: var(--bg-sunken);
  color: var(--text-muted);
}

.owner.mine {
  background: var(--accent-soft);
  color: var(--accent);
}

.detail-pane {
  min-height: 320px;
  padding: 0;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-subtle);
}

.detail-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  overflow-wrap: anywhere;
}

.detail-title p {
  margin: 4px 0 0;
  font-size: 11.5px;
}

.detail-actions {
  display: flex;
  gap: 6px;
  flex: none;
}

.danger-hover:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-soft);
}

.confirm {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 12px 16px 0;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}

.confirm p {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}

.confirm-actions {
  display: flex;
  gap: 6px;
  flex: none;
}

.confirm-actions button {
  height: 26px;
  font-size: 12px;
}

.turns {
  margin: 0;
  padding: 6px 16px 18px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.turn {
  padding: 14px 0;
  border-bottom: 1px solid var(--border-subtle);
}

.turn:last-child {
  border-bottom: none;
}

.question {
  display: flex;
  gap: 9px;
  margin: 0 0 10px;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.55;
  color: var(--text);
  overflow-wrap: anywhere;
}

.answer {
  display: flex;
  gap: 9px;
}

.answer-body {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text);
}

.mark {
  flex: none;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  background: var(--bg-sunken);
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
}

.answer .mark {
  margin-top: 2px;
  background: var(--accent-soft);
  color: var(--accent);
}

.citations {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 8px 0 0 27px;
  font-size: 11px;
  color: var(--text-faint);
}

.cite-label {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.cite {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-sunken);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10.5px;
  cursor: default;
}

.citations.missing {
  margin-left: 27px;
  font-size: 11px;
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
}

.row-skeleton {
  height: 46px;
}

.block-skeleton {
  height: 110px;
}

.retry {
  margin-left: 6px;
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .list-pane {
    position: static;
    max-height: 320px;
  }
}
</style>

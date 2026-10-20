<script setup lang="ts">
/**
 * FAQ 管理台：增 / 改 / 删「命中即直答」的问答对。
 *
 * 三件刻意的事：
 *
 * 1. **搜索走服务端**（`?query=`）：答案正文可能上千字，把全量搬给浏览器只为过滤一次
 *    不划算；`total` 是过滤前的总数，所以"共 150 条 / 筛出 6 条"两个数都是准的；
 * 2. **删除用行内二次确认**，不用 `window.confirm` —— 它会把整页冻住，而且样式不可控；
 * 3. **改完立刻按当前搜索词重拉**：本地改一行不会让"筛出几条"变，让后端重算一遍，
 *    列表和计数才不会各说一套。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import FaqDialog from '../components/FaqDialog.vue'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import PageShell from '../components/ui/PageShell.vue'
import type { FaqEntry } from '../api/types'
import { deleteFaq, faqState, refreshFaq } from '../stores/faq'

const PAGE_SIZE = 20

const faq = faqState()

const query = ref('')
const page = ref(1)
const expanded = ref<number | null>(null)
const confirmingId = ref<number | null>(null)
const dialog = ref<{ entry: FaqEntry | null } | null>(null)

let searchTimer: number | undefined

const pageCount = computed(() => Math.max(1, Math.ceil(faq.entries.length / PAGE_SIZE)))

const visible = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return faq.entries.slice(start, start + PAGE_SIZE)
})

const filtered = computed(() => query.value.trim().length > 0)

function search(): void {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    page.value = 1
    expanded.value = null
    void refreshFaq(query.value)
  }, 250)
}

function clearSearch(): void {
  window.clearTimeout(searchTimer)
  query.value = ''
  page.value = 1
  void refreshFaq('')
}

function preview(entry: FaqEntry): string {
  return entry.answer.replace(/\s+/g, ' ').trim()
}

function isLong(entry: FaqEntry): boolean {
  return preview(entry).length > 140 || entry.answer.includes('\n')
}

async function remove(entryId: number): Promise<void> {
  const ok = await deleteFaq(entryId)
  if (ok) confirmingId.value = null
  if (faq.entries.length === 0 && page.value > 1) page.value -= 1
}

onMounted(() => {
  void refreshFaq('')
})
onUnmounted(() => window.clearTimeout(searchTimer))
</script>

<template>
  <PageShell
    title="FAQ 管理"
    :meta="`共 ${faq.total} 条`"
    description="命中即直答的标准答案，改完下一次提问就生效。FAQ 只答近乎一字不差的问法，改写过的提问仍走知识库检索。"
  >
    <template #actions>
      <button class="primary" type="button" @click="dialog = { entry: null }">
        <Icon name="plus" :size="15" />
        新建问答对
      </button>
    </template>

    <section class="card panel">
      <div class="toolbar">
        <label class="search">
          <Icon name="search" :size="15" />
          <input
            v-model="query"
            type="search"
            placeholder="搜索问题或答案"
            aria-label="搜索问答对"
            @input="search"
          />
        </label>
        <span class="count">
          <template v-if="filtered">筛出 {{ faq.entries.length }} 条</template>
          <template v-else>按 ID 升序</template>
        </span>
      </div>

      <p v-if="faq.error" class="alert error">
        {{ faq.error }}
        <button class="ghost retry" type="button" @click="refreshFaq(query)">重试</button>
      </p>

      <div v-if="faq.loading && !faq.loaded" class="skeletons" aria-hidden="true">
        <div v-for="index in 3" :key="index" class="skeleton row-skeleton"></div>
      </div>

      <EmptyState
        v-else-if="faq.entries.length === 0"
        :title="filtered ? '没有匹配的问答对' : '还没有问答对'"
        :hint="
          filtered
            ? '换个词试试，或者清空搜索看全部。'
            : '新建第一条：问题写用户的原话，答案写标准口径。'
        "
      >
        <button v-if="filtered" class="ghost" type="button" @click="clearSearch">清空搜索</button>
        <button v-else class="primary" type="button" @click="dialog = { entry: null }">
          新建问答对
        </button>
      </EmptyState>

      <ol v-else class="list">
        <li v-for="entry in visible" :key="entry.id" class="row">
          <div class="row-head">
            <span class="id">#{{ entry.id }}</span>
            <p class="question">{{ entry.question }}</p>
            <div class="row-actions">
              <button
                class="icon-btn"
                type="button"
                title="编辑"
                aria-label="编辑"
                @click="dialog = { entry }"
              >
                <Icon name="edit" :size="14" />
              </button>
              <button
                class="icon-btn danger-hover"
                type="button"
                title="删除"
                aria-label="删除"
                @click="confirmingId = entry.id"
              >
                <Icon name="trash" :size="14" />
              </button>
            </div>
          </div>

          <p class="answer" :class="{ expanded: expanded === entry.id }">
            {{ preview(entry) }}
          </p>
          <button
            v-if="isLong(entry)"
            class="toggle"
            type="button"
            @click="expanded = expanded === entry.id ? null : entry.id"
          >
            {{ expanded === entry.id ? '收起' : '展开全文' }}
            <Icon :name="expanded === entry.id ? 'chevron-down' : 'chevron-right'" :size="12" />
          </button>

          <div v-if="confirmingId === entry.id" class="confirm">
            <p>删除「{{ entry.question }}」？删除后这个问题会交给知识库或模型兜底。</p>
            <div class="confirm-actions">
              <button class="ghost" type="button" @click="confirmingId = null">取消</button>
              <button
                class="danger solid"
                type="button"
                :disabled="faq.busy === `delete:${entry.id}`"
                @click="remove(entry.id)"
              >
                {{ faq.busy === `delete:${entry.id}` ? '删除中…' : '删除' }}
              </button>
            </div>
          </div>
        </li>
      </ol>

      <footer v-if="pageCount > 1" class="pager">
        <button
          class="ghost"
          type="button"
          :disabled="page <= 1"
          @click="page = Math.max(1, page - 1)"
        >
          上一页
        </button>
        <span class="pager-info">第 {{ page }} / {{ pageCount }} 页</span>
        <button
          class="ghost"
          type="button"
          :disabled="page >= pageCount"
          @click="page = Math.min(pageCount, page + 1)"
        >
          下一页
        </button>
      </footer>
    </section>

    <FaqDialog v-if="dialog" :entry="dialog.entry" @close="dialog = null" />
  </PageShell>
</template>

<style scoped>
.panel {
  padding: 0;
  overflow: hidden;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border-subtle);
}

.search {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  max-width: 420px;
  height: 32px;
  padding: 0 10px;
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
  height: 100%;
  border: none;
  background: transparent;
  padding: 0;
  font-size: 13px;
}

.search input:focus {
  outline: none;
  box-shadow: none;
}

.count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.retry {
  margin-left: 8px;
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 12px 14px;
}

.row-skeleton {
  height: 62px;
}

.list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.row {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-subtle);
}

.row:last-child {
  border-bottom: none;
}

.row:hover {
  background: var(--bg-hover);
}

.row-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.id {
  flex: none;
  margin-top: 1px;
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--bg-sunken);
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.6;
}

.question {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text);
  overflow-wrap: anywhere;
}

.row-actions {
  flex: none;
  display: flex;
  gap: 0;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.row:hover .row-actions,
.row:focus-within .row-actions {
  opacity: 1;
}

.danger-hover:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-soft);
}

.answer {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.answer.expanded {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
  white-space: pre-wrap;
}

.toggle {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 20px;
  padding: 0 4px;
  border: none;
  background: none;
  color: var(--accent);
  font-size: 11.5px;
}

.toggle:hover:not(:disabled) {
  background: var(--accent-soft);
}

.confirm {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 2px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
}

.confirm p {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.confirm-actions {
  flex: none;
  display: flex;
  gap: 6px;
}

.confirm-actions button {
  height: 26px;
  font-size: 12px;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--border-subtle);
}

.pager button {
  height: 26px;
  font-size: 12px;
}

.pager-info {
  font-size: 12px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
</style>

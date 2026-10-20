<script setup lang="ts">
/**
 * 答案反馈看板（运维 + 管理员）。
 *
 * 三件刻意的事：
 *
 * 1. **比率带分子分母**：「负反馈率 33%」必须同时看到 `1/3` —— 3 条反馈里的 1 条
 *    和 300 条里的 100 条完全不是一回事；
 * 2. **一条都没有时比率画 `—`**，不折算成 0：没有样本的比率不是"0%"，是"没法算"；
 * 3. **看到的是点按钮那一刻的快照**：问题、答案、通道、备注都在这一行里 ——
 *    会话后来被清空也不影响复盘（这正是快照存在的理由）。
 */
import { computed, onMounted, ref } from 'vue'

import Icon from '../components/Icon.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import MetricCard from '../components/MetricCard.vue'
import PageShell from '../components/ui/PageShell.vue'
import type { FeedbackEntry } from '../api/types'
import { feedbackState, loadFeedback } from '../stores/feedback'
import { humanTime } from '../utils/format'

const feedback = feedbackState()
const expanded = ref<number | null>(null)

const WINDOWS = [
  { days: 7, label: '7 天' },
  { days: 30, label: '30 天' },
  { days: 90, label: '90 天' },
  { days: 0, label: '全部' },
]

const rate = computed(() => {
  const value = feedback.summary.negative_rate
  return value === null ? '—' : `${Math.round(value * 100)}%`
})

const rateDetail = computed(() => {
  const total = feedback.summary.total
  if (total === 0) return '还没有反馈，算不出比率'
  return `${feedback.summary.down}/${total} 条反馈是「没帮助」`
})

function preview(entry: FeedbackEntry): string {
  return entry.answer.replace(/\s+/g, ' ').trim()
}

function long(entry: FeedbackEntry): boolean {
  return preview(entry).length > 160
}

function channelLabel(channel: string): string {
  if (channel === 'faq') return 'FAQ 直答'
  if (channel === 'rag') return '知识库'
  if (channel === 'greeting') return '寒暄'
  return channel || '未记录通道'
}

onMounted(() => {
  void loadFeedback()
})
</script>

<template>
  <PageShell
    title="答案反馈"
    :meta="`${feedback.summary.total} 条`"
    description="用户点「有帮助 / 没帮助」时留下的记录（含当轮问答快照）。负反馈率的分母是「有反馈的轮次」，不是全部提问 —— 没点按钮的人默认没意见。"
  >
    <template #actions>
      <button class="ghost" type="button" :disabled="feedback.loading" @click="loadFeedback()">
        <Icon name="refresh" :size="15" />
        刷新
      </button>
    </template>

    <div class="metrics">
      <MetricCard
        label="有帮助"
        :value="String(feedback.summary.up)"
        detail="点了「有帮助」的轮次"
        tone="ok"
      />
      <MetricCard
        label="没帮助"
        :value="String(feedback.summary.down)"
        detail="点了「没帮助」的轮次"
        :tone="feedback.summary.down > 0 ? 'danger' : 'plain'"
      />
      <MetricCard label="负反馈率" :value="rate" :detail="rateDetail" />
      <MetricCard
        label="反馈条数"
        :value="String(feedback.summary.total)"
        :detail="feedback.days === 0 ? '全部历史' : `最近 ${feedback.days} 天`"
      />
    </div>

    <section class="card panel">
      <div class="toolbar">
        <div class="seg" role="group" aria-label="时间窗口">
          <button
            v-for="window in WINDOWS"
            :key="window.days"
            type="button"
            :class="{ on: feedback.days === window.days }"
            :disabled="feedback.loading"
            @click="loadFeedback(window.days, feedback.rating)"
          >
            {{ window.label }}
          </button>
        </div>
        <div class="seg" role="group" aria-label="评级筛选">
          <button
            type="button"
            :class="{ on: feedback.rating === '' }"
            :disabled="feedback.loading"
            @click="loadFeedback(feedback.days, '')"
          >
            全部
          </button>
          <button
            type="button"
            :class="{ on: feedback.rating === 'down' }"
            :disabled="feedback.loading"
            @click="loadFeedback(feedback.days, 'down')"
          >
            仅没帮助
          </button>
        </div>
        <span class="count">{{ feedback.entries.length }} 条</span>
      </div>

      <p v-if="feedback.error" class="alert error">
        {{ feedback.error }}
        <button class="ghost retry" type="button" @click="loadFeedback()">重试</button>
      </p>

      <div v-else-if="feedback.loading && !feedback.loaded" class="skeletons" aria-hidden="true">
        <div v-for="index in 3" :key="index" class="skeleton row-skeleton"></div>
      </div>

      <EmptyState
        v-else-if="feedback.entries.length === 0"
        :title="feedback.rating === 'down' ? '这个窗口里没有「没帮助」' : '还没有反馈'"
        :hint="
          feedback.rating === 'down'
            ? '换个更长的时间窗口看看。'
            : '去「对话」里问几轮，点一下回答下方的「有帮助 / 没帮助」就会出现。'
        "
      />

      <ol v-else class="list">
        <li v-for="entry in feedback.entries" :key="entry.id" class="row">
          <div class="row-head">
            <span class="badge" :class="entry.rating">
              <Icon :name="entry.rating === 'up' ? 'thumb-up' : 'thumb-down'" :size="13" />
              {{ entry.rating === 'up' ? '有帮助' : '没帮助' }}
            </span>
            <p class="question">{{ entry.question }}</p>
            <span class="when">{{ humanTime(entry.created_at) }}</span>
          </div>

          <p class="answer" :class="{ expanded: expanded === entry.id }">{{ preview(entry) }}</p>
          <button
            v-if="long(entry)"
            class="toggle"
            type="button"
            @click="expanded = expanded === entry.id ? null : entry.id"
          >
            {{ expanded === entry.id ? '收起' : '展开全文' }}
            <Icon :name="expanded === entry.id ? 'chevron-down' : 'chevron-right'" :size="12" />
          </button>

          <p v-if="entry.comment" class="comment">「{{ entry.comment }}」</p>

          <div class="meta">
            <span>{{ entry.username || '账号已删除' }}</span>
            <span>·</span>
            <span>{{ channelLabel(entry.channel) }}</span>
            <span>·</span>
            <RouterLink
              class="jump"
              :to="{ name: 'history', params: { session: entry.session_id } }"
            >
              看这段会话
            </RouterLink>
          </div>
        </li>
      </ol>
    </section>
  </PageShell>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.panel {
  padding: 0;
  overflow: hidden;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.seg button {
  height: 28px;
  padding: 0 10px;
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

.count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.retry {
  margin-left: 6px;
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 12px;
}

.row-skeleton {
  height: 70px;
}

.list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.row {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 13px 16px;
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
  gap: 9px;
}

.badge {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-top: 1px;
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 10.5px;
  line-height: 1.7;
}

.badge.up {
  background: var(--success-soft);
  color: var(--success);
}

.badge.down {
  background: var(--danger-soft);
  color: var(--danger);
}

.question {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.when {
  flex: none;
  font-size: 11px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.answer {
  margin: 0 0 0 0;
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

.comment {
  margin: 2px 0 0;
  padding-left: 9px;
  border-left: 2px solid var(--border-strong);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.meta {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-faint);
}

.jump {
  color: var(--accent);
}

@media (max-width: 900px) {
  .metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>

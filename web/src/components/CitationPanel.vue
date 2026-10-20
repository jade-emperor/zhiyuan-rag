<script setup lang="ts">
/**
 * 右侧引用面板：选中的那一条引用的元数据 + 原文预览。
 *
 * 数据来源分两层，界面上要能分清：
 * 1. **引用卡片本身**（source / doc_id / page / score / preview）来自答案的 `citations`；
 * 2. **原文片段**要另调 `GET /documents/{id}/chunks`（父块预览，最多 20 段）——
 *    这是"查看原文"，不是全文导出。员工/运维/管理员都能调（`require_knowledge`），
 *    但看不见的文档会 404；错误原样显示，不翻译成"出错了"。
 *
 * 高亮用问题里的关键词（不是答案里的词）：用户核对的是"我问的东西在这里吗"。
 */
import { computed, nextTick, ref, watch } from 'vue'

import * as api from '../api/endpoints'
import type { ChunkPreview, Citation } from '../api/types'
import { chatState } from '../stores/chat'
import { closePanel, uiState } from '../stores/ui'
import { highlightTerms, looksLikeTable, pageLabel, queryTerms, shorten, tablePreview } from '../utils/citations'
import { citationStrength, scoreText, type ConfidenceAssessment } from '../utils/confidence'
import { assessConfidence } from '../utils/confidence'
import Icon from './Icon.vue'

interface LoadedPreview {
  blocks: ChunkPreview[]
  returned: number
  truncated: boolean
  parentBlocks: number
  matched: number | null
}

const ui = uiState()
const chat = chatState()

const panel = ref<HTMLElement | null>(null)
const loading = ref(false)
const error = ref('')
const loaded = ref<LoadedPreview | null>(null)
const cache = new Map<string, LoadedPreview>()

const selected = computed(() => ui.selected)
const citation = computed<Citation | null>(() => selected.value?.citation ?? null)

const message = computed(() => {
  const target = selected.value
  if (target === null) return null
  return chat.messages.find((item) => item.id === target.messageId) ?? null
})

const question = computed(() => {
  const target = selected.value
  if (target === null) return ''
  const index = chat.messages.findIndex((item) => item.id === target.messageId)
  for (let i = index; i >= 0; i -= 1) {
    if (chat.messages[i].role === 'user') return chat.messages[i].text
  }
  return ''
})

const terms = computed(() => queryTerms(question.value))

const assessment = computed<ConfidenceAssessment>(() => {
  const item = message.value
  if (item === null) {
    return { level: 'unrated', basis: 'unrated', value: null, badge: '', detail: '' }
  }
  return assessConfidence({
    channel: item.channel,
    refused: item.refused,
    citations: item.citations,
    mode: item.request?.mode ?? undefined,
    fusion: item.request?.fusion,
  })
})

const strength = computed(() => {
  if (citation.value === null || message.value === null) return null
  return citationStrength(citation.value, message.value.citations, assessment.value)
})

const table = computed(() => {
  if (citation.value === null) return null
  if (!looksLikeTable(citation.value.preview)) return null
  return tablePreview(citation.value.preview, 4, 5)
})

const images = computed(() =>
  (citation.value?.images ?? []).filter((image) => /^https?:\/\//i.test(image.url)),
)

const source = computed(() => citation.value?.source ?? '')
const filename = computed(() => {
  const value = citation.value?.doc_id ?? ''
  const parts = value.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || value
})

function signatureOf(citationValue: Citation): string {
  return `${citationValue.doc_id}#${citationValue.index}`
}

function normalize(text: string): string {
  return text.replace(/\s+/g, '').slice(0, 40)
}

async function load(documentId: string, citationValue: Citation): Promise<void> {
  const key = signatureOf(citationValue)
  const cached = cache.get(key)
  if (cached !== undefined) {
    loaded.value = cached
    error.value = ''
    void focusMatched()
    return
  }
  loading.value = true
  error.value = ''
  loaded.value = null
  try {
    const result = await api.documentPreview(documentId, 20, 800)
    const needle = normalize(citationValue.preview)
    let matched: number | null = null
    for (const block of result.blocks) {
      if (needle.length > 0 && normalize(block.text).includes(needle)) {
        matched = block.index
        break
      }
    }
    const payload: LoadedPreview = {
      blocks: result.blocks,
      returned: result.returned,
      truncated: result.truncated,
      parentBlocks: result.parent_blocks,
      matched,
    }
    cache.set(key, payload)
    loaded.value = payload
    void focusMatched()
  } catch (failure) {
    // 404（看不见/不存在）与网络错误都如实显示；不写成"未知错误"
    error.value = (failure as Error).message
  } finally {
    loading.value = false
  }
}

async function focusMatched(): Promise<void> {
  await nextTick()
  const target = panel.value?.querySelector('[data-matched="true"]')
  if (target instanceof HTMLElement) {
    target.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
}

function retry(): void {
  if (citation.value !== null) {
    cache.delete(signatureOf(citation.value))
    void load(citation.value.doc_id, citation.value)
  }
}

watch(
  () => (citation.value === null ? '' : signatureOf(citation.value)),
  () => {
    const value = citation.value
    if (value === null) {
      loaded.value = null
      error.value = ''
      return
    }
    void load(value.doc_id, value)
  },
  { immediate: true },
)
</script>

<template>
  <aside ref="panel" class="panel" aria-label="引用原文">
    <header class="head">
      <div class="head-text">
        <span class="eyebrow">引用原文</span>
        <h2 v-if="citation !== null" class="title" :title="citation.doc_id">
          {{ filename }}
        </h2>
        <h2 v-else class="title">未选择引用</h2>
      </div>
      <button
        class="icon-btn"
        type="button"
        title="关闭引用面板"
        aria-label="关闭引用面板"
        @click="closePanel"
      >
        <Icon name="close" :size="16" />
      </button>
    </header>

    <div v-if="citation === null" class="empty">
      <span class="empty-icon"><Icon name="quote" :size="22" /></span>
      <p class="empty-title">选择引用</p>
    </div>

    <div v-else class="content">
      <section class="meta">
        <div class="meta-row">
          <span class="index-badge">{{ citation.index }}</span>
          <span class="tag soft">{{ source }}</span>
          <span class="tag" :class="citation.page === null ? 'soft' : 'ok'">
            {{ pageLabel(citation) }}
          </span>
        </div>

        <dl class="facts">
          <div class="fact">
            <dt>文档</dt>
            <dd class="mono break" :title="citation.doc_id">{{ citation.doc_id || '—' }}</dd>
          </div>
          <div class="fact">
            <dt>相关度</dt>
            <dd>
              <span class="score-value">{{ scoreText(citation, assessment) }}</span>
            </dd>
          </div>
          <div v-if="citation.rerank_score !== null" class="fact">
            <dt>重排分</dt>
            <dd class="score-value">{{ citation.rerank_score.toFixed(3) }}</dd>
          </div>
        </dl>

        <div v-if="strength !== null && strength.value !== null" class="bar" aria-hidden="true">
          <div class="fill" :style="{ width: `${Math.round((strength.value ?? 0) * 100)}%` }"></div>
        </div>
      </section>

      <section class="snippet">
        <div class="section-head">
          <h3>片段</h3>
          
        </div>
        <!-- highlightTerms 先 escapeHtml 再插 <mark>：预览里的尖括号不会变成标签 -->
        <p class="preview" v-html="highlightTerms(citation.preview, terms)"></p>
        <div v-if="table !== null" class="table-wrap mini">
          <table>
            <tbody>
              <tr v-for="(row, rowIndex) in table" :key="rowIndex">
                <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ shorten(cell, 40) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-if="images.length > 0" class="figures">
        <div class="section-head">
          <h3>图片 ({{ images.length }})</h3>
        </div>
        <div class="figure-grid">
          <a
            v-for="image in images"
            :key="image.key"
            class="figure"
            :href="image.url"
            target="_blank"
            rel="noopener noreferrer"
            :aria-label="`查看图片 ${image.key.slice(0, 8)}`"
          >
            <img
              :src="image.url"
              alt="引用图片"
              loading="lazy"
              decoding="async"
              referrerpolicy="no-referrer"
            />
          </a>
        </div>
      </section>

      <section class="original">
        <div class="section-head">
          <h3>原文</h3>
          <button class="ghost small" type="button" :disabled="loading" @click="retry">
            {{ loading ? '读取中…' : '重新读取' }}
          </button>
        </div>

        <p v-if="loading" class="loading"><span class="spin"></span> 正在读取入库后的父块…</p>
        <div v-else-if="error.length > 0" class="alert error">
          {{ error }}
          <button class="ghost small" type="button" @click="retry">重试</button>
        </div>

        <template v-else-if="loaded !== null">
          <p v-if="loaded.blocks.length === 0" class="alert info">
            暂无块
          </p>
          <template v-else>
            <div class="blocks">
              <article
                v-for="block in loaded.blocks"
                :key="block.index"
                class="block"
                :class="{ matched: block.index === loaded.matched }"
                :data-matched="block.index === loaded.matched ? 'true' : 'false'"
              >
                <div class="block-head">
                  <span class="tag soft">#{{ block.index }}</span>
                  <span class="tag" :class="block.page === null ? 'soft' : 'ok'">
                    {{ block.page === null ? '页码未知' : `第 ${block.page} 页` }}
                    <template v-if="block.last_page !== null && block.last_page !== block.page">
                      –{{ block.last_page }}
                    </template>
                  </span>
                  <span class="faint">{{ block.chars }} 字</span>
                  <span v-if="block.index === loaded.matched" class="tag success">命中</span>
                </div>
                <p class="block-text" v-html="highlightTerms(block.text, terms)"></p>
              </article>
            </div>
          </template>

        </template>
      </section>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  height: 100%;
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  border-left: 1px solid var(--border);
}

.head {
  flex: none;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 12px 12px 10px;
  border-bottom: 1px solid var(--border);
}

.head-text {
  flex: 1;
  min-width: 0;
}

.eyebrow {
  display: block;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.07em;
  color: var(--text-faint);
  text-transform: uppercase;
}

.title {
  margin-top: 3px;
  font-size: 13.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty {
  margin: auto;
  padding: 24px;
  max-width: 240px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.empty-icon {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: var(--radius);
  background: var(--bg-sunken);
  color: var(--text-faint);
}

.empty-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}

.empty-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-faint);
}

.cite-sample {
  display: inline-block;
  padding: 0 3px;
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
}

.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.index-badge {
  width: 22px;
  height: 22px;
  border-radius: var(--radius-xs);
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 22px;
  text-align: center;
}

.facts {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.fact {
  display: flex;
  gap: 8px;
  font-size: 12px;
}

.fact dt {
  flex: none;
  width: 44px;
  color: var(--text-faint);
}

.fact dd {
  margin: 0;
  min-width: 0;
  color: var(--text-muted);
}

.break {
  overflow-wrap: anywhere;
}

.score-value {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text);
}

.score-hint {
  display: block;
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.4;
  margin-top: 1px;
}

.bar {
  height: 5px;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--accent);
}

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.section-head h3 {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
}

.preview {
  margin: 0;
  padding: 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-muted);
  border: 1px solid var(--border-subtle);
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}

.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.table-wrap table {
  border-collapse: collapse;
  width: 100%;
  font-size: 11px;
}

.table-wrap td {
  border: 1px solid var(--border-subtle);
  padding: 4px 6px;
  color: var(--text-muted);
}

.mini {
  margin-top: 8px;
}

.figure-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
}

.figure {
  display: block;
  aspect-ratio: 4 / 3;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-sunken);
}

.figure img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.figure:hover {
  border-color: var(--accent);
}

.loading {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: var(--text-muted);
  font-size: 12.5px;
}

.blocks {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block {
  padding: 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg);
}

.block.matched {
  border-color: color-mix(in srgb, var(--warn) 45%, transparent);
  background: var(--warn-soft);
}

.block-head {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.block-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--text-muted);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.small {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}

.note {
  margin: 8px 0 0;
}

.alert.error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
</style>

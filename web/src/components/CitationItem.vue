<script setup lang="ts">
/**
 * 引用清单里的一条（答案下方的「引用来源 (N)」）。
 *
 * 页码的显示口径：**`page === null` 就是"不知道"** —— md/txt 没有页码概念，
 * 老数据也可能没有。这里显示「页码未知」，**不许拿 0 或 1 顶上**：
 * 编一个页号比没有页号糟得多（用户会照着一个错的页码去翻）。
 *
 * 分数条的口径见 `utils/confidence.ts`：能算绝对分才算，其余只画"本次结果内相对强度"，
 * 条下面用小字说明，避免把相对分读成概率。
 */
import { computed } from 'vue'

import type { Citation } from '../api/types'
import type { ConfidenceAssessment } from '../utils/confidence'
import { citationStrength, scoreText } from '../utils/confidence'
import { fileName, looksLikeTable, shorten, tablePreview } from '../utils/citations'
import Icon from './Icon.vue'

const props = defineProps<{
  citation: Citation
  assessment: ConfidenceAssessment
  all: readonly Citation[]
  active?: boolean
}>()

const emit = defineEmits<{ open: [] }>()

const strength = computed(() =>
  citationStrength(props.citation, props.all, props.assessment),
)

const page = computed(() =>
  props.citation.page === null ? '页码未知' : `第 ${props.citation.page} 页`,
)

const table = computed(() =>
  looksLikeTable(props.citation.preview) ? tablePreview(props.citation.preview, 3, 4) : null,
)

const barWidth = computed(() =>
  strength.value.value === null ? '0%' : `${Math.round(strength.value.value * 100)}%`,
)

const images = computed(() =>
  (props.citation.images ?? []).filter((image) => /^https?:\/\//i.test(image.url)),
)
</script>

<template>
  <div
    class="citation"
    :class="{ active }"
    role="button"
    tabindex="0"
    :aria-label="`引用 ${citation.index}：${fileName(citation)}，${page}`"
    @click="emit('open')"
    @keydown.enter.prevent="emit('open')"
    @keydown.space.prevent="emit('open')"
  >
    <span class="index" aria-hidden="true">{{ citation.index }}</span>

    <div class="body">
      <div class="line">
        <span class="file" :title="citation.doc_id">{{ fileName(citation) }}</span>
        <span class="tag soft source">{{ citation.source }}</span>
        <span class="tag" :class="citation.page === null ? 'soft' : 'ok'">{{ page }}</span>
      </div>

      <p v-if="table === null" class="preview">{{ shorten(citation.preview, 180) }}</p>
      <div v-else class="table-thumb" aria-label="表格预览">
        <table>
          <tbody>
            <tr v-for="(row, rowIndex) in table" :key="rowIndex">
              <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ shorten(cell, 24) }}</td>
            </tr>
          </tbody>
        </table>
        <span class="table-label">表格</span>
      </div>

      <div v-if="images.length > 0" class="images" @click.stop>
        <a
          v-for="image in images.slice(0, 3)"
          :key="image.key"
          class="image-link"
          :href="image.url"
          target="_blank"
          rel="noopener noreferrer"
          :aria-label="`查看引用图片 ${image.key.slice(0, 8)}`"
        >
          <img
            :src="image.url"
            alt="引用图片"
            loading="lazy"
            decoding="async"
            referrerpolicy="no-referrer"
          />
        </a>
        <span v-if="images.length > 3" class="more">+{{ images.length - 3 }}</span>
      </div>

      <div class="score-row">
        <div class="bar" :title="strength.hint">
          <div class="fill" :style="{ width: barWidth }"></div>
        </div>
        <span class="score" :title="strength.hint">
          {{ scoreText(citation, assessment) }}
          <span v-if="strength.relative" class="relative">相对</span>
        </span>
      </div>
    </div>

    <span class="open" aria-hidden="true">
      查看原文
      <Icon name="chevron-right" :size="13" />
    </span>
  </div>
</template>

<style scoped>
.citation {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 9px 8px;
  border-top: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background 0.12s ease;
}

.citation:hover,
.citation:focus-visible {
  background: var(--bg-muted);
  outline: none;
}

.citation.active {
  background: var(--accent-soft);
}

.index {
  flex: none;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  border-radius: var(--radius-xs);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  line-height: 20px;
  text-align: center;
}

.body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.line {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.file {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
}

.source {
  flex: none;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preview {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.table-thumb {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg);
  max-height: 64px;
}

.table-thumb table {
  border-collapse: collapse;
  width: 100%;
  font-size: 10.5px;
  color: var(--text-muted);
}

.table-thumb td {
  border: 1px solid var(--border-subtle);
  padding: 2px 4px;
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.table-label {
  position: absolute;
  right: 3px;
  bottom: 3px;
  background: var(--bg-sunken);
  border-radius: 2px;
  padding: 0 3px;
  font-size: 9px;
  color: var(--text-faint);
}

.score-row {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 1px;
}

.bar {
  flex: 1;
  max-width: 120px;
  height: 4px;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
  overflow: hidden;
}

.fill {
  height: 100%;
  border-radius: var(--radius-full);
  background: var(--accent);
  transition: width 0.2s ease;
}

.score {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-faint);
  white-space: nowrap;
}

.relative {
  margin-left: 3px;
  font-family: var(--font-sans);
  color: var(--text-faint);
}

.images {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 2px;
}

.image-link {
  display: block;
  flex: none;
  width: 56px;
  height: 40px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  overflow: hidden;
  background: var(--bg-sunken);
}

.image-link img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.image-link:hover {
  border-color: var(--accent);
}

.more {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-faint);
}

.open {
  flex: none;
  align-self: center;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 24px;
  padding: 0 4px 0 6px;
  border-radius: var(--radius-sm);
  color: var(--text-faint);
  font-size: 11px;
}

.citation:hover .open,
.citation:focus-visible .open {
  color: var(--accent);
}
</style>

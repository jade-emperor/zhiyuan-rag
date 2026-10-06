<script setup lang="ts">
/**
 * 引用清单里的一条。
 *
 * 页码的显示口径来自 P0-5:**`page === null` 就是"不知道"** ——
 * md/txt 没有页码概念,老数据也没有。这里显示「页码未知」,**不许拿 0 或 1 顶上**:
 * 编一个页号比没有页号糟得多(用户会照着一个错的页码去翻)。
 */
import type { Citation } from '../api/types'

defineProps<{ citation: Citation }>()

function score(value: number | null): string {
  return value === null ? '—' : value.toFixed(3)
}
</script>

<template>
  <div class="citation">
    <span class="index">[{{ citation.index }}]</span>
    <div class="body">
      <div class="meta">
        <span class="tag soft">{{ citation.source }}</span>
        <span v-if="citation.page !== null" class="tag ok">第 {{ citation.page }} 页</span>
        <span v-else class="tag soft" title="md/txt 没有页码概念;P0-5 之前入库的块也没有">
          页码未知
        </span>
        <span class="faint">{{ citation.doc_id }}</span>
        <span class="faint score" title="score = 融合分;rerank = 重排分(关了重排就没有)">
          score {{ score(citation.score) }} · rerank {{ score(citation.rerank_score) }}
        </span>
      </div>
      <div class="preview">{{ citation.preview }}…</div>
    </div>
  </div>
</template>

<style scoped>
.citation {
  display: flex;
  gap: 8px;
  padding: 6px 0;
  border-top: 1px dashed var(--border);
}

.index {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.body {
  min-width: 0;
  flex: 1;
}

.meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.score {
  margin-left: auto;
}

.preview {
  color: var(--text-muted);
  font-size: 13px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}
</style>

<script setup lang="ts">
/** 文档预览：父块列表 + 页码。 */
import { computed } from 'vue'

import { closePreview, documentsState } from '../stores/documents'
import DialogShell from './ui/DialogShell.vue'

const docs = documentsState()

const filename = computed(
  () => docs.docs.find((doc) => doc.document_id === docs.previewFor)?.filename ?? docs.previewFor,
)

function pages(page: number | null, last: number | null): string {
  if (page === null) return '页码未知'
  if (last !== null && last !== page) return `第 ${page}–${last} 页`
  return `第 ${page} 页`
}
</script>

<template>
  <DialogShell label="文档预览" :title="filename" size="lg" @close="closePreview">
    <template v-if="docs.preview" #subtitle>
      入库 {{ docs.preview.child_chunks }} 块（子块） · 预览 {{ docs.preview.parent_blocks }} 段（父块）
      <span v-if="docs.preview.truncated"> · 前 {{ docs.preview.returned }} 段</span>
    </template>

    <p v-if="docs.previewLoading" class="muted"><span class="spin"></span> 读取中…</p>
    <p v-if="docs.previewError" class="alert error">{{ docs.previewError }}</p>

    <p v-if="docs.preview && docs.preview.blocks.length === 0" class="alert info">暂无块</p>

    <div class="blocks">
      <article v-for="block in docs.preview?.blocks ?? []" :key="block.index" class="block">
        <div class="block-head">
          <span class="tag soft">#{{ block.index }}</span>
          <span class="tag" :class="block.page === null ? 'soft' : 'ok'">
            {{ pages(block.page, block.last_page) }}
          </span>
          <span class="faint">{{ block.chars }} 字</span>
        </div>
        <p class="text">{{ block.text }}</p>
      </article>
    </div>
  </DialogShell>
</template>

<style scoped>
.blocks {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.block {
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
}

.block:first-child {
  border-top: none;
  padding-top: 0;
}

.block-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 5px;
}

.text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-muted);
  font-size: 12.5px;
  line-height: 1.7;
}
</style>

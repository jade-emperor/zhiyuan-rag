<script setup lang="ts">
/**
 * 预览弹层:这份文档入库后是什么样。
 *
 * 两个必须说清的数字(否则会被读成丢数据):
 * - **入库 N 块** = 写进向量库的**子块**数(与清单卡片的 `chunks` 同口径);
 * - **预览 M 段** = 折叠后的**父块**数(模型真正读到的那段)。
 *
 * 还有一条**诚实的限制**:顺序只能按页码排 ——
 * PDF 基本就是原文顺序,md/txt 只能保证稳定。这里写在界面上,不藏着。
 */
import { computed } from 'vue'

import { closePreview, documentsState } from '../stores/documents'

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
  <div class="overlay" @click.self="closePreview">
    <div class="card dialog" role="dialog" aria-label="文档预览">
      <header>
        <div>
          <h3>{{ filename }}</h3>
          <p v-if="docs.preview" class="faint">
            入库 {{ docs.preview.child_chunks }} 块(子块) · 预览 {{ docs.preview.parent_blocks }} 段(父块)
            <span v-if="docs.preview.truncated">· 只显示前 {{ docs.preview.returned }} 段</span>
          </p>
        </div>
        <button class="ghost" type="button" @click="closePreview">关闭</button>
      </header>

      <p v-if="docs.previewLoading" class="muted"><span class="spin"></span> 读取中…</p>
      <p v-if="docs.previewError" class="alert error">{{ docs.previewError }}</p>

      <div v-if="docs.preview && docs.preview.blocks.length === 0" class="alert info">
        这份文档在向量库里还没有块 —— 可能还在入库,或者入库失败了(回清单看状态)。
      </div>

      <div class="blocks">
        <div v-for="block in docs.preview?.blocks ?? []" :key="block.index" class="block">
          <div class="block-head">
            <span class="tag soft">#{{ block.index }}</span>
            <span class="tag" :class="block.page === null ? 'soft' : 'ok'">
              {{ pages(block.page, block.last_page) }}
            </span>
            <span class="faint">{{ block.chars }} 字</span>
          </div>
          <p class="text">{{ block.text }}</p>
        </div>
      </div>

      <p class="faint note">
        顺序按页码排:同一页内不保证与原文一致(PDF 基本就是原文顺序)。
        要看全文请直接打开原文件 —— 它们在后端的 <code>{{ docs.uploadsDir }}</code> 目录下。
      </p>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(31, 35, 40, 0.35);
  display: grid;
  place-items: center;
  padding: 24px;
  z-index: 50;
}

.dialog {
  width: min(52rem, 100%);
  max-height: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  box-shadow: var(--shadow-lg);
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
}

h3 {
  font-size: 15px;
  word-break: break-all;
}

.blocks {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.block {
  border-top: 1px dashed var(--border);
  padding-top: 8px;
}

.block-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-muted);
  font-size: 13px;
}

.note {
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
</style>

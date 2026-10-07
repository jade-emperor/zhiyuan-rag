<script setup lang="ts">
/**
 * 一张文档卡片(对齐参考图:文件名 / 状态 / 日期 / 块数 / 预览 / 删除)。
 *
 * 「删除」是**两步**:第一次点只是把按钮换成「确认删除?」,第二次才真的发请求。
 * 不弹 `window.confirm`(那个会冻住整页、也没法测),也不"点了就删"——
 * 删错一份文档要重新灌(539 页 PDF 在 CPU 上 8 分钟)。
 */
import { computed, ref } from 'vue'

import type { DocStatus, DocumentRecord } from '../api/types'
import { humanBytes, humanTime } from '../utils/format'

const props = defineProps<{ doc: DocumentRecord; deleting: boolean }>()
const emit = defineEmits<{ preview: []; remove: [] }>()

const confirming = ref(false)

const LABELS: Record<DocStatus, string> = {
  pending: '待处理',
  processing: '处理中',
  processed: '已入库',
  failed: '失败',
  deleting: '删除中',
}

const TONE: Record<DocStatus, string> = {
  pending: 'warn',
  processing: 'warn',
  processed: 'ok',
  failed: 'danger',
  deleting: 'soft',
}

const label = computed(() => LABELS[props.doc.status])
const tone = computed(() => TONE[props.doc.status])
const busy = computed(() => props.doc.status === 'pending' || props.doc.status === 'processing')
</script>

<template>
  <div class="card doc">
    <div class="head">
      <span class="icon" aria-hidden="true">📄</span>
      <div class="title" :title="doc.filename">{{ doc.filename }}</div>
    </div>

    <div class="meta">
      <span class="tag" :class="tone">
        <span v-if="busy" class="spin"></span>
        {{ label }}
      </span>
      <span
        v-if="doc.status === 'processed' && !doc.searchable"
        class="tag warn"
        title="状态是 processed 但库里一块都没有 —— 跑 project-rag doctor 看看"
      >
        检索不到
      </span>
      <span class="faint date">{{ humanTime(doc.created_at) }}</span>
    </div>

    <div class="faint">
      {{ doc.chunks }} 块 · {{ humanBytes(doc.size_bytes) }} · 来源 {{ doc.source }}
    </div>

    <p v-if="doc.error" class="alert error small" :title="doc.error">{{ doc.error }}</p>

    <div class="actions">
      <button type="button" :disabled="busy" @click="emit('preview')">预览</button>
      <template v-if="!confirming">
        <button class="danger" type="button" :disabled="deleting" @click="confirming = true">
          {{ deleting ? '删除中…' : '删除' }}
        </button>
      </template>
      <template v-else>
        <button class="danger" type="button" :disabled="deleting" @click="emit('remove')">
          确认删除?
        </button>
        <button class="ghost" type="button" @click="confirming = false">取消</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.doc {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.icon {
  font-size: 18px;
  line-height: 1.2;
}

.title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-all;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.date {
  margin-left: auto;
}

.actions {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}

.actions button {
  flex: 1;
}

.small {
  font-size: 12px;
}
</style>

<script setup lang="ts">
/**
 * 目录里的文档表格(二级页面的主体)。
 *
 * **显示的是 `state`,不是 `status`**:`status` 只是"登记表怎么说",而登记表是一份
 * 没人失效的缓存 —— 集合被清空过它还写"已入库 677 块"。`state` 是后端把登记表、磁盘、
 * 向量库三处对完之后给的那一个字段。四个不一致态各有各的**修法**(见 `HINTS`),
 * 只说"不一致"不给下一步,用户还是只能来问人。
 *
 * **两个块数并排**:`chunks`(登记说多少)与 `indexed_chunks`(库里实际多少)。
 * 相等才是"对得上";只给一个数根本看不出差在哪。
 *
 * **删除是两步**(第一次点只是把按钮换成「确认删除?」):删错一份文档要重新灌,
 * 而 539 页 PDF 在 CPU 上要 8 分钟。不弹 `window.confirm` —— 那个会冻住整页、也没法测。
 */
import { computed, ref, watch } from 'vue'

import type { DocState, DocumentRecord } from '../api/types'
import { humanBytes, humanTime } from '../utils/format'
import Icon from './Icon.vue'

const props = defineProps<{
  docs: DocumentRecord[]
  /** 正在删的那一份(后端一次删一份,所以同一时刻只会有一个)。 */
  deleting: string
  selected: string[]
}>()
const emit = defineEmits<{
  preview: [documentId: string]
  remove: [documentId: string]
  acl: [documentId: string]
  move: [documentId: string]
  'update:selected': [ids: string[]]
}>()

const STATE_LABELS: Record<DocState, string> = {
  pending: '待处理',
  processing: '处理中',
  processed: '已入库',
  failed: '失败',
  deleting: '删除中',
  ok: '已入库',
  changed: '文件已变更',
  not_indexed: '库内没有块',
  file_missing: '文件已不在',
  unregistered: '未入库',
}

const STATE_TONES: Record<DocState, string> = {
  pending: 'warn',
  processing: 'warn',
  processed: 'ok',
  failed: 'danger',
  deleting: 'soft',
  ok: 'ok',
  changed: 'warn',
  not_indexed: 'danger',
  file_missing: 'danger',
  unregistered: 'warn',
}

/** 每种"不对劲"的下一步:表格里只留最短动作,完整说明挂 title。 */
const HINTS: Partial<Record<DocState, string>> = {
  changed: '内容已旧 · 需重灌',
  not_indexed: '登记说已入库、库内无块 · 重灌',
  file_missing: '原件不在 · 可删登记',
  unregistered: '只有文件、没有登记 · project-rag ingest',
}

const HINT_TITLES: Partial<Record<DocState, string>> = {
  changed: '磁盘文件已变更,库内仍是旧内容;用 project-rag ingest --path <文件> 重灌',
  not_indexed: '登记说已入库,向量库里没有块;重灌或删除登记行',
  file_missing: '磁盘上找不到这个文件;库内若还有块,可删除清掉',
  unregistered: '磁盘上有、登记表没有;用 project-rag ingest --path <文件> 入库',
}

/** 正在等第二次确认的那几行。用 ID 而不是布尔:表格是行级的,一次只确认一行。 */
const confirming = ref<string[]>([])

const allSelected = computed(
  () => props.docs.length > 0 && props.selected.length === props.docs.length,
)

/** 清单变了(换了目录/刷新出新的行)就把"待确认"清掉:那个 ID 可能已经不在了。 */
watch(
  () => props.docs.map((doc) => doc.document_id).join('\u0000'),
  () => {
    confirming.value = []
  },
)

function isConfirming(doc: DocumentRecord): boolean {
  return confirming.value.includes(doc.document_id)
}

function askRemove(doc: DocumentRecord): void {
  confirming.value = [...confirming.value, doc.document_id]
}

function cancelRemove(doc: DocumentRecord): void {
  confirming.value = confirming.value.filter((id) => id !== doc.document_id)
}

function toggleAll(): void {
  emit('update:selected', allSelected.value ? [] : props.docs.map((doc) => doc.document_id))
}

function toggle(doc: DocumentRecord): void {
  const has = props.selected.includes(doc.document_id)
  emit(
    'update:selected',
    has
      ? props.selected.filter((id) => id !== doc.document_id)
      : [...props.selected, doc.document_id],
  )
}

function label(doc: DocumentRecord): string {
  return STATE_LABELS[doc.state]
}

function tone(doc: DocumentRecord): string {
  return STATE_TONES[doc.state]
}

function busy(doc: DocumentRecord): boolean {
  return doc.status === 'pending' || doc.status === 'processing'
}

/** 未登记的文件没有登记行 —— 没有东西可删、也没有行可挪。 */
function registered(doc: DocumentRecord): boolean {
  return doc.status !== 'unregistered'
}

/** 库里一块都没有时预览必然空,按钮点开只会让人以为坏了。 */
function previewable(doc: DocumentRecord): boolean {
  return doc.indexed_chunks > 0
}

function chunkMismatch(doc: DocumentRecord): boolean {
  return registered(doc) && doc.chunks !== doc.indexed_chunks
}

function size(doc: DocumentRecord): string {
  return doc.on_disk ? humanBytes(doc.disk_size_bytes) : '不在'
}
</script>

<template>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="pick">
            <input
              type="checkbox"
              :checked="allSelected"
              :aria-label="allSelected ? '取消全选' : '全选'"
              @change="toggleAll"
            />
          </th>
          <th class="name">名称</th>
          <th class="state">状态</th>
          <th class="num">分块</th>
          <th class="num">大小</th>
          <th class="time">更新</th>
          <th class="actions"><span class="sr-only">操作</span></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="doc in docs"
          :key="doc.document_id"
          :class="{ picked: selected.includes(doc.document_id) }"
        >
          <td class="pick">
            <input
              type="checkbox"
              :checked="selected.includes(doc.document_id)"
              :aria-label="`选择 ${doc.filename}`"
              @change="toggle(doc)"
            />
          </td>

          <td class="name">
            <div class="file">
              <span class="file-icon" aria-hidden="true"><Icon name="file" :size="14" /></span>
              <span class="file-name" :title="doc.document_id">{{ doc.filename }}</span>
            </div>
            <div class="sub">
              <span>{{ doc.source }}</span>
              <span v-if="doc.restricted" class="tag warn">限定可见</span>
              <span v-if="doc.status === 'processed' && !doc.searchable" class="tag danger">
                检索不到
              </span>
              <span v-if="HINTS[doc.state]" class="hint" :title="HINT_TITLES[doc.state]">
                {{ HINTS[doc.state] }}
              </span>
            </div>
            <p v-if="doc.error" class="error-line" :title="doc.error">{{ doc.error }}</p>
          </td>

          <td class="state">
            <span class="tag" :class="tone(doc)">
              <span v-if="busy(doc)" class="spin"></span>
              {{ label(doc) }}
            </span>
          </td>

          <td class="num">
            <span>{{ doc.indexed_chunks }}</span>
            <span v-if="chunkMismatch(doc)" class="mismatch" :title="`登记表记的是 ${doc.chunks} 块`">
              / {{ doc.chunks }}
            </span>
          </td>

          <td class="num">{{ size(doc) }}</td>

          <td class="time">{{ doc.created_at ? humanTime(doc.created_at) : '—' }}</td>

          <td class="actions">
            <template v-if="isConfirming(doc)">
              <button
                class="danger solid small"
                type="button"
                :disabled="deleting === doc.document_id"
                @click="emit('remove', doc.document_id)"
              >
                确认删除?
              </button>
              <button class="ghost small" type="button" @click="cancelRemove(doc)">取消</button>
            </template>
            <template v-else>
              <button
                class="ghost small"
                type="button"
                :disabled="busy(doc) || !previewable(doc)"
                @click="emit('preview', doc.document_id)"
              >
                预览
              </button>
              <button
                v-if="registered(doc)"
                class="ghost small"
                type="button"
                @click="emit('acl', doc.document_id)"
              >
                权限
              </button>
              <button
                v-if="registered(doc)"
                class="ghost small"
                type="button"
                @click="emit('move', doc.document_id)"
              >
                移动
              </button>
              <button
                v-if="registered(doc)"
                class="ghost small danger-text"
                type="button"
                :disabled="deleting === doc.document_id"
                @click="askRemove(doc)"
              >
                {{ deleting === doc.document_id ? '删除中…' : '删除' }}
              </button>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-wrap {
  overflow-x: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

table {
  width: 100%;
  min-width: 760px;
  border-collapse: collapse;
  font-size: 12.5px;
}

thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 8px 10px;
  text-align: left;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--text-faint);
  background: var(--bg-muted);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

tbody td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

tbody tr:last-child td {
  border-bottom: none;
}

tbody tr:hover {
  background: var(--bg-muted);
}

tbody tr.picked {
  background: var(--accent-soft);
}

.pick {
  width: 32px;
  padding-right: 0;
}

.pick input {
  margin: 0;
  cursor: pointer;
}

.name {
  min-width: 220px;
}

.file {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.file-icon {
  flex: none;
  color: var(--text-faint);
  display: grid;
  place-items: center;
}

.file-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sub {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-faint);
  min-height: 16px;
}

.hint {
  color: var(--warn);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.error-line {
  margin: 3px 0 0;
  font-size: 11px;
  color: var(--danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.state {
  width: 108px;
}

.num {
  width: 84px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.mismatch {
  color: var(--warn);
}

.time {
  width: 132px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.actions {
  text-align: right;
  white-space: nowrap;
}

.actions button + button {
  margin-left: 4px;
}

.small {
  height: 26px;
  padding: 0 8px;
  font-size: 11.5px;
}

.danger-text {
  color: var(--danger);
}

.danger-text:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}

@media (max-width: 768px) {
  table {
    min-width: 640px;
  }

  .time {
    display: none;
  }
}
</style>

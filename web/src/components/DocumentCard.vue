<script setup lang="ts">
/**
 * 一张文档卡片(对齐参考图:文件名 / 状态 / 日期 / 块数 / 预览 / 删除)。
 *
 * **显示的是 `state`,不是 `status`**:`status` 只是"登记表怎么说",而登记表是一份
 * 没人失效的缓存 —— 集合被清空了它还写"已入库 677 块",语料目录里新放的文件它根本不知道。
 * `state` 是后端把登记表、磁盘、向量库三处对完之后给的那一个字段(闭集见 `DocState`)。
 * 四个不一致态各有各的**修法**,所以各有各的话术(见 `HINTS`)—— 只说"不一致"不给下一步,
 * 用户还是只能来问人。
 *
 * **两个数并排**:`chunks`(登记说多少块)与 `indexed_chunks`(库里实际多少块)。
 * 相等才是"对得上";不等就是"哪一步出过事",而只给一个数根本看不出来。
 *
 * 「删除」是**两步**:第一次点只是把按钮换成「确认删除?」,第二次才真的发请求。
 * 不弹 `window.confirm`(那个会冻住整页、也没法测),也不"点了就删"——
 * 删错一份文档要重新灌(539 页 PDF 在 CPU 上 8 分钟)。
 */
import { computed, ref } from 'vue'

import type { DocState, DocumentRecord } from '../api/types'
import { humanBytes, humanTime } from '../utils/format'

const props = defineProps<{ doc: DocumentRecord; deleting: boolean }>()
const emit = defineEmits<{ preview: []; remove: []; acl: [] }>()

const confirming = ref(false)

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

/** 每种"不对劲"的下一步。**必须指向一条能照着做的路**(命令或按钮),否则等于没说。 */
const HINTS: Partial<Record<DocState, string>> = {
  changed:
    '磁盘上的文件变了(登记时与现在字节数不同)—— 库里的还是旧内容,重灌才会更新:' +
    ' project-rag ingest --path <这份文件的路径>',
  not_indexed:
    '登记说已入库,向量库里一块都没有(集合被清过 / 那次入库其实没成)—— 重灌:' +
    ' project-rag ingest --path <这份文件的路径>;不想留就点「删除」清掉登记行',
  file_missing:
    '磁盘上找不到这个文件(被手工删了 / 上传的原件被清了)—— 内容回不来了;' +
    '库里若还有块,点「删除」可以把它们一起清掉',
  unregistered:
    '磁盘上有、登记表没有 —— 还没入库:project-rag ingest --path <这个文件的路径>;' +
    '不想入库就把它从语料目录里移走(这张卡片只读:它没有登记行,「删除」删不了磁盘文件)',
}

const label = computed(() => STATE_LABELS[props.doc.state])
const tone = computed(() => STATE_TONES[props.doc.state])
const busy = computed(() => props.doc.status === 'pending' || props.doc.status === 'processing')
/** 未登记的文件没有登记行 —— 没有东西可删(删除接口删的是登记行 + 库内块)。 */
const removable = computed(() => props.doc.status !== 'unregistered')
/** 库里一块都没有时预览必然空,按钮点开只会让人以为坏了。 */
const previewable = computed(() => props.doc.indexed_chunks > 0)
const hint = computed(() => HINTS[props.doc.state] ?? '')
/** 两个块数不一致时才把"登记说多少"摆出来 —— 一致时摆两遍是噪音。 */
const chunkMismatch = computed(
  () => props.doc.status !== 'unregistered' && props.doc.chunks !== props.doc.indexed_chunks,
)
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
        class="tag danger"
        title="登记说 processed 但库里一块都没有 —— 现在搜不到它"
      >
        检索不到
      </span>
      <span
        v-if="doc.restricted"
        class="tag warn"
        title="这份文档被收窄过:只有授权名单里的人 / 组(以及管理员)能检索到它"
      >
        限定可见
      </span>
      <span v-if="doc.created_at" class="faint date">{{ humanTime(doc.created_at) }}</span>
    </div>

    <div class="faint">
      库内 {{ doc.indexed_chunks }} 块
      <template v-if="chunkMismatch">· 登记说 {{ doc.chunks }} 块</template>
      · 磁盘 {{ doc.on_disk ? humanBytes(doc.disk_size_bytes) : '文件不在' }} · 来源
      {{ doc.source }}
    </div>

    <p v-if="hint" class="alert warn small hint">{{ hint }}</p>
    <p v-if="doc.error" class="alert error small" :title="doc.error">{{ doc.error }}</p>

    <div class="actions">
      <button type="button" :disabled="busy || !previewable" @click="emit('preview')">预览</button>
      <button
        v-if="removable"
        class="ghost"
        type="button"
        title="设置这份文档给谁看(某个人 / 某一类员工)"
        @click="emit('acl')"
      >
        权限
      </button>
      <template v-if="!removable">
        <button class="ghost" type="button" disabled title="未登记的文件不在知识库里,没有可删的记录">
          未登记
        </button>
      </template>
      <template v-else-if="!confirming">
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

.hint {
  margin: 0;
  word-break: break-all;
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

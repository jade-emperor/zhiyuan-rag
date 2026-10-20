<script setup lang="ts">
/**
 * 上传面板(对齐参考图左侧的 Actions)。
 *
 * 三条与后端对齐的口径:
 *
 * 1. **`accept` 与大小上限来自 `capabilities`**,前端一处都不写死 ——
 *    写死的话,后端加了格式前端不知道;而后端不认的后缀传上去只会拿到一句报错;
 * 2. **客户端也先拦一次**:后缀不在白名单、或超过大小上限,当场说清楚,
 *    不发那一趟(但**拦不住的仍然以后端为准** —— 客户端的检查是体验,不是安全边界);
 * 3. **拖放与点选走同一条路**:两条路各自实现,迟早有一条忘了校验。
 *
 * 还一条话术上的规矩:**白名单是空的**时不许说「不支持的类型」—— 那是"还没读到
 * capabilities"(能力自述没取到),与"后端不认这个后缀"是两件事。以前混成一句话,
 * 用户看到的是「不支持的类型 .docx —— 这个后端只吃 」(后面空着),被指到错的方向。
 *
 * **上传到哪一格**:目录二级页面把 `folder` 传进来,面板上写明"进「X」" ——
 * 服务端不按地址栏猜,前端也不按"当前页"猜,落点是一个显式参数。
 */
import { computed, ref } from 'vue'

import { capabilitiesState } from '../stores/capabilities'
import { clearUploads, documentsState, uploadFiles } from '../stores/documents'
import Icon from './Icon.vue'
import { humanBytes } from '../utils/format'

const props = withDefaults(
  defineProps<{
    folder?: string
    folderName?: string
  }>(),
  { folder: '', folderName: '未分类' },
)

const caps = capabilitiesState()
const docs = documentsState()

const picker = ref<HTMLInputElement | null>(null)
const dragging = ref(false)

const suffixes = computed(() => caps.data?.suffixes ?? [])
const accept = computed(() => suffixes.value.join(','))
const maxMb = computed(() => caps.data?.max_upload_mb ?? 0)

function openPicker(): void {
  picker.value?.click()
}

function onPicked(event: Event): void {
  const input = event.target as HTMLInputElement
  void submit(Array.from(input.files ?? []))
  input.value = '' // 选同一个文件两次也要能触发 change
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  void submit(Array.from(event.dataTransfer?.files ?? []))
}

/** 客户端先筛一遍:能当场说清的错就别让用户等一趟请求。 */
function reject(file: File): string | null {
  const dot = file.name.lastIndexOf('.')
  const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
  if (suffixes.value.length === 0) {
    return '服务能力尚未加载，请刷新页面或检查服务连接后重试'
  }
  if (!suffixes.value.includes(suffix)) {
    return `暂不支持 ${suffix || '无后缀'} 文件，可用格式为 ${suffixes.value.join(' / ')}`
  }
  if (maxMb.value > 0 && file.size > maxMb.value * 1024 * 1024) {
    return `文件超过 ${maxMb.value} MB 上限（当前 ${humanBytes(file.size)}）`
  }
  return null
}

async function submit(files: File[]): Promise<void> {
  if (files.length === 0) return
  const good: File[] = []
  const bad: { name: string; detail: string }[] = []
  for (const file of files) {
    const reason = reject(file)
    if (reason === null) good.push(file)
    else bad.push({ name: file.name, detail: reason })
  }
  for (const item of bad) {
    docs.uploads.unshift({
      name: item.name,
      size: 0,
      state: 'failed',
      detail: item.detail,
    })
  }
  if (good.length > 0) await uploadFiles(good, props.folder)
}

defineExpose({ openPicker })
</script>

<template>
  <aside class="panel">
    <div class="panel-head">
      <h2>导入资料</h2>
      <button v-if="docs.uploads.length > 0" class="ghost tiny" type="button" @click="clearUploads">
        清空记录
      </button>
    </div>

    <div
      class="drop"
      :class="{ over: dragging }"
      role="button"
      tabindex="0"
      @click="openPicker"
      @keydown.enter="openPicker"
      @dragover.prevent="dragging = true"
      @dragleave="dragging = false"
      @drop.prevent="onDrop"
    >
      <span class="drop-icon"><Icon name="paperclip" :size="17" /></span>
      <strong>拖拽文件至此，或点击选择</strong>
      <span class="faint">支持格式：{{ suffixes.length > 0 ? suffixes.join(' / ') : '正在加载' }}</span>
      <span class="faint">导入至「{{ folderName }}」 · 单份 ≤ {{ maxMb }} MB</span>
      <input
        ref="picker"
        class="hidden-input"
        type="file"
        multiple
        :accept="accept"
        @change="onPicked"
      />
    </div>

    <div v-if="docs.uploads.length > 0" class="tasks">
      <div v-for="(task, index) in docs.uploads" :key="`${task.name}-${index}`" class="task">
        <span class="name" :title="task.name">{{ task.name }}</span>
        <span
          class="tag"
          :class="task.state === 'failed' ? 'danger' : task.state === 'uploading' ? 'warn' : 'ok'"
        >
          {{ task.state === 'uploading' ? '上传中' : task.state === 'submitted' ? '已提交' : '失败' }}
        </span>
        <span class="faint detail" :title="task.detail">{{ task.detail }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  width: 100%;
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 15px;
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 4%, transparent), transparent 58%),
    var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xs);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-head h2 {
  font-size: 13px;
  font-weight: 650;
}

.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  padding: 23px 12px;
  border: 1px dashed color-mix(in srgb, var(--accent) 30%, var(--border-strong));
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--accent) 4%, var(--bg-muted));
  text-align: center;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}

.drop:hover,
.drop.over {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.drop strong {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
}

.drop-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: var(--radius);
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--accent);
  box-shadow: var(--shadow-xs);
}

.hidden-input {
  display: none;
}

.tasks {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 180px;
  overflow-y: auto;
}

.task {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2px 6px;
  align-items: center;
  padding: 6px 0;
  border-top: 1px solid var(--border-subtle);
}

.name {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail {
  grid-column: 1 / -1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tiny {
  height: 22px;
  padding: 0 6px;
  font-size: 11px;
}

@media (max-width: 1040px) {
  .panel {
    position: static;
  }
}
</style>

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
 */
import { computed, ref } from 'vue'

import { capabilitiesState } from '../stores/capabilities'
import { clearUploads, documentsState, uploadFiles } from '../stores/documents'
import { humanBytes } from '../utils/format'

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
  if (!suffixes.value.includes(suffix)) {
    return `不支持的类型 ${suffix || '(无后缀)'} —— 这个后端只吃 ${suffixes.value.join(' / ')}`
  }
  if (maxMb.value > 0 && file.size > maxMb.value * 1024 * 1024) {
    return `超过上限:${humanBytes(file.size)} > ${maxMb.value} MB`
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
  if (good.length > 0) await uploadFiles(good)
}
</script>

<template>
  <aside class="panel">
    <h2>Actions</h2>

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
      <button class="primary block" type="button">上传文件</button>
      <p class="faint">把文件拖到这里也行</p>
      <input
        ref="picker"
        class="hidden-input"
        type="file"
        multiple
        :accept="accept"
        @change="onPicked"
      />
    </div>

    <p class="faint">
      支持:{{ suffixes.length > 0 ? suffixes.join(' / ') : '(还没读到 capabilities)' }}<br />
      单个文件上限 {{ maxMb }} MB
    </p>

    <div v-if="docs.uploads.length > 0" class="tasks">
      <div class="tasks-head">
        <span class="faint">本次上传</span>
        <button class="ghost tiny" type="button" @click="clearUploads">清空</button>
      </div>
      <div v-for="(task, index) in docs.uploads" :key="`${task.name}-${index}`" class="task">
        <span class="name" :title="task.name">{{ task.name }}</span>
        <span class="tag" :class="task.state === 'failed' ? 'danger' : task.state === 'uploading' ? 'warn' : 'ok'">
          {{ task.state === 'uploading' ? '上传中' : task.state === 'submitted' ? '已提交' : '失败' }}
        </span>
        <span class="faint detail">{{ task.detail }}</span>
      </div>
    </div>

    <p class="alert info small">
      上传是**异步入库**:接口立刻返回,后台切块嵌入(539 页 PDF 在 CPU 上约 8 分钟)。
      卡片上的状态会自动刷新;**只有「已入库」才是能被检索到的**。
    </p>
  </aside>
</template>

<style scoped>
.panel {
  width: 260px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

h2 {
  font-size: 16px;
}

.drop {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  padding: 16px 12px;
  text-align: center;
  cursor: pointer;
  background: var(--bg);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.drop.over {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.block {
  width: 100%;
}

.hidden-input {
  display: none;
}

.tasks {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tasks-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.task {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 4px 8px;
  font-size: 12px;
}

.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail {
  grid-column: 1 / -1;
}

.tiny {
  font-size: 12px;
  padding: 2px 6px;
}

.small {
  font-size: 12px;
}
</style>

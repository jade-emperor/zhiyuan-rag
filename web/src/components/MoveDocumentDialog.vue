<script setup lang="ts">
/**
 * 「移动到目录」弹层。
 *
 * 挪动**只改归属**:不重切、不重灌、块 id 一个都不变 —— 所以它是毫秒级的,
 * 而且不会让历史引用失效。弹层里把这一点写出来,是因为"移动"听起来像会重建索引,
 * 管理员不敢点。
 */
import { computed, onMounted, ref } from 'vue'

import { documentsState, moveDocument } from '../stores/documents'
import { foldersState, refreshFolders } from '../stores/folders'
import DialogShell from './ui/DialogShell.vue'

const props = defineProps<{ documentId: string }>()
const emit = defineEmits<{ close: [] }>()

const docs = documentsState()
const folders = foldersState()

const target = ref<string>('')
const saving = ref(false)
const error = ref('')

const doc = computed(() => docs.docs.find((item) => item.document_id === props.documentId))
const current = computed(() => doc.value?.folder ?? '')
const filename = computed(() => doc.value?.filename ?? props.documentId)

/** 可选项:全部真实目录 + 未分类(当前所在的那一格置灰 —— 挪到原地没有意义)。 */
const choices = computed(() => {
  const rows = folders.folders.map((folder) => ({
    id: folder.folder_id,
    name: folder.name,
    detail: `${folder.documents} 份`,
  }))
  const unfiled = folders.unfiled
  rows.push({
    id: 'unfiled',
    name: unfiled?.name ?? '未分类',
    detail: `${unfiled?.documents ?? 0} 份`,
  })
  return rows
})

onMounted(async () => {
  if (folders.folders.length === 0) await refreshFolders()
  target.value = ''
})

async function submit(): Promise<void> {
  saving.value = true
  error.value = ''
  // 界面上的 "unfiled" 是路由用的别名,接口里用空串表示(与后端 `UNFILED_ID` 同一条口径)。
  await moveDocument(props.documentId, target.value === 'unfiled' ? '' : target.value)
  saving.value = false
  if (docs.error) {
    error.value = docs.error
    return
  }
  emit('close')
}
</script>

<template>
  <DialogShell label="移动文档" title="移动到目录" size="sm" @close="emit('close')">
    <template #subtitle>
      <span class="truncate">{{ filename }}</span>
      · 只改归属,不重切、不重灌,历史引用不受影响
    </template>

    <ul class="choices">
      <li v-for="choice in choices" :key="choice.id">
        <label :class="{ disabled: choice.id === current }">
          <input
            v-model="target"
            type="radio"
            :value="choice.id"
            :disabled="choice.id === current"
          />
          <span class="name">{{ choice.name }}</span>
          <span class="faint">{{ choice.detail }}</span>
          <span v-if="choice.id === current" class="tag soft">当前</span>
        </label>
      </li>
    </ul>

    <p v-if="error" class="alert error">{{ error }}</p>

    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="saving || target === ''" @click="submit">
        {{ saving ? '移动中…' : '移动' }}
      </button>
    </template>
  </DialogShell>
</template>

<style scoped>
.choices {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 320px;
  overflow-y: auto;
}

.choices label {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.choices label:hover {
  background: var(--bg-muted);
}

.choices label.disabled {
  cursor: default;
  opacity: 0.55;
}

.choices input[type='radio'] {
  margin: 0;
}

.name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>

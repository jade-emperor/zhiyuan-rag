<script setup lang="ts">
/**
 * 目录的新建 / 改名弹层。两种模式共用一份表单:字段一样,只有标题与提交动作用的不同。
 *
 * 名字的规则**不在前端重写一遍**(长度、保留字、斜杠都在后端 `check_folder_name` 一处):
 * 前端只做"别提交空名",其余交给后端 —— 两处各写一套规则,迟早有一处松,
 * 而松的那一处会让用户存进去一个命令行再也找不到的目录名。
 */
import { computed, ref } from 'vue'

import type { FolderRecord } from '../api/types'
import { createFolder, foldersState, renameFolder } from '../stores/folders'
import DialogShell from './ui/DialogShell.vue'

const props = defineProps<{
  /** 给了就是改名,不给就是新建。 */
  folder?: FolderRecord | null
}>()
const emit = defineEmits<{ close: []; done: [folder: FolderRecord | null] }>()

const folders = foldersState()

const name = ref(props.folder?.name ?? '')
const description = ref(props.folder?.description ?? '')
const error = ref('')

const editing = computed(() => props.folder != null)
const title = computed(() => (editing.value ? '重命名目录' : '新建目录'))
const busy = computed(() =>
  editing.value
    ? folders.busy === `rename:${props.folder?.folder_id}`
    : folders.busy === 'create',
)

async function submit(): Promise<void> {
  const trimmed = name.value.trim()
  if (!trimmed) {
    error.value = '目录名不能为空'
    return
  }
  error.value = ''
  if (editing.value && props.folder) {
    await renameFolder(props.folder.folder_id, trimmed, description.value)
    if (folders.error) {
      error.value = folders.error
      return
    }
    emit('done', null)
    emit('close')
    return
  }
  const created = await createFolder(trimmed, description.value)
  if (created === null) {
    error.value = folders.error || '新建失败'
    return
  }
  emit('done', created)
  emit('close')
}
</script>

<template>
  <DialogShell :label="title" :title="title" size="sm" @close="emit('close')">
    <template #subtitle>
      目录是一级分类,只有两层 —— 文档直接放在目录里,不再往下分子目录。
    </template>

    <label class="field">
      <span>名称</span>
      <input
        v-model="name"
        type="text"
        maxlength="40"
        placeholder="例如：公司制度"
        autofocus
        @keydown.enter.prevent="submit"
      />
    </label>

    <label class="field">
      <span>说明 <span class="faint">(可选)</span></span>
      <input
        v-model="description"
        type="text"
        maxlength="120"
        placeholder="说明该目录收录的内容与适用范围"
        @keydown.enter.prevent="submit"
      />
    </label>

    <p v-if="error" class="alert error">{{ error }}</p>

    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="busy" @click="submit">
        {{ busy ? '保存中…' : editing ? '保存' : '创建' }}
      </button>
    </template>
  </DialogShell>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field > span {
  font-size: 12px;
  color: var(--text-muted);
}
</style>

<script setup lang="ts">
/**
 * 组侧授权弹层:**这个组能看哪些文档**。
 *
 * 与「知识库」页那份文档侧授权（`DocumentAclDialog`）读的是**同一张规则表**，
 * 只是视角反过来 —— 有人授权时盯着"这份文档给谁看"，也有人盯着"这个组能看哪些"。
 *
 * 三态与两条边界（口径都在后端，这里只如实说）：
 *
 * - **人人可见**（没有规则）：本组本来就能看。**这个弹层禁不掉它** —— 要限制一份公开文档是
 *   一次动别人可见性的操作，所以这里给的是一个显式按钮「收窄成只有本组可见」，
 *   而不是藏在一个勾选框里；
 * - **已授权 / 看不到**：勾上 = 把本组加进这份文档的名单，取消 = 移出。
 *   移出之后**名单空了**，这份文档会回到人人可见 —— 后端会把这几份点名回报，
 *   这里原样显示（那是"取消一个组"能造成的最意外的后果）。
 */
import { computed, onMounted, ref } from 'vue'

import * as api from '../api/endpoints'
import type { GroupDocument, GroupDocumentsOut } from '../api/types'

const props = defineProps<{ groupId: string; groupName: string }>()
const emit = defineEmits<{ close: [] }>()

const documents = ref<GroupDocument[]>([])
const allowed = ref<string[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const savingId = ref('')

const openCount = computed(() => documents.value.filter((doc) => doc.visibility === 'open').length)

function payload(): string[] {
  // 人人可见的文档**不放进名单**：放进去也不会改变什么（后端不把它们算作授权），
  // 而放了会让"我勾了它"变成一句谎
  return allowed.value.filter((id) => {
    const doc = documents.value.find((item) => item.document_id === id)
    return doc !== undefined && doc.visibility !== 'open'
  })
}

function toggle(documentId: string): void {
  allowed.value = allowed.value.includes(documentId)
    ? allowed.value.filter((item) => item !== documentId)
    : [...allowed.value, documentId]
}

function adopt(result: GroupDocumentsOut): void {
  documents.value = result.documents
  allowed.value = result.documents
    .filter((doc) => doc.visibility === 'allowed')
    .map((doc) => doc.document_id)
  notice.value = result.notice
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    adopt(await api.adminGroupDocuments(props.groupId))
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    adopt(await api.adminSetGroupDocuments(props.groupId, payload()))
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    saving.value = false
  }
}

/** 把一份**公开**文档收窄成"只有这个组能看"：走文档侧那份整份名单的接口。 */
async function restrictToGroup(documentId: string): Promise<void> {
  savingId.value = documentId
  error.value = ''
  notice.value = ''
  try {
    await api.adminSetDocumentAcl(documentId, { user_ids: [], group_ids: [props.groupId] })
    adopt(await api.adminGroupDocuments(props.groupId))
    notice.value = '已把这份文档收窄成只有本组能看（别人看不到了，这是文档侧的可见范围）'
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    savingId.value = ''
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="card dialog" role="dialog" aria-label="组的文档权限">
      <header>
        <div>
          <h3>「{{ groupName }}」能看的文档</h3>
          <p class="faint">
            勾上 = 把这个组加进这份文档的名单；取消 = 移出。
            <span v-if="openCount > 0">
              <br />
              其中 {{ openCount }} 份是<strong>人人可见</strong>（没有配过可见范围）——
              这里禁不掉它们，要用右边那个按钮收窄。
            </span>
          </p>
        </div>
        <button class="ghost" type="button" @click="emit('close')">关闭</button>
      </header>

      <p v-if="loading" class="muted"><span class="spin"></span> 读取中…</p>
      <p v-if="error" class="alert error">{{ error }}</p>
      <p v-if="notice" class="alert info">{{ notice }}</p>

      <div v-if="!loading" class="rows">
        <div v-for="doc in documents" :key="doc.document_id" class="row">
          <label class="check">
            <input
              type="checkbox"
              :checked="doc.visibility === 'open' ? true : allowed.includes(doc.document_id)"
              :disabled="doc.visibility === 'open'"
              @change="toggle(doc.document_id)"
            />
            <span class="name" :title="doc.document_id">{{ doc.filename }}</span>
          </label>
          <span v-if="doc.visibility === 'open'" class="tag soft">人人可见</span>
          <span v-else-if="doc.visibility === 'allowed'" class="tag ok">已授权本组</span>
          <span v-else class="tag warn">看不到</span>
          <span v-if="doc.visibility !== 'open' && doc.other_subjects === 0" class="faint small">
            取消后这份会回到人人可见
          </span>
          <button
            v-if="doc.visibility === 'open'"
            class="ghost small"
            type="button"
            :disabled="savingId === doc.document_id"
            @click="restrictToGroup(doc.document_id)"
          >
            {{ savingId === doc.document_id ? '收窄中…' : '收窄成只有本组可见' }}
          </button>
        </div>
        <p v-if="documents.length === 0" class="faint">知识库里还没有文档。</p>
      </div>

      <div class="actions">
        <button class="primary" type="button" :disabled="loading || saving" @click="save">
          {{ saving ? '保存中…' : '保存（整份名单）' }}
        </button>
        <span class="faint">保存只影响这个组的授权；不改动文档本身的内容。</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dialog {
  width: min(720px, 92vw);
  max-height: 86vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px;
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

h3 {
  font-size: 16px;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
}

.check {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.small {
  font-size: 12px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
</style>

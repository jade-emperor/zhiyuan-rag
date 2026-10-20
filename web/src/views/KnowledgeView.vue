<script setup lang="ts">
/**
 * 知识库**一级页面**:目录清单。点一格进二级页面(那一格里的文档)。
 *
 * 两条口径:
 *
 * - **读数来自后端一次对账**(`GET /api/v1/folders`):份数 / 库内块数 / 需要处理的份数
 *   都是登记表、磁盘、向量库对完之后算的 —— 前端不拿列表长度自己数(那会漏掉
 *   语料目录里还没入库的文件);
 * - **未分类恒定出现**:磁盘上刚放进来、还没登记的文件只能归它。它是兜底格子,
 *   不是错误状态,所以画成虚线边框而不是告警色。
 *
 * 页面本身不轮询:目录的计数只在"文档有增删/入库完成"时变,而那两件事都发生在二级页面,
 * 它会自己轮询并在回来时重拉。这一页在窗口重新可见 / 拿到焦点时刷一次就够了。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import FolderCard from '../components/FolderCard.vue'
import FolderDialog from '../components/FolderDialog.vue'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import PageShell from '../components/ui/PageShell.vue'
import { foldersState, refreshFolders } from '../stores/folders'
import { humanBytes } from '../utils/format'

const router = useRouter()
const folders = foldersState()

const creating = ref(false)
const query = ref('')

/** 这份清单的读数(含未分类那一格)。 */
const totals = computed(() => {
  const all = [...folders.folders, ...(folders.unfiled ? [folders.unfiled] : [])]
  return {
    documents: all.reduce((sum, folder) => sum + folder.documents, 0),
    chunks: all.reduce((sum, folder) => sum + folder.chunks, 0),
    size: all.reduce((sum, folder) => sum + folder.size_bytes, 0),
    issues: all.reduce((sum, folder) => sum + folder.issues, 0),
  }
})

/** 搜索只过滤**真实目录**:未分类是兜底格子,不该因为搜不到关键词就消失。 */
const visible = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return folders.folders
  return folders.folders.filter(
    (folder) =>
      folder.name.toLowerCase().includes(needle) ||
      folder.description.toLowerCase().includes(needle),
  )
})

const empty = computed(
  () => folders.folders.length === 0 && (folders.unfiled?.documents ?? 0) === 0,
)

function open(folderId: string): void {
  void router.push({ name: 'folder', params: { folder: folderId } })
}

function onVisible(): void {
  if (document.visibilityState === 'visible') void refreshFolders()
}

onMounted(() => {
  void refreshFolders()
  window.addEventListener('focus', onVisible)
  document.addEventListener('visibilitychange', onVisible)
})

onUnmounted(() => {
  window.removeEventListener('focus', onVisible)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<template>
  <PageShell title="知识库" :meta="`${folders.summary.folders} 个目录`">
    <template #actions>
      <span v-if="folders.refreshedAt" class="faint">上次刷新 {{ folders.refreshedAt }}</span>
      <button class="ghost" type="button" :disabled="folders.loading" @click="refreshFolders">
        <Icon name="refresh" :size="14" />
        {{ folders.loading ? '刷新中…' : '刷新' }}
      </button>
      <button class="primary" type="button" @click="creating = true">
        <Icon name="plus" :size="14" /> 新建目录
      </button>
    </template>

    <p v-if="!empty" class="totals">
      共 {{ totals.documents }} 份 · {{ totals.chunks }} 块 · {{ humanBytes(totals.size) }}
      <span v-if="totals.issues > 0" class="warn-text">· {{ totals.issues }} 份需要处理</span>
    </p>

    <p v-if="folders.error" class="alert error">{{ folders.error }}</p>

    <div v-if="!empty" class="toolbar">
      <label class="search">
        <Icon name="search" :size="14" />
        <input v-model="query" type="search" placeholder="搜索目录" aria-label="搜索目录" />
      </label>
      <span v-if="query.trim()" class="faint">{{ visible.length }} / {{ folders.folders.length }}</span>
    </div>

    <EmptyState
      v-if="empty"
      title="还没有目录"
      hint="目录是一级分类。建一个,再把文档传进去;也可以直接用 project-rag ingest --folder <名字> 灌。"
    >
      <button class="primary" type="button" @click="creating = true">
        <Icon name="plus" :size="14" /> 新建目录
      </button>
    </EmptyState>

    <div v-else class="grid">
      <FolderCard
        v-for="folder in visible"
        :key="folder.folder_id"
        :folder="folder"
        @open="open(folder.folder_id)"
      />
      <!-- 未分类是兜底格子:**空的就不占位置**("0 份"的一张虚线圈看着像坏了)。
           一旦有磁盘上的孤儿文件,它会自己出现。 -->
      <FolderCard
        v-if="folders.unfiled && folders.unfiled.documents > 0"
        :folder="folders.unfiled"
        @open="open(folders.unfiled.folder_id)"
      />
    </div>

    <p v-if="!empty && visible.length === 0 && query.trim()" class="faint">
      没有匹配「{{ query.trim() }}」的目录。
    </p>

    <FolderDialog v-if="creating" @close="creating = false" @done="creating = false" />
  </PageShell>
</template>

<style scoped>
.totals {
  margin: -10px 0 0;
  font-size: 12px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.warn-text {
  color: var(--warn);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}

.search {
  display: flex;
  align-items: center;
  gap: 6px;
  width: min(320px, 100%);
  padding: 0 9px;
  height: 32px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-faint);
}

.search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-ring);
}

.search input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 0;
  height: 100%;
  font-size: 12.5px;
  color: var(--text);
}

.search input:focus {
  outline: none;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(264px, 1fr));
  gap: 12px;
}

@media (max-width: 768px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

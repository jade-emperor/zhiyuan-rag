<script setup lang="ts">
/**
 * 左侧工作区：品牌 + 新建对话 + 知识库范围 + 搜索 + 历史。
 *
 * 两处刻意的取舍：
 * 1. 知识库是**单选**：后端 `source_filter` 只接受一个来源（`QueryRequest.source_filter`），
 *    做成多选会发出它解释不了的请求。这里如实单选，并在下方写清口径；
 * 2. 顶栏（AppHeader）仍承担跨页导航；侧栏只管"对话"这一个工作区，
 *    不复制导航，避免两个入口指向同一页却守不同能力。
 */
import { computed, onMounted } from 'vue'

import { newChat } from '../stores/chat'
import { loadSources, sourcesState } from '../stores/sources'
import { paramsState, persist } from '../stores/params'
import { closeDrawer, uiState } from '../stores/ui'
import Icon from './Icon.vue'
import SessionList from './SessionList.vue'
import { ref } from 'vue'

const params = paramsState()
const sources = sourcesState()
const ui = uiState()

const search = ref('')

const currentLabel = computed(() => params.source_filter ?? '全部知识库')

/** 当前选中的来源被删掉时，select 里不能没有这一项（否则会显示成第一项，骗人）。 */
const orphan = computed(() =>
  params.source_filter !== null && !sources.items.includes(params.source_filter)
    ? params.source_filter
    : null,
)

function touch(): void {
  persist()
}

async function reloadSources(): Promise<void> {
  await loadSources(true)
}

function startNew(): void {
  newChat()
  if (ui.compact) closeDrawer()
}

onMounted(async () => {
  await loadSources()
})
</script>

<template>
  <aside class="sidebar" aria-label="对话工作区">
    <div class="workspace-label">
      <span class="workspace-title">
        <Icon name="message" :size="15" />
        智能问答
      </span>
      <button
        v-if="ui.compact"
        class="icon-btn close"
        type="button"
        title="关闭侧栏"
        aria-label="关闭侧栏"
        @click="closeDrawer"
      >
        <Icon name="close" :size="16" />
      </button>
    </div>

    <button class="primary new-chat" type="button" @click="startNew">
      <Icon name="plus" :size="16" />
      新建对话
    </button>

    <section class="kb">
      <div class="field-label">
        <span>检索范围</span>
        <button
          class="icon-btn"
          type="button"
          title="刷新知识库列表"
          aria-label="刷新知识库列表"
          :disabled="sources.loading"
          @click="reloadSources"
        >
          <Icon name="refresh" :size="13" />
        </button>
      </div>
      <select
        v-model="params.source_filter"
        aria-label="知识库范围"
        :title="`当前：${currentLabel}（一次只检索一个来源）`"
        @change="touch"
      >
        <option :value="null">全部知识库</option>
        <option v-if="orphan !== null" :value="orphan">{{ orphan }}（已不在清单中）</option>
        <option v-for="name in sources.items" :key="name" :value="name">{{ name }}</option>
      </select>
      <p v-if="sources.error" class="alert error small">
        {{ sources.error }}
        <button class="ghost retry" type="button" @click="reloadSources">重试</button>
      </p>
      <p v-else class="hint">{{ sources.items.length }} 个知识来源</p>
    </section>

    <label class="search">
      <Icon name="search" :size="15" aria-hidden="true" />
      <input v-model="search" type="search" placeholder="搜索历史问答" aria-label="搜索对话" />
      <button
        v-if="search.length > 0"
        class="icon-btn"
        type="button"
        title="清除搜索"
        aria-label="清除搜索"
        @click="search = ''"
      >
        <Icon name="close" :size="13" />
      </button>
    </label>

    <SessionList :query="search" />

    <div class="sidebar-foot">
      <span class="foot-mark"><Icon name="quote" :size="12" /></span>
      <p>回答可点击引用，回到原文核验。</p>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--bg);
  border-right: 1px solid var(--border);
}

.workspace-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 4px 4px;
}

.workspace-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.close {
  margin-left: auto;
}

.new-chat {
  width: 100%;
  height: 36px;
  justify-content: flex-start;
  padding-left: 10px;
}

.kb {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 2px 2px 8px;
  border-bottom: 1px solid var(--border-subtle);
}

.field-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-muted);
}

.kb select {
  height: 30px;
  font-size: 12.5px;
}

.hint {
  margin: 0;
  padding: 0 2px;
  color: var(--text-faint);
  font-size: 11px;
}

.small {
  font-size: 11.5px;
  padding: 6px 8px;
}

.retry {
  height: 20px;
  padding: 0 5px;
  font-size: 11px;
}

.search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  height: 32px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-faint);
  font-size: var(--fs-sm);
  font-weight: 400;
}

.search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-ring);
}

.search input {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: none;
  background: transparent;
  padding: 0;
  box-shadow: none;
}

.search input:focus {
  border: none;
  box-shadow: none;
}

.search .icon-btn {
  width: 22px;
  height: 22px;
}

.sidebar-foot {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 10px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: var(--bg-muted);
}

.foot-mark {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  flex: none;
  border-radius: var(--radius-xs);
  background: var(--accent-soft);
  color: var(--accent);
}

.sidebar-foot p {
  margin: 0;
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.55;
}
</style>

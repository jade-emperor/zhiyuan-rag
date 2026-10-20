<script setup lang="ts">
/**
 * 主区顶栏：会话标题 + 当前检索范围/方式 + 两个面板开关。
 *
 * 这里的标签都来自**当前参数快照**（`params`），不写死任何能力清单；
 * 具体可选值仍由 `capabilities` 提供（在参数抽屉里）。
 */
import { computed } from 'vue'

import type { FusionMode, RetrievalMode } from '../api/types'
import { chatState, currentSessionTitle } from '../stores/chat'
import { capabilitiesState } from '../stores/capabilities'
import { paramsState } from '../stores/params'
import { openSettings, togglePanel, toggleSidebar, uiState } from '../stores/ui'
import Icon from './Icon.vue'

const chat = chatState()
const params = paramsState()
const ui = uiState()
const caps = capabilitiesState()

function modeLabel(mode: RetrievalMode): string {
  switch (mode) {
    case 'vector':
      return '向量检索'
    case 'keyword':
      return 'BM25'
    case 'hybrid':
      return '混合检索'
    default:
      return mode
  }
}

function fusionLabel(fusion: FusionMode): string {
  switch (fusion) {
    case 'rrf':
      return 'RRF'
    case 'weighted':
      return '加权'
    default:
      return fusion
  }
}

const scope = computed(() => params.source_filter ?? '全部知识库')
const method = computed(() => {
  const base = modeLabel(params.mode)
  if (params.mode !== 'hybrid') return base
  return `${base} · ${fusionLabel(params.fusion)}`
})
const rerankAvailable = computed(() => caps.data?.retrieval.rerank_available !== false)
const referenceCount = computed(() =>
  chat.messages.reduce((total, item) => total + item.citations.length, 0),
)
</script>

<template>
  <header class="topbar">
    <button
      class="icon-btn"
      type="button"
      title="打开/收起问答列表"
      aria-label="打开对话列表"
      :aria-expanded="ui.drawerOpen"
      @click="toggleSidebar"
    >
      <Icon name="menu" :size="17" />
    </button>

    <div class="title-block">
      <h1 class="title" :title="currentSessionTitle()">{{ currentSessionTitle() }}</h1>
      <div class="chips">
        <button class="chip" type="button" title="检索范围" @click="openSettings">
          <Icon name="book" :size="12" />
          {{ scope }}
        </button>
        <button class="chip" type="button" title="检索方式" @click="openSettings">
          {{ method }}
        </button>
        <span v-if="rerankAvailable" class="chip ghost-chip">
          {{ params.rerank ? '重排 · 已启用' : '重排 · 未启用' }}
        </span>
      </div>
    </div>

    <div class="actions">
      <span v-if="chat.generating" class="generating" role="status">
        <span class="spin"></span> 正在生成
      </span>
      <button
        class="icon-btn"
        type="button"
        title="检索设置"
        aria-label="检索设置"
        :aria-expanded="ui.settingsOpen"
        @click="openSettings"
      >
        <Icon name="settings" :size="16" />
      </button>
      <button
        class="icon-btn panel-toggle"
        :class="{ active: ui.panelOpen }"
        type="button"
        title="引用原文"
        aria-label="引用原文面板"
        :aria-pressed="ui.panelOpen"
        @click="togglePanel"
      >
        <Icon name="quote" :size="16" />
        <span v-if="referenceCount > 0" class="count">{{ referenceCount }}</span>
      </button>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  flex: none;
  height: var(--topbar-h);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(8px);
  min-width: 0;
}

.title-block {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 10px;
  overflow: hidden;
}

.title {
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: -0.01em;
  max-width: 34%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chips {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  overflow: hidden;
}

.chip {
  height: 22px;
  padding: 0 7px;
  gap: 4px;
  border: 1px solid var(--border);
  background: var(--bg-muted);
  color: var(--text-muted);
  border-radius: var(--radius-full);
  font-size: 11px;
  font-weight: 500;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chip:hover:not(:disabled) {
  background: var(--bg-sunken);
  color: var(--text);
}

.ghost-chip {
  border-color: transparent;
  background: transparent;
  cursor: default;
  color: var(--text-faint);
}

.actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: none;
}

.generating {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: 4px;
  color: var(--text-muted);
  font-size: 11.5px;
  white-space: nowrap;
}

.panel-toggle {
  position: relative;
}

.count {
  position: absolute;
  top: 1px;
  right: 0;
  min-width: 13px;
  height: 13px;
  padding: 0 3px;
  border-radius: var(--radius-full);
  background: var(--accent);
  color: #fff;
  font-size: 9px;
  line-height: 13px;
  text-align: center;
}

@media (max-width: 768px) {
  .topbar {
    padding: 0 8px;
    gap: 6px;
  }

  .title {
    max-width: 46%;
    font-size: 13px;
  }

  .chips .ghost-chip {
    display: none;
  }

  .generating {
    display: none;
  }
}
</style>

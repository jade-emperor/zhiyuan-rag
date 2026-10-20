<script setup lang="ts">
/** 引用来源区块：折叠头 + 引用卡片列表。 */
import type { Citation } from '../api/types'
import type { ChatMessage } from '../stores/chat'
import type { ConfidenceAssessment } from '../utils/confidence'
import CitationItem from './CitationItem.vue'
import Icon from './Icon.vue'

defineProps<{
  message: ChatMessage
  assessment: ConfidenceAssessment
  expanded: boolean
  selectedIndex: number | null
}>()

defineEmits<{
  toggle: []
  open: [citation: Citation]
}>()
</script>

<template>
  <section v-if="message.citations.length > 0" class="citations">
    <button
      class="citations-toggle"
      type="button"
      :aria-expanded="expanded"
      @click="$emit('toggle')"
    >
      <Icon :name="expanded ? 'chevron-down' : 'chevron-right'" :size="14" />
      引用来源 ({{ message.citations.length }})
    </button>

    <div v-show="expanded" class="citation-list">
      <CitationItem
        v-for="citation in message.citations"
        :key="citation.index"
        :citation="citation"
        :assessment="assessment"
        :all="message.citations"
        :active="selectedIndex === citation.index"
        @open="$emit('open', citation)"
      />
    </div>
  </section>
</template>

<style scoped>
.citations {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  overflow: hidden;
}

.citations-toggle {
  width: 100%;
  height: 36px;
  padding: 0 10px;
  justify-content: flex-start;
  gap: 6px;
  border: none;
  border-radius: 0;
  background: var(--bg-muted);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
}

.citations-toggle:hover:not(:disabled) {
  background: var(--bg-sunken);
}

.citation-list {
  padding: 0 4px;
}
</style>

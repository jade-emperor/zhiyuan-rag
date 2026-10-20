<script setup lang="ts">
/** AI 消息底部操作栏：复制 / 重新生成 / 反馈 / 引用数。 */
import type { ChatMessage } from '../stores/chat'
import Icon from './Icon.vue'

defineProps<{
  message: ChatMessage
  canRegenerate: boolean
  regenerateTitle: string
  copied: boolean
  /** 这条的反馈正在写库：两个按钮都置灰，避免连点出几个来回。 */
  feedbackBusy: boolean
}>()

defineEmits<{
  copy: []
  regenerate: []
  feedback: [value: 'up' | 'down']
  citations: []
}>()
</script>

<template>
  <footer class="actions">
    <button
      class="icon-btn"
      type="button"
      :title="copied ? '已复制' : '复制回答'"
      :aria-label="copied ? '已复制' : '复制回答'"
      @click="$emit('copy')"
    >
      <Icon :name="copied ? 'check' : 'copy'" :size="15" />
    </button>
    <button
      class="icon-btn"
      type="button"
      :title="regenerateTitle"
      aria-label="重新生成"
      :disabled="!canRegenerate"
      @click="$emit('regenerate')"
    >
      <Icon name="refresh" :size="15" />
    </button>
    <button
      class="icon-btn"
      :class="{ active: message.feedback === 'up' }"
      type="button"
      title="有帮助"
      aria-label="有帮助"
      :aria-pressed="message.feedback === 'up'"
      :disabled="feedbackBusy"
      @click="$emit('feedback', 'up')"
    >
      <Icon name="thumb-up" :size="15" />
    </button>
    <button
      class="icon-btn"
      :class="{ active: message.feedback === 'down' }"
      type="button"
      title="没帮助"
      aria-label="没帮助"
      :aria-pressed="message.feedback === 'down'"
      :disabled="feedbackBusy"
      @click="$emit('feedback', 'down')"
    >
      <Icon name="thumb-down" :size="15" />
    </button>
    <span class="spacer"></span>
    <button
      v-if="message.citations.length > 0"
      class="icon-btn citation-jump"
      type="button"
      title="引用来源"
      @click="$emit('citations')"
    >
      <Icon name="quote" :size="15" />
      <span class="jump-count">{{ message.citations.length }}</span>
    </button>
  </footer>
</template>

<style scoped>
.actions {
  display: flex;
  align-items: center;
  gap: 0;
  margin-left: -6px;
}

.spacer {
  flex: 1;
}

.citation-jump {
  gap: 2px;
  width: auto;
  padding: 0 6px;
}

.jump-count {
  font-size: 10.5px;
  font-family: var(--font-mono);
}
</style>

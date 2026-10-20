<script setup lang="ts">
/** 空会话引导：只给标题与建议问法，不写解释。 */
import Icon from './Icon.vue'

const SUGGESTIONS = [
  '这份资料的核心结论是什么？',
  '把流程整理成可执行的步骤',
  '有哪些限制条件和例外？',
]

defineEmits<{ suggest: [text: string] }>()
</script>

<template>
  <div class="welcome">
    <span class="welcome-icon"><Icon name="sparkles" :size="24" /></span>
    <h2>开始提问</h2>
    <div class="suggestions">
      <button
        v-for="suggestion in SUGGESTIONS"
        :key="suggestion"
        class="suggestion"
        type="button"
        @click="$emit('suggest', suggestion)"
      >
        <Icon name="sparkles" :size="14" />
        {{ suggestion }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.welcome {
  margin: auto;
  max-width: 540px;
  text-align: center;
  padding: 24px 8px 48px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.welcome-icon {
  width: 48px;
  height: 48px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-lg);
  background: var(--accent-soft);
  color: var(--accent);
}

.welcome h2 {
  font-size: 20px;
  letter-spacing: -0.02em;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 4px;
}

.suggestion {
  height: 34px;
  padding: 0 11px;
  gap: 6px;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-muted);
  background: var(--bg);
  border-color: var(--border);
  border-radius: var(--radius-full);
}

.suggestion:hover:not(:disabled) {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 40%, transparent);
  background: var(--accent-soft);
}
</style>

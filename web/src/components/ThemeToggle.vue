<script setup lang="ts">
/** 主题切换:浅色 / 深色两档。图标表示**切换后的目标**,不是当前状态。 */
import { computed } from 'vue'

import { currentTheme, toggleTheme } from '../stores/theme'

const next = computed(() => (currentTheme.value === 'dark' ? '浅色' : '深色'))
</script>

<template>
  <button class="theme-toggle" type="button" :title="`切换到${next}主题`" @click="toggleTheme()">
    {{ currentTheme === 'dark' ? '☀' : '☾' }}
    <span class="label">{{ next }}</span>
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.theme-toggle:hover {
  background: var(--bg-sunken);
  color: var(--text);
}

@media (max-width: 720px) {
  .label {
    display: none;
  }
}
</style>

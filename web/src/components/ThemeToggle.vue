<script setup lang="ts">
/** 主题切换：浅色 / 深色两档。图标表示**切换后的目标**，不是当前状态。 */
import { computed } from 'vue'

import { currentTheme, toggleTheme } from '../stores/theme'
import Icon from './Icon.vue'

const next = computed(() => (currentTheme.value === 'dark' ? '浅色' : '深色'))
</script>

<template>
  <button class="theme-toggle" type="button" :title="`切换到${next}主题`" @click="toggleTheme()">
    <Icon :name="currentTheme === 'dark' ? 'sun' : 'moon'" :size="14" />
    <span class="label">{{ next }}</span>
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  font-size: 11.5px;
  cursor: pointer;
}

.theme-toggle:hover {
  border-color: var(--border);
  background: var(--bg-muted);
  color: var(--text);
}

@media (max-width: 720px) {
  .label {
    display: none;
  }

  .theme-toggle {
    width: 30px;
    padding: 0;
    justify-content: center;
  }
}
</style>

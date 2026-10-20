<script setup lang="ts">
import Icon from '../Icon.vue'

withDefaults(
  defineProps<{
    title: string
    hint?: string
  }>(),
  { hint: '' },
)
</script>

<template>
  <div class="empty-state">
    <span class="empty-icon"><Icon name="file" :size="18" /></span>
    <strong>{{ title }}</strong>
    <p v-if="hint">{{ hint }}</p>
    <slot />
  </div>
</template>

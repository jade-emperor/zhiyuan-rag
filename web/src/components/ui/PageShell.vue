<script setup lang="ts">
/** 页面骨架：统一标题、主操作与内容宽度。所有业务页共用。 */
withDefaults(
  defineProps<{
    title: string
    meta?: string
  }>(),
  { meta: '' },
)
</script>

<template>
  <main class="page">
    <div class="page-inner">
      <header class="page-head">
        <div class="page-title">
          <h1>{{ title }}</h1>
          <span v-if="meta" class="page-meta">{{ meta }}</span>
        </div>
        <div class="page-actions">
          <slot name="actions" />
        </div>
      </header>
      <slot />
    </div>
  </main>
</template>

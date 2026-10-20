<script setup lang="ts">
/** 页面骨架：统一标题、导语、主操作与内容宽度。所有业务页共用。 */
withDefaults(
  defineProps<{
    title: string
    meta?: string
    eyebrow?: string
    description?: string
  }>(),
  { meta: '', eyebrow: '', description: '' },
)
</script>

<template>
  <main class="page">
    <div class="page-inner">
      <slot name="before" />
      <header class="page-head">
        <div class="page-heading">
          <p v-if="eyebrow" class="page-eyebrow">{{ eyebrow }}</p>
          <div class="page-title">
            <h1>{{ title }}</h1>
            <span v-if="meta" class="page-meta">{{ meta }}</span>
          </div>
          <p v-if="description" class="page-desc">{{ description }}</p>
        </div>
        <div class="page-actions">
          <slot name="actions" />
        </div>
      </header>
      <slot />
    </div>
  </main>
</template>

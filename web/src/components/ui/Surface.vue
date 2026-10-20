<script setup lang="ts">
/** 内容区块：细边框卡片 + 标题行 + 操作位。页面里的每个功能区用它。 */
defineProps<{
  title?: string
}>()
</script>

<template>
  <section class="surface">
    <header v-if="title || $slots.actions" class="surface-head">
      <h2 v-if="title">{{ title }}</h2>
      <h2 v-else class="sr-only">区块</h2>
      <div class="surface-actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="surface-body">
      <slot />
    </div>
  </section>
</template>

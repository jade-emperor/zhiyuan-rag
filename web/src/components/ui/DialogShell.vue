<script setup lang="ts">
/** 弹层骨架：遮罩 + 对话框 + 标题/说明 + 关闭。三个业务弹层共用。 */
withDefaults(
  defineProps<{
    label: string
    title: string
    size?: 'sm' | 'md' | 'lg'
  }>(),
  { size: 'md' },
)

defineEmits<{ close: [] }>()
</script>

<template>
  <div class="overlay" @click.self="$emit('close')">
    <div class="card dialog" :class="size" role="dialog" :aria-label="label">
      <header>
        <div class="head-text">
          <h3>{{ title }}</h3>
          <p v-if="$slots.subtitle" class="faint"><slot name="subtitle" /></p>
        </div>
        <button class="ghost" type="button" @click="$emit('close')">关闭</button>
      </header>
      <div class="dialog-body">
        <slot />
      </div>
      <footer v-if="$slots.footer" class="dialog-foot">
        <slot name="footer" />
      </footer>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  background: var(--overlay);
  display: grid;
  place-items: center;
  padding: 24px;
  animation: fade 0.14s ease;
}

@keyframes fade {
  from {
    opacity: 0;
  }
}

.dialog {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px;
  max-height: min(86vh, 900px);
  box-shadow: var(--shadow-lg);
}

.dialog.sm {
  width: min(440px, 94vw);
}

.dialog.md {
  width: min(640px, 94vw);
}

.dialog.lg {
  width: min(760px, 96vw);
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-subtle);
}

.head-text {
  min-width: 0;
}

h3 {
  font-size: 15px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

header p {
  margin: 4px 0 0;
}

.dialog-body {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dialog-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}

@media (max-width: 768px) {
  .overlay {
    padding: 10px;
    align-items: end;
  }

  .dialog {
    width: 100% !important;
    max-height: 88vh;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
  }
}
</style>

<script setup lang="ts">
/**
 * 知源助手品牌标识：一卷打开的书页 + 一枚「源点」。
 * 图形跟随主题色，不依赖外部图片资源；尺寸只由调用点给。
 */
withDefaults(defineProps<{ size?: number }>(), { size: 32 })
</script>

<template>
  <span class="mark" :style="{ width: `${size}px`, height: `${size}px` }" aria-hidden="true">
    <svg viewBox="0 0 32 32" fill="none" focusable="false">
      <path
        d="M6.5 9.2c3.5-1.2 6.6-1 9.5 1v14.1c-2.9-2-6-2.2-9.5-1V9.2Z"
        fill="currentColor"
        opacity=".96"
      />
      <path
        d="M25.5 9.2c-3.5-1.2-6.6-1-9.5 1v14.1c2.9-2 6-2.2 9.5-1V9.2Z"
        fill="currentColor"
        opacity=".7"
      />
      <path d="M16 10.2v14.1" stroke="#fff" stroke-opacity=".55" stroke-width="1.2" />
      <circle cx="16" cy="6.4" r="2.4" fill="#fff" />
      <circle cx="16" cy="6.4" r="4.8" stroke="#fff" stroke-opacity=".28" />
    </svg>
  </span>
</template>

<style scoped>
.mark {
  display: inline-grid;
  place-items: center;
  flex: none;
  border-radius: 30%;
  color: #fff;
  background: var(--brand-gradient);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.24),
    var(--shadow-brand);
}

.mark svg {
  width: 72%;
  height: 72%;
  display: block;
}
</style>

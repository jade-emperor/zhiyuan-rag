<script setup lang="ts">
/**
 * 一个指标卡。**必须带分子/分母** —— 只给百分比读不出样本量
 * (3/4 与 300/400 都是 75%,但一个说明不了任何事)。
 */
defineProps<{
  label: string
  value: string
  detail: string
  /** 分母为 0 时不着色:没有样本的比率不是"0%",是"没法算"。 */
  tone?: 'plain' | 'ok' | 'warn' | 'danger'
}>()
</script>

<template>
  <div class="card metric">
    <div class="faint label">{{ label }}</div>
    <div class="value" :class="tone ?? 'plain'">{{ value }}</div>
    <div class="faint detail">{{ detail }}</div>
  </div>
</template>

<style scoped>
.metric {
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.label {
  font-size: 12px;
}

.value {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1.3;
}

.value.ok {
  color: var(--accent);
}

.value.warn {
  color: var(--warn);
}

.value.danger {
  color: var(--danger);
}

.detail {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>

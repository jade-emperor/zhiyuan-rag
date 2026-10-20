<script setup lang="ts">
/**
 * 认证页骨架：左侧品牌说明（桌面）+ 右侧表单，≤900px 收成单列只留表单。
 *
 * 左侧那三行**不是装饰**:登录页是使用者见到这个系统的第一屏,而"它能做什么、
 * 凭什么可信"在这里说清比进去之后再猜便宜。三句话都对得上真实实现
 * (引用可回溯 / 两路召回 + 重排 / 文档级可见范围),没有一句是市场话术。
 */
import ThemeToggle from '../ThemeToggle.vue'
import Icon from '../Icon.vue'

withDefaults(
  defineProps<{
    title: string
    subtitle?: string
  }>(),
  { subtitle: '' },
)
</script>

<template>
  <div class="auth">
    <aside class="auth-brand">
      <div class="brand-line">
        <span class="brand-mark"><Icon name="sparkles" :size="19" /></span>
        RAG Assistant
      </div>

      <div class="brand-body">
        <h2>把企业文档<br />变成可引用的答案</h2>
        <ul class="brand-points">
          <li>
            <span class="point-icon"><Icon name="quote" :size="15" /></span>
            <span class="point-text">
              <strong>答案带引用</strong>
              <span>每句结论都能点回原文那一段</span>
            </span>
          </li>
          <li>
            <span class="point-icon"><Icon name="search" :size="15" /></span>
            <span class="point-text">
              <strong>两路召回</strong>
              <span>向量与关键词并行检索，融合后重排</span>
            </span>
          </li>
          <li>
            <span class="point-icon"><Icon name="users" :size="15" /></span>
            <span class="point-text">
              <strong>可见范围</strong>
              <span>按文档收窄，该谁看的只有谁看得到</span>
            </span>
          </li>
        </ul>
      </div>

      <p class="brand-note">企业知识库 · 内部使用</p>
    </aside>

    <div class="auth-top">
      <ThemeToggle />
    </div>

    <div class="auth-form-wrap">
      <div class="auth-card">
        <div>
          <h1>{{ title }}</h1>
          <p v-if="subtitle" class="auth-sub">{{ subtitle }}</p>
        </div>
        <slot />
      </div>
    </div>
  </div>
</template>

<style scoped>
.brand-body {
  display: flex;
  flex-direction: column;
  gap: 26px;
  max-width: 420px;
}

.brand-body h2 {
  margin: 0;
  font-size: 30px;
  font-weight: 650;
  line-height: 1.32;
  letter-spacing: -0.03em;
  color: var(--text);
}

.brand-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.brand-points li {
  display: flex;
  align-items: flex-start;
  gap: 11px;
}

.point-icon {
  flex: none;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: var(--accent-soft);
  color: var(--accent);
}

.point-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.point-text strong {
  font-size: 13px;
  font-weight: 600;
}

.point-text span {
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-muted);
}

@media (max-height: 720px) {
  .brand-body {
    gap: 18px;
  }

  .brand-body h2 {
    font-size: 25px;
  }
}

@media (max-width: 900px) {
  .auth-top {
    top: 10px;
    right: 12px;
    position: fixed;
  }
}
</style>

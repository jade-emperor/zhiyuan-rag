<script setup lang="ts">
/**
 * 认证页骨架：左侧品牌叙事（桌面）+ 右侧表单，≤900px 收成单列只留表单。
 *
 * 左侧三段都对应真实能力：精准溯源、混合检索、权限隔离。它们不是装饰，
 * 而是使用者进入系统前最需要确认的三件事。
 */
import BrandMark from '../BrandMark.vue'
import Icon from '../Icon.vue'
import ThemeToggle from '../ThemeToggle.vue'

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
        <BrandMark :size="38" />
        <span class="brand-word">
          <strong>知源助手</strong>
          <small>ZHIYUAN INTELLIGENCE</small>
        </span>
      </div>

      <div class="brand-body">
        <p class="eyebrow">企业知识智能平台</p>
        <h2>
          让知识有迹可循<br />
          让答案<span>有据可依</span>
        </h2>
        <p class="brand-lede">
          以企业自有知识为源，融合语义理解与关键词检索，
          在每一次问答中给出可追溯、可验证、可管控的答案。
        </p>

        <ul class="brand-points">
          <li>
            <span class="point-icon"><Icon name="quote" :size="17" /></span>
            <span class="point-text">
              <strong>精准溯源</strong>
              <span>每句结论皆有出处，一键直达原文段落。</span>
            </span>
          </li>
          <li>
            <span class="point-icon"><Icon name="search" :size="17" /></span>
            <span class="point-text">
              <strong>混合检索</strong>
              <span>语义与关键词双引擎并行，精准锁定所需信息。</span>
            </span>
          </li>
          <li>
            <span class="point-icon"><Icon name="users" :size="17" /></span>
            <span class="point-text">
              <strong>权限隔离</strong>
              <span>细粒度文档管控，确保数据“各归其主”。</span>
            </span>
          </li>
        </ul>
      </div>

      <div class="brand-foot">
        <div class="capability-strip" aria-label="核心能力">
          <span><Icon name="check" :size="12" /> 语义检索</span>
          <span><Icon name="check" :size="12" /> 关键词召回</span>
          <span><Icon name="check" :size="12" /> 交叉重排</span>
          <span><Icon name="check" :size="12" /> 权限过滤</span>
        </div>
        <p class="brand-note">知源助手 · 企业知识智能平台</p>
      </div>
    </aside>

    <div class="auth-top">
      <ThemeToggle />
    </div>

    <div class="auth-form-wrap">
      <div class="auth-card">
        <div class="auth-intro">
          <p class="auth-kicker">WELCOME TO ZHIYUAN</p>
          <h1>{{ title }}</h1>
          <p v-if="subtitle" class="auth-sub">{{ subtitle }}</p>
        </div>
        <slot />
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-brand {
  padding: 48px 64px;
}

.brand-line {
  align-items: center;
  gap: 12px;
}

.brand-word {
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: 1.2;
}

.brand-word strong {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.015em;
}

.brand-word small {
  color: var(--text-faint);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}

.brand-body {
  display: flex;
  flex-direction: column;
  gap: 22px;
  max-width: 560px;
}

.eyebrow {
  margin: 0;
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.18em;
}

.brand-body h2 {
  margin: 0;
  font-size: clamp(32px, 3.2vw, 44px);
  font-weight: 650;
  line-height: 1.28;
  letter-spacing: -0.04em;
}

.brand-body h2 span {
  color: var(--accent);
}

.brand-lede {
  max-width: 500px;
  margin: -8px 0 2px;
  color: var(--text-muted);
  font-size: 13.5px;
  line-height: 1.8;
}

.brand-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.brand-points li {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--border) 78%, transparent);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--bg) 68%, transparent);
  backdrop-filter: blur(8px);
}

.point-icon {
  flex: none;
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border-radius: 11px;
  background: var(--accent-soft);
  color: var(--accent);
}

.point-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.point-text strong {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.point-text span {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-muted);
}

.brand-foot {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.capability-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.capability-strip span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--bg) 64%, transparent);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 500;
}

.capability-strip svg {
  color: var(--accent);
}

.auth-card {
  width: min(410px, 100%);
  padding: 34px 32px 30px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  background: color-mix(in srgb, var(--bg-elevated) 94%, transparent);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(18px);
}

.auth-intro {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.auth-kicker {
  margin: 0;
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.16em;
}

.auth-card h1 {
  font-size: 28px;
  font-weight: 650;
  letter-spacing: -0.035em;
}

.auth-card .auth-sub {
  margin: 0;
  color: var(--text-faint);
  font-size: 12.5px;
  line-height: 1.65;
}

.auth-card :deep(.field) {
  gap: 7px;
}

.auth-card :deep(.field > span) {
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 550;
}

.auth-card :deep(.field input) {
  height: 40px;
  border-color: var(--border-strong);
  border-radius: var(--radius-sm);
  font-size: 13.5px;
}

.auth-card :deep(.auth-foot) {
  padding-top: 15px;
}

@media (max-height: 780px) {
  .auth-brand {
    padding: 36px 48px;
  }

  .brand-body {
    gap: 14px;
  }

  .brand-lede {
    display: none;
  }

  .brand-points li {
    padding: 9px 12px;
  }

  .brand-body h2 {
    font-size: 32px;
  }
}

@media (max-width: 900px) {
  .auth-top {
    top: 10px;
    right: 12px;
    position: fixed;
  }

  .auth-card {
    padding: 28px 22px 24px;
  }
}
</style>

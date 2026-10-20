<script setup lang="ts">
/** 空会话引导：把“能问什么、基于什么回答”一次说清，而不是只留一个输入框。 */
import { computed } from 'vue'

import { paramsState } from '../stores/params'
import { sourcesState } from '../stores/sources'
import BrandMark from './BrandMark.vue'
import Icon from './Icon.vue'

const params = paramsState()
const sources = sourcesState()

const SUGGESTIONS = [
  {
    kicker: '快速了解',
    text: '这份资料的核心结论是什么？',
    detail: '提炼要点，并标注出处',
  },
  {
    kicker: '流程梳理',
    text: '把关键流程整理成可执行清单',
    detail: '按步骤组织，保留原文依据',
  },
  {
    kicker: '风险核查',
    text: '有哪些限制条件与例外？',
    detail: '定位条款，避免遗漏关键约束',
  },
]

const modeLabel = computed(() => {
  if (params.mode === 'vector') return '语义检索'
  if (params.mode === 'keyword') return '关键词检索'
  return '混合检索'
})

const scope = computed(() => params.source_filter ?? '全部知识库')

defineEmits<{ suggest: [text: string] }>()
</script>

<template>
  <div class="welcome">
    <div class="welcome-grid">
      <section class="welcome-intro">
        <div class="welcome-brand">
          <BrandMark :size="34" />
          <span>知源 · 知识问答</span>
        </div>

        <p class="welcome-eyebrow">KNOWLEDGE INTELLIGENCE</p>
        <h2>
          从一个问题开始，<br />
          找到<span>有依据的答案</span>
        </h2>
        <p class="welcome-lede">
          基于企业知识资产进行语义理解与交叉检索。每一条结论都能回到原文，
          让判断有出处，让协作更高效。
        </p>

        <div class="context-row" aria-label="当前检索上下文">
          <span class="context-chip">
            <Icon name="book" :size="13" />
            {{ scope }}
          </span>
          <span class="context-chip">
            <Icon name="search" :size="13" />
            {{ modeLabel }}
          </span>
          <span v-if="sources.items.length > 0" class="context-chip">
            {{ sources.items.length }} 个知识来源
          </span>
        </div>
      </section>

      <section class="prompt-panel" aria-label="建议问法">
        <header class="panel-head">
          <div>
            <span class="panel-kicker">建议问法</span>
            <strong>从真实工作场景出发</strong>
          </div>
          <Icon name="sparkles" :size="17" />
        </header>

        <button
          v-for="suggestion in SUGGESTIONS"
          :key="suggestion.text"
          class="prompt-card"
          type="button"
          @click="$emit('suggest', suggestion.text)"
        >
          <span class="prompt-copy">
            <span class="prompt-kicker">{{ suggestion.kicker }}</span>
            <strong>{{ suggestion.text }}</strong>
            <span class="prompt-detail">{{ suggestion.detail }}</span>
          </span>
          <Icon class="prompt-arrow" name="chevron-right" :size="15" />
        </button>
      </section>
    </div>

    <footer class="welcome-foot">
      <span><Icon name="quote" :size="13" /> 回答附带原文引用</span>
      <span><Icon name="search" :size="13" /> 语义与关键词混合检索</span>
      <span><Icon name="users" :size="13" /> 按文档权限隔离</span>
    </footer>
  </div>
</template>

<style scoped>
.welcome {
  width: 100%;
  max-width: 1060px;
  margin: auto;
  padding: 32px 4px 44px;
  display: flex;
  flex-direction: column;
  gap: 28px;
}

.welcome-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(320px, 0.92fr);
  gap: clamp(28px, 4vw, 56px);
  align-items: center;
}

.welcome-intro {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 14px;
  min-width: 0;
}

.welcome-brand {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
}

.welcome-eyebrow {
  margin: 8px 0 0;
  color: var(--accent);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.17em;
}

.welcome-intro h2 {
  margin: 0;
  font-size: clamp(30px, 3.1vw, 42px);
  font-weight: 650;
  line-height: 1.24;
  letter-spacing: -0.045em;
}

.welcome-intro h2 span {
  color: var(--accent);
}

.welcome-lede {
  max-width: 480px;
  margin: 0;
  color: var(--text-muted);
  font-size: 13.5px;
  line-height: 1.8;
}

.context-row {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 4px;
}

.context-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--bg) 74%, transparent);
  color: var(--text-muted);
  font-size: 11.5px;
  font-weight: 500;
}

.context-chip svg {
  color: var(--accent);
}

.prompt-panel {
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 5%, transparent), transparent 46%),
    color-mix(in srgb, var(--bg) 92%, transparent);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 2px 2px 8px;
  color: var(--accent);
}

.panel-head > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.panel-kicker {
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.panel-head strong {
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}

.prompt-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  height: auto;
  padding: 11px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: var(--bg);
  text-align: left;
  white-space: normal;
  transition:
    border-color 0.15s ease,
    background 0.15s ease,
    transform 0.15s ease;
}

.prompt-card:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  background: var(--accent-soft);
  transform: translateX(2px);
}

.prompt-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.prompt-kicker {
  color: var(--text-faint);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
}

.prompt-copy strong {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 550;
  line-height: 1.45;
  white-space: normal;
}

.prompt-detail {
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.45;
}

.prompt-arrow {
  flex: none;
  color: var(--text-faint);
}

.prompt-card:hover .prompt-arrow {
  color: var(--accent);
}

.welcome-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 9px 22px;
  padding-top: 18px;
  border-top: 1px solid var(--border-subtle);
  color: var(--text-faint);
  font-size: 11px;
}

.welcome-foot span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.welcome-foot svg {
  color: var(--accent);
}

@media (max-width: 980px) {
  .welcome-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 24px;
  }

  .welcome {
    max-width: 640px;
    padding-top: 20px;
  }

  .welcome-lede {
    max-width: none;
  }
}

@media (max-width: 640px) {
  .welcome {
    padding: 12px 2px 20px;
    gap: 16px;
  }

  .welcome-intro {
    gap: 9px;
  }

  .welcome-intro h2 {
    font-size: 27px;
  }

  .welcome-eyebrow {
    margin-top: 2px;
  }

  .welcome-lede {
    display: none;
  }

  .prompt-panel {
    padding: 12px;
    gap: 6px;
  }

  .prompt-card {
    padding: 9px 10px;
  }

  .prompt-detail {
    display: none;
  }

  .welcome-foot {
    display: none;
  }
}
</style>

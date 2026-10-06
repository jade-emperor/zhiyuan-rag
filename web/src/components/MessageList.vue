<script setup lang="ts">
/**
 * 消息区。
 *
 * 四种"不是正常答案"的状态各自有话术,**不合并成一句"出错了"**:
 * 拒答(知识库里没有)、截断(模型没说完)、用户停止、传输错误 ——
 * 它们的下一步动作完全不同(换问法 / 看完整答案 / 重问 / 查后端)。
 */
import { computed } from 'vue'

import type { ChatMessage } from '../stores/chat'
import CitationItem from './CitationItem.vue'

const props = defineProps<{ messages: ChatMessage[] }>()

const channelLabel: Record<string, string> = {
  faq: 'FAQ 直答',
  rag: '知识库',
  greeting: '寒暄',
}

function badge(message: ChatMessage): string {
  if (message.channel === null) return ''
  return channelLabel[message.channel] ?? message.channel
}

const hasMessages = computed(() => props.messages.length > 0)
</script>

<template>
  <div class="messages">
    <div v-if="!hasMessages" class="welcome">
      <h2>问点什么吧</h2>
      <p class="muted">
        答案会带引用与页码。右侧可以调检索方式、融合权重与重排开关 —— 改动只影响你接下来的提问。
      </p>
    </div>

    <div v-for="message in messages" :key="message.id" class="row" :class="message.role">
      <div class="avatar">{{ message.role === 'user' ? '🙋' : '🍜' }}</div>
      <div class="bubble card">
        <div v-if="message.role === 'assistant' && badge(message)" class="head">
          <span class="tag info">{{ badge(message) }}</span>
        </div>

        <div class="text">{{ message.text }}<span v-if="message.streaming" class="caret">▍</span></div>

        <p v-if="message.role === 'assistant' && message.text === '' && message.streaming" class="muted">
          <span class="spin"></span> 正在检索并生成…
        </p>

        <div v-if="message.citations.length > 0" class="citations">
          <div class="faint head-line">依据 {{ message.citations.length }} 条</div>
          <CitationItem v-for="item in message.citations" :key="item.index" :citation="item" />
        </div>

        <p v-if="message.refused" class="alert info">拒答:知识库里没有能支撑这个问题的资料。</p>
        <p v-if="message.truncated" class="alert error">回答被截断(达到长度上限),不要当成完整答案。</p>
        <p v-if="message.stopped" class="alert info">已停止生成 —— 上面是已经收到的部分。</p>
        <p v-if="message.error" class="alert error">{{ message.error }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 0;
}

.welcome {
  margin: auto;
  text-align: center;
  max-width: 30rem;
}

.row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.row.user {
  flex-direction: row-reverse;
}

.avatar {
  width: 28px;
  height: 28px;
  flex: none;
  display: grid;
  place-items: center;
}

.bubble {
  padding: 10px 12px;
  max-width: min(46rem, 82%);
}

.row.user .bubble {
  background: var(--bg-sunken);
  border-color: var(--border);
}

.head {
  margin-bottom: 6px;
}

.text {
  white-space: pre-wrap;
  word-break: break-word;
}

.caret {
  animation: blink 1s step-start infinite;
  color: var(--accent);
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.citations {
  margin-top: 10px;
}

.head-line {
  margin-bottom: 2px;
}
</style>

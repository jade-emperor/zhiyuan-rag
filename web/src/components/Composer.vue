<script setup lang="ts">
/**
 * 输入框。回车发送、Shift+回车换行;生成中可以「停止」。
 *
 * 「停止」不是摆设:它断开这条 WS,而后端会把这一轮记成 `aborted` 进延迟账
 * (`/api/v1/reports/metrics` 的那个计数)—— 所以"用户没耐心了"这件事是**可观测的**,
 * 不是消失在日志里的一个断连接。
 */
import { computed, ref } from 'vue'

import { chatState, send, stop } from '../stores/chat'

const chat = chatState()
const draft = ref('')

const canSend = computed(() => draft.value.trim().length > 0 && !chat.generating)

async function submit(): Promise<void> {
  if (!canSend.value) return
  const text = draft.value
  draft.value = ''
  await send(text)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void submit()
  }
}
</script>

<template>
  <div class="composer">
    <div class="box card">
      <textarea
        v-model="draft"
        rows="1"
        placeholder="问点什么…(回车发送,Shift+回车换行)"
        :disabled="chat.generating"
        @keydown="onKeydown"
      ></textarea>
      <button v-if="chat.generating" class="ghost" type="button" @click="stop">停止</button>
      <button v-else class="primary" type="button" :disabled="!canSend" @click="submit">发送</button>
    </div>
    <p v-if="chat.notice" class="alert error">{{ chat.notice }}</p>
  </div>
</template>

<style scoped>
.composer {
  flex: none;
  padding: 0 24px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.box {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 8px;
}

textarea {
  border: none;
  resize: none;
  max-height: 8rem;
  min-height: 1.8rem;
  background: transparent;
}

textarea:focus {
  outline: none;
}

.box button {
  flex: none;
  margin-bottom: 2px;
}
</style>

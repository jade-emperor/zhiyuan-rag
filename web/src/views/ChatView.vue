<script setup lang="ts">
/**
 * 聊天页:三栏 —— 会话列表 / 消息区 / 参数面板(对齐作者给的参考图)。
 *
 * 数据来源一眼看清(免得后来人以为还有第五个接口):
 * 会话列表 → `GET /api/v1/sessions`;切换会话 → `GET /api/v1/history/{id}`;
 * 提问 → `WS /api/v1/stream`;来源与参数选项 → `GET /api/v1/sources` + `capabilities`。
 */
import { onMounted } from 'vue'

import Composer from '../components/Composer.vue'
import ConfigPanel from '../components/ConfigPanel.vue'
import MessageList from '../components/MessageList.vue'
import SessionList from '../components/SessionList.vue'
import { chatState } from '../stores/chat'
import { capabilitiesState, loadCapabilities } from '../stores/capabilities'

const chat = chatState()
const caps = capabilitiesState()

onMounted(async () => {
  if (caps.data === null) await loadCapabilities()
})
</script>

<template>
  <div class="chat">
    <SessionList />
    <main class="main">
      <MessageList :messages="chat.messages" />
      <Composer />
    </main>
    <ConfigPanel />
  </div>
</template>

<style scoped>
.chat {
  flex: 1;
  display: flex;
  min-height: 0;
}

.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-muted);
  min-width: 0;
  min-height: 0;
}
</style>

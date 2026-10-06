<script setup lang="ts">
/**
 * 登录页。
 *
 * 两条刻意的事:
 * 1. 页面上就能改**后端地址** —— 这个 app 只依赖它,而"连不上"与"密码不对"
 *    必须让用户分得清(所以错误提示来自 capabilities 探测,不是笼统的一句"登录失败");
 * 2. **没有注册入口**:账号由管理员用 `project-rag user add` 建(后端也没有注册接口)——
 *    前端不给一个点了必然 404 的按钮。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { backendBase, setBackendBase } from '../api/client'
import { login, authState } from '../stores/auth'
import { capabilitiesState, loadCapabilities } from '../stores/capabilities'

const router = useRouter()
const route = useRoute()
const auth = authState()
const caps = capabilitiesState()

const username = ref('')
const password = ref('')
const backendDraft = ref(backendBase())
const showBackend = ref(false)

const backendOk = computed(() => caps.data !== null && caps.error === '')

onMounted(async () => {
  if (caps.data === null) await loadCapabilities()
})

async function connect(): Promise<void> {
  setBackendBase(backendDraft.value)
  showBackend.value = false
  await loadCapabilities()
}

async function submit(): Promise<void> {
  if (username.value.trim().length === 0 || password.value.length === 0) {
    auth.error = '用户名与密码都要填'
    return
  }
  try {
    await login(username.value.trim(), password.value)
    const next = typeof route.query.next === 'string' ? route.query.next : '/chat'
    await router.replace(next.startsWith('/') ? next : '/chat')
  } catch {
    /* 错误已经写进 auth.error,这里不再抛(模板会显示) */
  }
}
</script>

<template>
  <div class="page">
    <div class="card login">
      <h1>🍜 RAG Assistant</h1>
      <p class="muted">课程知识库问答。员工可检索;管理员还能管文档与看观测报表。</p>

      <label class="field">
        <span>用户名</span>
        <input v-model="username" type="text" autocomplete="username" @keydown.enter="submit" />
      </label>
      <label class="field">
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password" @keydown.enter="submit" />
      </label>

      <p v-if="auth.error" class="alert error">{{ auth.error }}</p>

      <button class="primary block" type="button" :disabled="auth.loading" @click="submit">
        {{ auth.loading ? '登录中…' : '登录' }}
      </button>

      <p class="faint">没有注册入口:账号由管理员用 <code>project-rag user add</code> 创建。</p>

      <div class="backend">
        <button class="ghost small" type="button" @click="showBackend = !showBackend">
          后端 {{ backendBase() || '未配置' }} · {{ backendOk ? '已连通' : '未连通' }}
        </button>
        <div v-if="showBackend" class="backend-box">
          <input v-model="backendDraft" type="text" placeholder="http://主机:端口" />
          <button class="primary small" type="button" @click="connect">连接</button>
        </div>
        <p v-if="caps.error" class="alert error small">{{ caps.error }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 24px;
}

.login {
  width: 380px;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

h1 {
  font-size: 18px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.block {
  width: 100%;
}

.backend {
  border-top: 1px solid var(--border);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.backend-box {
  display: flex;
  gap: 6px;
}

.small {
  font-size: 12px;
  padding: 4px 8px;
}
</style>

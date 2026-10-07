<script setup lang="ts">
/**
 * 顶部导航:品牌、页签、后端地址、当前账号。
 *
 * 两件刻意的事:
 * 1. **管理页签只对管理员渲染** —— 员工看不到「知识库」「可观测」。但这不是权限,
 *    后端对那两个接口各自有 `require_admin`(前端藏起来只是不给点必然 403 的按钮);
 * 2. **后端地址放在这里**(而不是藏在设置页深处):它是这个 app 唯一的环境,
 *    填错了连登录都进不去,必须一眼可见、一点可改。
 */
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { backendBase, setBackendBase } from '../api/client'
import { authState, isAdmin, logout } from '../stores/auth'
import { loadCapabilities } from '../stores/capabilities'
import { resetChat } from '../stores/chat'

const router = useRouter()
const auth = authState()

const editingBackend = ref(false)
const backendDraft = ref(backendBase())
const backendError = ref('')

const backendLabel = computed(() => {
  const base = backendBase()
  try {
    return new URL(base).host
  } catch {
    return base || '未配置'
  }
})

async function saveBackend(): Promise<void> {
  const next = backendDraft.value.trim()
  if (next.length === 0) {
    backendError.value = '后端地址不能为空(形如 http://主机:端口)'
    return
  }
  setBackendBase(next)
  // 换了后端 = 换了另一套账号体系:本地令牌一定不属于它,先清干净再重新拉能力。
  // 不清的话,下一步会拿着 A 的令牌去问 B,而用户看到的只是"登录过期"。
  await logout()
  resetChat()
  backendError.value = ''
  editingBackend.value = false
  const data = await loadCapabilities()
  if (data === null) {
    backendError.value = '连不上这个地址,请检查后端是否在跑'
    editingBackend.value = true
    return
  }
  await router.replace({ name: 'login' })
}

async function signOut(): Promise<void> {
  await logout()
  resetChat()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <header class="header">
    <div class="brand">
      <span class="logo">🍜</span>
      <strong>RAG Assistant</strong>
    </div>

    <nav class="nav">
      <RouterLink class="tab" :to="{ name: 'chat' }">对话</RouterLink>
      <RouterLink v-if="isAdmin" class="tab" :to="{ name: 'knowledge' }">知识库</RouterLink>
      <RouterLink v-if="isAdmin" class="tab" :to="{ name: 'metrics' }">可观测</RouterLink>
    </nav>

    <div class="right">
      <div class="backend">
        <button class="chip" type="button" @click="editingBackend = !editingBackend">
          <span class="dot" :class="{ ok: backendError === '' }"></span>
          后端 {{ backendLabel }}
        </button>
        <div v-if="editingBackend" class="backend-panel card">
          <label for="backend-input">RAG 服务地址(app 只依赖它)</label>
          <input id="backend-input" v-model="backendDraft" type="text" placeholder="http://主机:端口" />
          <p class="faint">换地址会清掉当前登录状态 —— 那是另一套账号体系。</p>
          <p v-if="backendError" class="alert error">{{ backendError }}</p>
          <div class="backend-actions">
            <button class="ghost" type="button" @click="editingBackend = false">取消</button>
            <button class="primary" type="button" @click="saveBackend">保存并连接</button>
          </div>
        </div>
      </div>

      <span class="who">
        {{ auth.user?.username ?? auth.username }}
        <span class="tag" :class="isAdmin ? 'info' : 'soft'">{{ isAdmin ? '管理员' : '员工' }}</span>
      </span>
      <button class="ghost" type="button" @click="signOut()">退出</button>
    </div>
  </header>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  gap: 24px;
  height: var(--header-h);
  padding: 0 16px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  flex: none;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
}

.logo {
  font-size: 18px;
}

.nav {
  display: flex;
  gap: 4px;
  flex: 1;
}

.tab {
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  text-decoration: none;
}

.tab:hover {
  background: var(--bg-sunken);
}

.tab.router-link-active {
  background: var(--bg-sunken);
  color: var(--text);
  font-weight: 600;
}

.right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.backend {
  position: relative;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
}

.dot.ok {
  background: var(--accent);
}

.backend-panel {
  position: absolute;
  right: 0;
  top: 34px;
  width: 320px;
  padding: 12px;
  z-index: 20;
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.backend-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.who {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: 13px;
}
</style>

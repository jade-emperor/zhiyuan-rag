<script setup lang="ts">
/**
 * 登录页。页面上就能改后端地址:这个 app 只依赖它,而「连不通」与「密码不对」必须让用户分得清。
 * 自助注册入口只在后端 `capabilities.registration.enabled` 为真时显示。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { backendBase, setBackendBase, storedUsername } from '../api/client'
import AuthShell from '../components/ui/AuthShell.vue'
import { login, authState } from '../stores/auth'
import { capabilitiesState, loadCapabilities } from '../stores/capabilities'

const router = useRouter()
const route = useRoute()
const auth = authState()
const caps = capabilitiesState()

const username = ref(storedUsername() ?? '')
const password = ref('')
const backendDraft = ref(backendBase())
const showBackend = ref(false)
const showPassword = ref(false)
const fieldErrors = reactive<{ username: string; password: string }>({ username: '', password: '' })

const backendOk = computed(() => caps.data !== null && caps.error === '')
const canRegister = computed(() => caps.data?.registration.enabled === true)

onMounted(async () => {
  if (caps.data === null) await loadCapabilities()
})

async function connect(): Promise<void> {
  setBackendBase(backendDraft.value)
  showBackend.value = false
  await loadCapabilities()
}

async function submit(): Promise<void> {
  fieldErrors.username = username.value.trim().length === 0 ? '请填写用户名' : ''
  fieldErrors.password = password.value.length === 0 ? '请填写密码' : ''
  if (fieldErrors.username !== '' || fieldErrors.password !== '') {
    auth.error = ''
    return
  }
  try {
    await login(username.value.trim(), password.value)
    const next = typeof route.query.next === 'string' ? route.query.next : '/chat'
    await router.replace(next.startsWith('/') ? next : '/chat')
  } catch {
    /* 错误已写入 auth.error,模板负责显示 */
  }
}
</script>

<template>
  <AuthShell class="login" title="登录">
    <label class="field">
      <span>用户名</span>
      <input v-model="username" type="text" autocomplete="username" @keydown.enter="submit" />
      <small v-if="fieldErrors.username" class="alert error small">{{ fieldErrors.username }}</small>
    </label>

    <label class="field">
      <span>密码</span>
      <div class="secret">
        <input
          v-model="password"
          :type="showPassword ? 'text' : 'password'"
          autocomplete="current-password"
          @keydown.enter="submit"
        />
        <button class="ghost small" type="button" @click="showPassword = !showPassword">
          {{ showPassword ? '隐藏' : '显示' }}
        </button>
      </div>
      <small v-if="fieldErrors.password" class="alert error small">{{ fieldErrors.password }}</small>
    </label>

    <p v-if="auth.error" class="alert error">{{ auth.error }}</p>

    <button class="primary login-btn" type="button" :disabled="auth.loading" @click="submit">
      {{ auth.loading ? '登录中…' : '登录' }}
    </button>

    <p v-if="canRegister" class="auth-foot">
      <span>还没有账号?</span>
      <RouterLink :to="{ name: 'register' }">创建账号</RouterLink>
    </p>
    <p v-else-if="caps.data !== null" class="auth-foot">账号由管理员创建</p>

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
  </AuthShell>
</template>

<style scoped>
.secret {
  display: flex;
  gap: 6px;
}

.secret input {
  flex: 1;
}

.login-btn {
  width: 100%;
  height: 38px;
  margin-top: 2px;
}

.backend {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 2px;
}

.backend-box {
  display: flex;
  gap: 6px;
}

.small {
  font-size: 11px;
  height: 22px;
  padding: 0 6px;
}
</style>

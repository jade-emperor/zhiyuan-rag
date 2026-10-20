<script setup lang="ts">
/**
 * 注册页:验证码 + 口令策略 + 可选邀请码。
 *
 * 能不能注册、要不要验证码、最小口令长度都来自 `capabilities.registration`,
 * 不在前端写死;服务端关掉自助注册时这里直接给出说明,而不是渲染一个必然 403 的表单。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { captcha as fetchCaptcha, register } from '../api/endpoints'
import AuthShell from '../components/ui/AuthShell.vue'
import { capabilitiesState, loadCapabilities } from '../stores/capabilities'
import { setSession } from '../stores/auth'

const router = useRouter()
const caps = capabilitiesState()

const username = ref('')
const password = ref('')
const confirm = ref('')
const invite = ref('')
const code = ref('')
const ticket = ref('')
const image = ref('')
const loading = ref(false)
const error = ref('')
const captchaLoading = ref(false)

const policy = computed(() => caps.data?.registration ?? null)
const enabled = computed(() => policy.value?.enabled === true)
const needInvite = computed(() => policy.value?.invite_required === true)
const minChars = computed(() => policy.value?.min_password_chars ?? 0)

async function refreshCaptcha(): Promise<void> {
  if (policy.value?.captcha_required !== true) return
  captchaLoading.value = true
  try {
    const result = await fetchCaptcha()
    ticket.value = result.ticket
    image.value = result.image
    code.value = ''
    error.value = ''
  } catch (failure) {
    error.value = (failure as Error).message
  } finally {
    captchaLoading.value = false
  }
}

onMounted(async () => {
  if (caps.data === null) await loadCapabilities()
  await refreshCaptcha()
})

async function submit(): Promise<void> {
  error.value = ''
  if (username.value.trim().length === 0) {
    error.value = '请填写登录名'
    return
  }
  if (password.value !== confirm.value) {
    error.value = '两次输入的密码不一致'
    return
  }
  if (policy.value?.captcha_required === true && code.value.trim().length === 0) {
    error.value = '请填写验证码'
    return
  }
  loading.value = true
  try {
    const result = await register({
      username: username.value.trim(),
      password: password.value,
      captcha_ticket: ticket.value,
      captcha_answer: code.value.trim(),
      invite_code: invite.value.trim(),
    })
    await setSession(result.token, result.username)
    await router.replace({ name: 'chat' })
  } catch (failure) {
    error.value = (failure as Error).message
    // 验证码一次性:失败后必须换一张,否则重试必然还是同一个错误
    await refreshCaptcha()
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthShell title="创建账号">
    <p v-if="policy === null" class="faint">读取中…</p>
    <p v-else-if="!enabled" class="alert warn">自助注册未开放，请联系管理员。</p>

    <template v-else>
      <label class="field">
        <span>登录名</span>
        <input v-model="username" type="text" autocomplete="username" maxlength="32" />
      </label>

      <label class="field">
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="new-password" />
        <small class="faint">≥ {{ minChars }} 位，含字母和数字</small>
      </label>

      <label class="field">
        <span>确认密码</span>
        <input v-model="confirm" type="password" autocomplete="new-password" @keydown.enter="submit" />
      </label>

      <label v-if="needInvite" class="field">
        <span>邀请码</span>
        <input v-model="invite" type="text" autocomplete="off" />
      </label>

      <div v-if="policy.captcha_required" class="field">
        <span>验证码</span>
        <div class="captcha">
          <input v-model="code" type="text" maxlength="8" autocomplete="off" @keydown.enter="submit" />
          <button type="button" class="captcha-img" title="换一张" @click="refreshCaptcha()">
            <img v-if="image" :src="image" alt="验证码" />
            <span v-else class="faint">{{ captchaLoading ? '加载中…' : '获取' }}</span>
          </button>
        </div>
      </div>

      <p v-if="error" class="alert error">{{ error }}</p>

      <button class="primary register-btn" type="button" :disabled="loading" @click="submit">
        {{ loading ? '创建中…' : '创建账号' }}
      </button>
    </template>

    <p class="auth-foot">
      <span>已有账号?</span>
      <RouterLink :to="{ name: 'login' }">去登录</RouterLink>
    </p>
  </AuthShell>
</template>

<style scoped>
.captcha {
  display: flex;
  gap: 8px;
}

.captcha-img {
  width: 118px;
  height: 36px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-muted);
  cursor: pointer;
  overflow: hidden;
  flex: none;
}

.captcha-img img {
  width: 100%;
  height: 100%;
  display: block;
}

.register-btn {
  width: 100%;
  height: 38px;
  margin-top: 2px;
}
</style>

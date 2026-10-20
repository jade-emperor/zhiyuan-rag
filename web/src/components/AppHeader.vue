<script setup lang="ts">
/**
 * 顶部导航：品牌、页签、服务地址与当前账号。
 *
 * 两件刻意的事：
 *
 * 1. **页签按能力渲染**：「对话」要 `can_use_knowledge`、「报表」要
 *    `can_view_reports`、「知识库 / 账号」要 `is_admin`。运维因此看到「对话 + 报表」，
 *    员工看到「对话 + 知识库(只读清单)」。**这不是权限** —— 后端每条路由各自有
 *    `require_reports` / `require_knowledge` / `require_admin`，前端藏起来只是不给点
 *    一个必然 403 的按钮；
 * 2. **服务地址放在这里**（而不是藏在设置页深处）：它是这个 app 唯一的环境，
 *    填错了连登录都进不去，必须一眼可见、一点可改。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { backendBase, setBackendBase } from '../api/client'
import {
  authState,
  canUseKnowledge,
  canViewReports,
  isAdmin,
  logout,
  roleLabel,
} from '../stores/auth'
import { loadCapabilities } from '../stores/capabilities'
import { resetChat } from '../stores/chat'
import { changePassword } from '../stores/auth'
import BrandMark from './BrandMark.vue'
import ThemeToggle from './ThemeToggle.vue'

const router = useRouter()
const auth = authState()

const editingBackend = ref(false)
const editingPassword = ref(false)
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passwordError = ref('')
const passwordDone = ref(false)
const passwordBusy = ref(false)
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

const displayName = computed(() => auth.user?.username ?? auth.username)
const initial = computed(() => (displayName.value || '知').slice(0, 1).toUpperCase())

async function saveBackend(): Promise<void> {
  const next = backendDraft.value.trim()
  if (next.length === 0) {
    backendError.value = '服务地址不能为空（形如 http://主机:端口）'
    return
  }
  setBackendBase(next)
  // 换了后端 = 换了另一套账号体系：本地令牌一定不属于它，先清干净再重新拉能力。
  // 不清的话，下一步会拿着 A 的令牌去问 B，而用户看到的只是"登录过期"。
  await logout()
  resetChat()
  backendError.value = ''
  editingBackend.value = false
  const data = await loadCapabilities()
  if (data === null) {
    backendError.value = '无法连接该地址，请确认服务是否已启动'
    editingBackend.value = true
    return
  }
  await router.replace({ name: 'login' })
}

/** Esc 关掉任一浮层：顶栏上的面板没有遮罩，不给键盘出口就只能点一下别处。 */
function closePanels(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  if (!editingBackend.value && !editingPassword.value) return
  editingBackend.value = false
  editingPassword.value = false
}

onMounted(() => window.addEventListener('keydown', closePanels))
onUnmounted(() => window.removeEventListener('keydown', closePanels))

function togglePasswordPanel(): void {
  editingPassword.value = !editingPassword.value
  editingBackend.value = false
  passwordError.value = ''
  passwordDone.value = false
}

async function savePassword(): Promise<void> {
  passwordError.value = ''
  passwordDone.value = false
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = '两次输入的新密码不一致'
    return
  }
  passwordBusy.value = true
  try {
    await changePassword(oldPassword.value, newPassword.value)
    passwordDone.value = true
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
  } catch (failure) {
    passwordError.value = (failure as Error).message
  } finally {
    passwordBusy.value = false
  }
}

async function signOut(): Promise<void> {
  await logout()
  resetChat()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <header class="header">
    <RouterLink class="brand" :to="{ name: 'chat' }" aria-label="知源助手首页">
      <BrandMark :size="34" />
      <span class="brand-copy">
        <strong>知源助手</strong>
        <small>企业知识智能平台</small>
      </span>
    </RouterLink>

    <nav class="nav">
      <RouterLink v-if="canUseKnowledge" class="tab" :to="{ name: 'chat' }">对话</RouterLink>
      <!-- 知识库这一页要 manage(页面上有上传/删除/授权)：页签必须与**那一页的要求**一致，
           否则员工看到一个点进去就被守卫弹回来的入口。 -->
      <RouterLink v-if="isAdmin" class="tab" :to="{ name: 'knowledge' }">知识库</RouterLink>
      <RouterLink v-if="canViewReports" class="tab" :to="{ name: 'reports' }">报表</RouterLink>
      <RouterLink v-if="isAdmin" class="tab" :to="{ name: 'accounts' }">账号与权限</RouterLink>
    </nav>

    <div class="right">
      <div class="backend">
        <button class="chip" type="button" title="服务连接" @click="editingBackend = !editingBackend">
          <span class="dot" :class="{ ok: backendError === '' }"></span>
          <span class="chip-label">{{ backendLabel }}</span>
        </button>
        <div v-if="editingBackend" class="backend-panel card">
          <div class="panel-title">
            <strong>服务连接</strong>
            <span class="faint">知源助手只依赖此地址</span>
          </div>
          <label for="backend-input">服务地址</label>
          <input id="backend-input" v-model="backendDraft" type="text" placeholder="http://主机:端口" />
          <p v-if="backendError" class="alert error">{{ backendError }}</p>
          <div class="backend-actions">
            <button class="ghost" type="button" @click="editingBackend = false">取消</button>
            <button class="primary" type="button" @click="saveBackend">保存并连接</button>
          </div>
        </div>
      </div>

      <div class="password">
        <button class="chip" type="button" @click="togglePasswordPanel()">修改密码</button>
        <div v-if="editingPassword" class="backend-panel card">
          <div class="panel-title">
            <strong>账号安全</strong>
            <span class="faint">更新后其他设备需重新登录</span>
          </div>
          <label for="old-password">当前密码</label>
          <input id="old-password" v-model="oldPassword" type="password" autocomplete="current-password" />
          <label for="new-password">新密码</label>
          <input id="new-password" v-model="newPassword" type="password" autocomplete="new-password" />
          <label for="confirm-password">确认新密码</label>
          <input id="confirm-password" v-model="confirmPassword" type="password" autocomplete="new-password" />
          <p v-if="passwordError" class="alert error">{{ passwordError }}</p>
          <p v-if="passwordDone" class="alert ok">密码已更新，其他设备需要重新登录。</p>
          <div class="backend-actions">
            <button class="ghost" type="button" @click="editingPassword = false">关闭</button>
            <button class="primary" type="button" :disabled="passwordBusy" @click="savePassword">
              {{ passwordBusy ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>

      <ThemeToggle />

      <div class="who">
        <span class="avatar" aria-hidden="true">{{ initial }}</span>
        <span class="who-copy">
          <span class="who-name">{{ displayName }}</span>
          <span class="tag" :class="isAdmin ? 'info' : 'soft'">{{ roleLabel }}</span>
        </span>
      </div>
      <button class="ghost logout" type="button" @click="signOut()">退出</button>
    </div>
  </header>
</template>

<style scoped>
.header {
  position: relative;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 22px;
  height: var(--header-h);
  padding: 0 22px;
  background: color-mix(in srgb, var(--bg) 90%, transparent);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(16px);
  flex: none;
}

.header::after {
  content: '';
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent-ring), transparent);
  opacity: 0.7;
  pointer-events: none;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text);
  text-decoration: none;
  flex: none;
}

.brand:hover {
  text-decoration: none;
}

.brand-copy {
  display: flex;
  flex-direction: column;
  gap: 0;
  line-height: 1.2;
}

.brand-copy strong {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.brand-copy small {
  color: var(--text-faint);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.12em;
}

.nav {
  display: flex;
  align-items: center;
  gap: 3px;
  flex: 1;
  min-width: 0;
}

.tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 14px;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
}

.tab:hover {
  background: var(--bg-sunken);
  color: var(--text);
  text-decoration: none;
}

.tab.router-link-active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 650;
}

.right {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: none;
}

.backend,
.password {
  position: relative;
}

.chip {
  height: 30px;
  padding: 0 9px;
  gap: 6px;
  border-color: transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: 11.5px;
  border-radius: var(--radius-full);
}

.chip:hover:not(:disabled) {
  border-color: var(--border);
  background: var(--bg-muted);
}

.chip-label {
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--text-faint) 10%, transparent);
}

.dot.ok {
  background: var(--success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 12%, transparent);
}

.backend-panel {
  position: absolute;
  right: 0;
  top: 38px;
  width: 330px;
  padding: 15px;
  z-index: 30;
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  gap: 9px;
  border-radius: var(--radius-lg);
}

.panel-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 4px;
}

.panel-title strong {
  font-size: 13px;
}

.panel-title .faint {
  font-size: 10px;
}

.backend-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 2px;
}

.who {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 9px;
  border-left: 1px solid var(--border);
}

.avatar {
  width: 29px;
  height: 29px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--brand-gradient);
  color: #fff;
  font-size: 12px;
  font-weight: 650;
  box-shadow: var(--shadow-xs);
}

.who-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: 1.2;
}

.who-name {
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.who-copy .tag {
  align-self: flex-start;
  height: 16px;
  padding: 0 5px;
  font-size: 10px;
}

.logout {
  height: 30px;
  padding: 0 7px;
  font-size: 11.5px;
}

@media (max-width: 1180px) {
  .header {
    gap: 12px;
    padding: 0 14px;
  }

  .brand-copy small {
    display: none;
  }

  .tab {
    padding: 0 10px;
  }

  .chip-label {
    display: none;
  }

  .who-copy {
    display: none;
  }

  .who {
    padding-left: 0;
    border-left: none;
  }
}

@media (max-width: 768px) {
  .header {
    gap: 6px;
    padding: 0 8px;
  }

  .brand-copy {
    display: none;
  }

  .nav {
    gap: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .nav::-webkit-scrollbar {
    display: none;
  }

  .tab {
    height: 32px;
    padding: 0 8px;
    font-size: 12px;
  }

  .right {
    gap: 2px;
  }

  .backend,
  .password,
  .who {
    display: none;
  }

  .right > button.ghost {
    padding: 0 6px;
    font-size: 12px;
  }
}
</style>

<script setup lang="ts">
/**
 * 账号与权限页(**仅管理员**)。两件事:账号、分组(一类员工)。
 *
 * **文档权限不在这里** —— 它在知识库页每张卡片的「权限」按钮上。理由:授权时人盯着的
 * 是"哪一份文档",把文档列表再抄一遍到这个页面上,两边迟早对不上(而且管理员要来回跳)。
 *
 * 三条界面口径:
 *
 * 1. **删自己 / 删最后一个管理员**由后端拦(400 + 中文原因),这里**原样显示那句话** ——
 *    前端也检查一遍的话,就又多了一份"谁不能被删"的真源;
 * 2. **改密码是给"本人忘了密码"用的**:改完不吊销他手上的令牌(吊销是另一件事);
 * 3. **组成员的保存是整份覆盖**:勾完点保存 = "这个组就是这些人"。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import GroupDocumentsDialog from '../components/GroupDocumentsDialog.vue'
import PageShell from '../components/ui/PageShell.vue'
import Surface from '../components/ui/Surface.vue'
import { authState } from '../stores/auth'
import {
  accountsState,
  createGroup,
  createUser,
  deleteGroup,
  deleteUser,
  groupName,
  loadAccounts,
  resetPassword,
  setMembers,
} from '../stores/accounts'
import type { GroupOut } from '../api/types'

const accounts = accountsState()
const auth = authState()

const draft = reactive({
  username: '',
  password: '',
  role: 'employee',
})
const newGroup = ref('')
/** 正在编辑成员的组(整份名单的草稿)。 */
const editingGroup = ref<GroupOut | null>(null)
const memberDraft = ref<string[]>([])
/** 正在配"这个组能看哪些文档"的组(null = 没开弹层)。 */
const documentsFor = ref<GroupOut | null>(null)
/** 正在改密码的账号(user_id)与新密码。 */
const passwordFor = ref('')
const passwordDraft = ref('')

const roles = computed(() => [
  { value: 'employee', label: '员工' },
  { value: 'ops', label: '运维' },
  { value: 'admin', label: '管理员' },
])

onMounted(() => {
  void loadAccounts()
})

async function submitUser(): Promise<void> {
  const ok = await createUser({ ...draft })
  if (ok) {
    draft.username = ''
    draft.password = ''
  }
}

async function submitGroup(): Promise<void> {
  if (newGroup.value.trim() === '') return
  const ok = await createGroup(newGroup.value.trim())
  if (ok) newGroup.value = ''
}

function startEditMembers(group: GroupOut): void {
  editingGroup.value = group
  memberDraft.value = [...group.member_ids]
}

function toggleMember(userId: string): void {
  memberDraft.value = memberDraft.value.includes(userId)
    ? memberDraft.value.filter((id) => id !== userId)
    : [...memberDraft.value, userId]
}

async function saveMembers(): Promise<void> {
  if (editingGroup.value === null) return
  const ok = await setMembers(editingGroup.value, memberDraft.value)
  if (ok) editingGroup.value = null
}

async function submitPassword(): Promise<void> {
  const user = accounts.users.find((item) => item.user_id === passwordFor.value)
  if (user === undefined) return
  const ok = await resetPassword(user, passwordDraft.value)
  if (ok) {
    passwordFor.value = ''
    passwordDraft.value = ''
  }
}

/** 一个账号能不能删:**只由后端说了算**,这里只把"点了自己"这种明显的手误提前说一句。 */
function isSelf(userId: string): boolean {
  return auth.user !== null && auth.user.username === userNameOf(userId)
}

function userNameOf(userId: string): string {
  return accounts.users.find((item) => item.user_id === userId)?.username ?? userId
}
</script>

<template>
  <PageShell title="账号与权限" :meta="`${accounts.users.length} 个账号 · ${accounts.groups.length} 个组`">
    <template #actions>
      <span v-if="accounts.loading" class="spin"></span>
      <button class="ghost" type="button" :disabled="accounts.loading" @click="loadAccounts">
        刷新
      </button>
    </template>

    <p v-if="accounts.error" class="alert error">{{ accounts.error }}</p>
    <p v-if="accounts.notice" class="alert info">{{ accounts.notice }}</p>

    <Surface title="账号">
      <table class="data-table">
        <thead>
          <tr>
            <th>登录名</th>
            <th>角色</th>
            <th>组</th>
            <th class="actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in accounts.users" :key="user.user_id">
            <td><code>{{ user.username }}</code></td>
            <td>
              <span class="tag" :class="user.is_admin ? 'info' : 'soft'">
                {{ roles.find((item) => item.value === user.role)?.label ?? user.role }}
              </span>
            </td>
            <td>
              <span v-if="user.group_ids.length === 0" class="faint">—</span>
              <span v-for="groupId in user.group_ids" :key="groupId" class="tag soft">
                {{ groupName(groupId) }}
              </span>
            </td>
            <td class="actions">
              <button
                class="ghost"
                type="button"
                @click="passwordFor = user.user_id; passwordDraft = ''"
              >
                改密码
              </button>
              <button
                class="ghost danger"
                type="button"
                :title="isSelf(user.user_id) ? '不能删自己' : ''"
                @click="deleteUser(user)"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="accounts.users.length === 0">
            <td colspan="4" class="faint">暂无账号</td>
          </tr>
        </tbody>
      </table>

      <div v-if="passwordFor !== ''" class="quick-row">
        <span class="quick-label">改密码 · {{ userNameOf(passwordFor) }}</span>
        <input v-model="passwordDraft" type="password" placeholder="新密码" />
        <button class="primary" type="button" @click="submitPassword">保存</button>
        <button class="ghost" type="button" @click="passwordFor = ''">取消</button>
      </div>

      <div class="quick-row">
        <span class="quick-label">新建账号</span>
        <input v-model="draft.username" type="text" placeholder="登录名" />
        <input v-model="draft.password" type="password" placeholder="密码" />
        <select v-model="draft.role">
          <option v-for="item in roles" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
        <button
          class="primary"
          type="button"
          :disabled="draft.username === '' || draft.password === ''"
          @click="submitUser"
        >
          建号
        </button>
      </div>
    </Surface>

    <Surface title="分组">
      <table class="data-table">
        <thead>
          <tr>
            <th>组名</th>
            <th>成员</th>
            <th class="actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="group in accounts.groups" :key="group.group_id">
            <td><code>{{ group.name }}</code></td>
            <td>
              <span v-if="group.member_ids.length === 0" class="faint">—</span>
              <span v-for="userId in group.member_ids" :key="userId" class="tag soft">
                {{ userNameOf(userId) }}
              </span>
            </td>
            <td class="actions">
              <button class="ghost" type="button" @click="startEditMembers(group)">成员</button>
              <button class="ghost" type="button" @click="documentsFor = group">文档权限</button>
              <button class="ghost danger" type="button" @click="deleteGroup(group)">删除</button>
            </td>
          </tr>
          <tr v-if="accounts.groups.length === 0">
            <td colspan="3" class="faint">暂无分组</td>
          </tr>
        </tbody>
      </table>

      <div v-if="editingGroup !== null" class="quick-block">
        <span class="quick-label">{{ editingGroup.name }} · 成员</span>
        <div class="checks">
          <label v-for="user in accounts.users" :key="user.user_id" class="check">
            <input
              type="checkbox"
              :checked="memberDraft.includes(user.user_id)"
              @change="toggleMember(user.user_id)"
            />
            {{ user.username }}
          </label>
        </div>
        <div class="quick-actions">
          <button class="primary" type="button" @click="saveMembers">保存</button>
          <button class="ghost" type="button" @click="editingGroup = null">取消</button>
        </div>
      </div>

      <div class="quick-row">
        <span class="quick-label">新建分组</span>
        <input v-model="newGroup" type="text" placeholder="组名" />
        <button class="primary" type="button" :disabled="newGroup.trim() === ''" @click="submitGroup">
          建组
        </button>
      </div>
    </Surface>

    <GroupDocumentsDialog
      v-if="documentsFor !== null"
      :group-id="documentsFor.group_id"
      :group-name="documentsFor.name"
      @close="documentsFor = null"
    />
  </PageShell>
</template>

<style scoped>
.quick-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}

.quick-row input,
.quick-row select {
  width: auto;
  min-width: 130px;
  flex: 0 1 180px;
  height: 30px;
}

.quick-label {
  font-size: 12px;
  color: var(--text-faint);
  margin-right: 2px;
  white-space: nowrap;
}

.quick-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-muted);
}

.quick-actions {
  display: flex;
  gap: 8px;
}

.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12.5px;
  color: var(--text);
}

@media (max-width: 768px) {
  .quick-row input,
  .quick-row select {
    flex: 1 1 100%;
    min-width: 0;
  }
}
</style>

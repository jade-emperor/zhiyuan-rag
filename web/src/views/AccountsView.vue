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
  { value: 'employee', label: '员工(检索与问答)' },
  { value: 'ops', label: '运维(问答 + 报表)' },
  { value: 'admin', label: '管理员(全部)' },
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
  <div class="page">
    <header class="head">
      <div>
        <h1>账号与权限</h1>
        <p class="faint">
          建号 / 删号 / 分组都在这里;<strong>文档给谁看</strong>在「知识库」页每张卡片的
          「权限」按钮上(授权时盯着的是那份文档,不是这张名单)。
          <span v-if="accounts.loading"> · <span class="spin"></span> 读取中…</span>
        </p>
      </div>
      <div class="controls">
        <button class="ghost" type="button" :disabled="accounts.loading" @click="loadAccounts">
          刷新
        </button>
      </div>
    </header>

    <p v-if="accounts.error" class="alert error">{{ accounts.error }}</p>
    <p v-if="accounts.notice" class="alert info">{{ accounts.notice }}</p>

    <section class="card block">
      <h2>账号</h2>
      <p class="faint">
        三种角色:<strong>员工</strong>(检索与问答)、<strong>运维</strong>(检索与问答 + 报表:延迟 + 消耗,
        但管不了文档与账号)、<strong>管理员</strong>(全部 + 建号删号授权)。
        <br />
        <strong>不能删自己的账号,也不能删掉最后一个管理员</strong> ——
        删了自己这枚令牌立刻失效(你会被踢出去),而管理员删光了就没人能再建号了。
      </p>
      <table>
        <thead>
          <tr>
            <th>登录名</th>
            <th>角色</th>
            <th>所属组(哪几类员工)</th>
            <th class="num">操作</th>
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
              <span v-if="user.group_ids.length === 0" class="faint">—(不属于任何组)</span>
              <span v-for="groupId in user.group_ids" :key="groupId" class="tag soft">
                {{ groupName(groupId) }}
              </span>
            </td>
            <td class="num actions">
              <button class="ghost" type="button" @click="passwordFor = user.user_id; passwordDraft = ''">
                改密码
              </button>
              <button
                class="ghost danger"
                type="button"
                :title="isSelf(user.user_id) ? '不能删自己的账号' : ''"
                @click="deleteUser(user)"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="accounts.users.length === 0">
            <td colspan="4" class="faint">还没有账号 —— 用下面的表单建第一个。</td>
          </tr>
        </tbody>
      </table>

      <div v-if="passwordFor !== ''" class="inline-form">
        <strong>改密码:</strong>
        <code>{{ userNameOf(passwordFor) }}</code>
        <input v-model="passwordDraft" type="password" placeholder="新密码(至少 8 位)" />
        <button class="primary" type="button" @click="submitPassword">保存</button>
        <button class="ghost" type="button" @click="passwordFor = ''">取消</button>
        <span class="faint">改完他手上的令牌仍然有效 —— 这条路径是给"本人忘了密码"用的。</span>
      </div>

      <div class="inline-form">
        <strong>建号:</strong>
        <input v-model="draft.username" type="text" placeholder="登录名" />
        <input v-model="draft.password" type="password" placeholder="密码(至少 8 位)" />
        <select v-model="draft.role">
          <option v-for="item in roles" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
        <button class="primary" type="button" :disabled="draft.username === '' || draft.password === ''" @click="submitUser">
          建号
        </button>
      </div>
    </section>

    <section class="card block">
      <h2>分组(一类员工)</h2>
      <p class="faint">
        组用来做<strong>文档授权</strong>:一份文档可以授权给"某个人"或"某一类人"。
        同一个人可以属于多个组,权限取<strong>并集</strong>(任一命中即可见)。
        <br />
        删组会<strong>连带清掉授权给这个组的所有规则</strong> ——
        否则将来建一个同名的新组会凭空继承旧权限。
        <br />
        「文档权限」按<strong>组</strong>配这个组能看哪些文档（与「知识库」页每份文档上的
        「权限」按钮是同一张规则表的两个方向）。
      </p>
      <table>
        <thead>
          <tr>
            <th>组名</th>
            <th>成员</th>
            <th class="num">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="group in accounts.groups" :key="group.group_id">
            <td><code>{{ group.name }}</code></td>
            <td>
              <span v-if="group.member_ids.length === 0" class="faint">—(还没有人)</span>
              <span v-for="userId in group.member_ids" :key="userId" class="tag soft">
                {{ userNameOf(userId) }}
              </span>
            </td>
            <td class="num actions">
              <button class="ghost" type="button" @click="startEditMembers(group)">改成员</button>
              <button class="ghost" type="button" @click="documentsFor = group">文档权限</button>
              <button class="ghost danger" type="button" @click="deleteGroup(group)">删除</button>
            </td>
          </tr>
          <tr v-if="accounts.groups.length === 0">
            <td colspan="3" class="faint">还没有组 —— 建一个(例如「销售」「售后」)。</td>
          </tr>
        </tbody>
      </table>

      <div v-if="editingGroup !== null" class="inline-form column">
        <strong>「{{ editingGroup.name }}」的成员</strong>
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
        <div class="actions">
          <button class="primary" type="button" @click="saveMembers">保存(整份名单)</button>
          <button class="ghost" type="button" @click="editingGroup = null">取消</button>
        </div>
      </div>

      <div class="inline-form">
        <strong>建组:</strong>
        <input v-model="newGroup" type="text" placeholder="组名(例如 销售)" />
        <button class="primary" type="button" :disabled="newGroup.trim() === ''" @click="submitGroup">
          建组
        </button>
      </div>
    </section>
    <GroupDocumentsDialog
      v-if="documentsFor !== null"
      :group-id="documentsFor.group_id"
      :group-name="documentsFor.name"
      @close="documentsFor = null"
    />
  </div>
</template>

<style scoped>
.page {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

h1 {
  font-size: 18px;
}

h2 {
  font-size: 15px;
}

.block {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

th {
  font-weight: 600;
  color: var(--text-muted);
  font-size: 12px;
}

.num {
  text-align: right;
}

.actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

.inline-form {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 6px;
}

.inline-form.column {
  flex-direction: column;
  align-items: flex-start;
}

.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}

input,
select {
  padding: 6px 8px;
}

.tag {
  margin-right: 4px;
}
</style>

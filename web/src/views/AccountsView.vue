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
import Icon from '../components/Icon.vue'
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

const adminCount = computed(() => accounts.users.filter((user) => user.is_admin).length)
const memberCount = computed(
  () => new Set(accounts.groups.flatMap((group) => group.member_ids)).size,
)

const roleMatrix = [
  { name: '员工', code: 'EMPLOYEE', abilities: ['知识问答', '引用溯源'], muted: ['报表', '知识管理'] },
  { name: '运维', code: 'OPS', abilities: ['知识问答', '运营报表'], muted: ['成员管理'] },
  { name: '管理员', code: 'ADMIN', abilities: ['知识问答', '运营报表', '知识管理', '成员管理'], muted: [] },
] as const
</script>

<template>
  <PageShell
    eyebrow="ORGANIZATION & ACCESS"
    title="账号与权限"
    :meta="`${accounts.users.length} 个账号 · ${accounts.groups.length} 个组`"
    description="统一管理成员身份、角色与知识访问边界。让合适的人，在合适的范围内，使用可信的知识。"
  >
    <template #actions>
      <span v-if="accounts.loading" class="spin"></span>
      <button class="ghost" type="button" :disabled="accounts.loading" @click="loadAccounts">
        刷新
      </button>
    </template>

    <p v-if="accounts.error" class="alert error">{{ accounts.error }}</p>
    <p v-if="accounts.notice" class="alert info">{{ accounts.notice }}</p>

    <div class="account-stats">
      <article class="stat-card">
        <span class="stat-icon"><Icon name="users" :size="17" /></span>
        <div><strong>{{ accounts.users.length }}</strong><span>成员账号</span><small>{{ memberCount }} 人已编入分组</small></div>
      </article>
      <article class="stat-card">
        <span class="stat-icon"><Icon name="check" :size="17" /></span>
        <div><strong>{{ adminCount }}</strong><span>管理员</span><small>拥有知识库管理权限</small></div>
      </article>
      <article class="stat-card">
        <span class="stat-icon"><Icon name="folder" :size="17" /></span>
        <div><strong>{{ accounts.groups.length }}</strong><span>个分组</span><small>用于批量授予文档权限</small></div>
      </article>
    </div>

    <div class="accounts-layout">
      <div class="accounts-main">
        <Surface title="账号">
          <template #actions>
            <span class="faint">角色决定可用能力</span>
          </template>
          <table class="data-table">
            <thead>
              <tr>
                <th>登录名</th>
                <th>角色</th>
                <th>所属组</th>
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
                    修改密码
                  </button>
                  <button
                    class="ghost danger"
                    type="button"
                    :title="isSelf(user.user_id) ? '不能删除当前登录账号' : ''"
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
            <span class="quick-label">修改密码 · {{ userNameOf(passwordFor) }}</span>
            <input v-model="passwordDraft" type="password" placeholder="新密码" />
            <button class="primary" type="button" @click="submitPassword">保存</button>
            <button class="ghost" type="button" @click="passwordFor = ''">取消</button>
          </div>

          <div class="quick-row create-row">
            <span class="quick-label">新增成员</span>
            <input v-model="draft.username" type="text" placeholder="登录名" />
            <input v-model="draft.password" type="password" placeholder="初始密码" />
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
              创建账号
            </button>
          </div>
        </Surface>

        <section class="role-matrix" aria-label="角色能力">
          <header class="matrix-head">
            <div>
              <p class="policy-kicker">ROLE MATRIX</p>
              <h2>角色能力</h2>
            </div>
            <span class="faint">按职责分配，最小化授权</span>
          </header>
          <div class="matrix-grid">
            <article v-for="role in roleMatrix" :key="role.code" class="matrix-card">
              <div class="matrix-title">
                <strong>{{ role.name }}</strong>
                <span>{{ role.code }}</span>
              </div>
              <ul>
                <li v-for="ability in role.abilities" :key="ability" class="on">
                  <Icon name="check" :size="11" /> {{ ability }}
                </li>
                <li v-for="ability in role.muted" :key="ability" class="off">
                  <Icon name="close" :size="11" /> {{ ability }}
                </li>
              </ul>
            </article>
          </div>
        </section>
      </div>

      <aside class="accounts-side">
        <Surface title="分组">
          <div class="group-table">
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
          </div>

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

          <div class="quick-row create-row group-create">
            <span class="quick-label">新建分组</span>
            <input v-model="newGroup" type="text" placeholder="组名" />
            <button class="primary" type="button" :disabled="newGroup.trim() === ''" @click="submitGroup">
              创建分组
            </button>
          </div>
        </Surface>

        <section class="policy-card">
          <p class="policy-kicker">ACCESS MODEL</p>
          <h2>权限边界</h2>
          <ul>
            <li><strong>角色</strong><span>决定成员可使用的功能范围。</span></li>
            <li><strong>分组</strong><span>用于批量委派文档访问权限。</span></li>
            <li><strong>文档权限</strong><span>只做收窄，不扩大可见范围。</span></li>
          </ul>
        </section>
      </aside>
    </div>

    <GroupDocumentsDialog
      v-if="documentsFor !== null"
      :group-id="documentsFor.group_id"
      :group-name="documentsFor.name"
      @close="documentsFor = null"
    />
  </PageShell>
</template>

<style scoped>
.account-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.stat-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 15px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 3%, transparent), transparent 66%),
    var(--bg);
  box-shadow: var(--shadow-xs);
}

.stat-icon {
  width: 33px;
  height: 33px;
  display: grid;
  place-items: center;
  flex: none;
  border-radius: var(--radius);
  background: var(--accent-soft);
  color: var(--accent);
}

.stat-card > div {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 5px;
}

.stat-card strong {
  font-size: 23px;
  font-weight: 650;
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}

.stat-card span:not(.stat-icon) {
  color: var(--text-muted);
  font-size: 11.5px;
}

.stat-card small {
  flex-basis: 100%;
  margin-top: 2px;
  color: var(--text-faint);
  font-size: 10.5px;
}

.accounts-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(380px, 0.9fr);
  gap: 16px;
  align-items: start;
}

.role-matrix {
  padding: 17px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  box-shadow: var(--shadow-xs);
}

.matrix-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.matrix-head h2 {
  margin-top: 3px;
  font-size: 13.5px;
  font-weight: 650;
}

.matrix-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 9px;
}

.matrix-card {
  padding: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: var(--bg-muted);
}

.matrix-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 7px;
}

.matrix-title strong {
  font-size: 12px;
  font-weight: 650;
}

.matrix-title span {
  color: var(--text-faint);
  font-size: 8px;
  font-weight: 700;
  letter-spacing: 0.1em;
}

.matrix-card ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.matrix-card li {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
}

.matrix-card li.on {
  color: var(--text-muted);
}

.matrix-card li.on svg {
  color: var(--accent);
}

.matrix-card li.off {
  color: var(--text-faint);
  opacity: 0.62;
}

.accounts-main,
.accounts-side {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}

.group-table {
  overflow-x: auto;
}

.policy-card {
  padding: 17px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background:
    radial-gradient(320px 200px at 100% 0%, var(--accent-soft), transparent 72%),
    var(--bg);
  box-shadow: var(--shadow-xs);
}

.policy-kicker {
  margin: 0;
  color: var(--accent);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.15em;
}

.policy-card h2 {
  margin: 3px 0 10px;
  font-size: 14px;
  font-weight: 650;
}

.policy-card ul {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.policy-card li {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding-left: 11px;
  border-left: 2px solid color-mix(in srgb, var(--accent) 24%, transparent);
}

.policy-card strong {
  font-size: 11.5px;
  font-weight: 600;
}

.policy-card li span {
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.55;
}

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

.create-row {
  margin-top: 14px;
}

.group-create {
  align-items: stretch;
}

.group-create input {
  flex: 1 1 100%;
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

@media (max-width: 1120px) {
  .accounts-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .accounts-side {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(240px, 0.8fr);
    align-items: start;
  }
}

@media (max-width: 768px) {
  .account-stats {
    grid-template-columns: minmax(0, 1fr);
  }

  .accounts-side {
    display: flex;
  }

  .matrix-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .quick-row input,
  .quick-row select {
    flex: 1 1 100%;
    min-width: 0;
  }
}
</style>

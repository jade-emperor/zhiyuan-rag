<script setup lang="ts">
/**
 * 文档权限弹层:这份文档给**谁**看。
 *
 * 三条口径写在这里,因为它们直接决定界面上那几句话:
 *
 * 1. **没配规则 = 人人可见**(不是"谁都看不到")。"取消限制"就是把两个名单都清空 ——
 *    界面因此要把这个默认状态说清楚,否则管理员会以为"没勾人 = 都看不见";
 * 2. **只用来收窄**:授权对象可以是某个人、也可以是某一类人(组),两者取并集,
 *    **管理员永远可见**(他是唯一能改这份名单的人);
 * 3. **保存是整份覆盖**(PUT 一份完整名单):勾完点保存,语义就是"这些人 + 这些组"。
 */
import { computed, onMounted, ref } from 'vue'

import * as api from '../api/endpoints'
import DialogShell from './ui/DialogShell.vue'
import { accountsState, loadAccounts } from '../stores/accounts'
import { documentsState, refreshDocuments } from '../stores/documents'

const props = defineProps<{ documentId: string }>()
const emit = defineEmits<{ close: [] }>()

const accounts = accountsState()
const docs = documentsState()

const selectedUsers = ref<string[]>([])
const selectedGroups = ref<string[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')

const filename = computed(
  () => docs.docs.find((doc) => doc.document_id === props.documentId)?.filename ?? props.documentId,
)
const restricted = computed(() => selectedUsers.value.length + selectedGroups.value.length > 0)

onMounted(async () => {
  // 人员与组一次拉回来(弹层要用它们渲染勾选框);已有数据就不重复拉。
  if (accounts.users.length === 0 && accounts.groups.length === 0) await loadAccounts()
  try {
    const acl = await api.adminDocumentAcl(props.documentId)
    selectedUsers.value = [...acl.user_ids]
    selectedGroups.value = [...acl.group_ids]
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
})

/** 输入:改哪一份名单(``users`` / ``groups``)+ 对象 ID。行为:勾上或取消。输出:无。
 *
 * 用键名而不是直接传那个 ref:模板里 `selectedUsers` 会被 Vue **自动解包**成数组,
 * 传进来的不是 ref(传 ref 反而会让写模板的人以为要写 `.value`)。
 */
function toggle(which: 'users' | 'groups', id: string): void {
  const list = which === 'users' ? selectedUsers : selectedGroups
  list.value = list.value.includes(id)
    ? list.value.filter((item) => item !== id)
    : [...list.value, id]
}

async function save(): Promise<void> {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const acl = await api.adminSetDocumentAcl(props.documentId, {
      user_ids: selectedUsers.value,
      group_ids: selectedGroups.value,
    })
    selectedUsers.value = [...acl.user_ids]
    selectedGroups.value = [...acl.group_ids]
    notice.value = acl.restricted
      ? '已保存:只有名单里的人(和他们所属的组)能看到这份文档'
      : '已保存:这份文档回到「人人可见」(名单是空的)'
    // 清单上的「限定」标记来自后端,保存完顺手刷一次,免得卡片和弹层说的不一样。
    void refreshDocuments()
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <DialogShell label="文档权限" :title="filename" size="md" @close="emit('close')">
    <template #subtitle>
      <span v-if="loading" class="tag soft">读取中…</span>
      <span v-else class="tag" :class="restricted ? 'warn' : 'ok'">
        {{ restricted ? '限定' : '人人可见' }}
      </span>
    </template>

    <p v-if="error" class="alert error">{{ error }}</p>
    <p v-if="notice" class="alert info">{{ notice }}</p>

    <template v-if="!loading">
      <section>
        <h4>人员</h4>
        <div class="checks">
          <label v-for="user in accounts.users" :key="user.user_id" class="check">
            <input
              type="checkbox"
              :checked="selectedUsers.includes(user.user_id)"
              @change="toggle('users', user.user_id)"
            />
            {{ user.username }}
          </label>
          <p v-if="accounts.users.length === 0" class="faint">暂无账号</p>
        </div>
      </section>

      <section>
        <h4>组</h4>
        <div class="checks">
          <label v-for="group in accounts.groups" :key="group.group_id" class="check">
            <input
              type="checkbox"
              :checked="selectedGroups.includes(group.group_id)"
              @change="toggle('groups', group.group_id)"
            />
            {{ group.name }}
          </label>
          <p v-if="accounts.groups.length === 0" class="faint">暂无分组</p>
        </div>
      </section>
    </template>

    <template #footer>
      <button
        class="ghost"
        type="button"
        :disabled="saving"
        @click="selectedUsers = []; selectedGroups = []"
      >
        清空名单(= 取消限制)
      </button>
      <button class="primary" type="button" :disabled="saving || loading" @click="save">
        {{ saving ? '保存中…' : '保存(整份名单)' }}
      </button>
    </template>
  </DialogShell>
</template>

<style scoped>
section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

h4 {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
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
  font-size: 13px;
  color: var(--text);
}
</style>

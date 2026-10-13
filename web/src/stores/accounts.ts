/**
 * 账号、分组与文档可见范围(P0-6,管理员页)。
 *
 * 三条口径,与后端一字对应:
 *
 * 1. **账号列表里没有密码**(响应里也没有它的哈希)—— 建号时填的密码只进不出;
 * 2. **整份名单**是声明式写法:改组成员 / 改文档范围都是"把这一份名单 PUT 上去",
 *    不是一堆增删请求 —— 半份名单是"看起来设过了"的权限;
 * 3. **删除是幂等的**(账号、组都一样):`deleted=false` = 本来就没有,界面照原样提示,
 *    不当成错误。
 *
 * 还有一条**界面上的**规矩:后端拒绝的理由(删自己 / 删最后一个管理员 / 重名 / 幽灵成员)
 * 都是中文人话,这里**原样显示**,不再包装成"操作失败" —— 那句人话才是唯一的线索。
 */

import { reactive } from 'vue'

import * as api from '../api/endpoints'
import type { GroupOut, UserOut } from '../api/types'

const state = reactive({
  users: [] as UserOut[],
  groups: [] as GroupOut[],
  loading: false,
  error: '',
  /** 上一次操作的结果提示(成功那一类)。错误走 `error`。 */
  notice: '',
})

export function accountsState() {
  return state
}

/** 输入:无。行为:把账号与分组一起拉回来(两个接口,一次刷新)。输出:无。
 *
 * 一起拉是刻意的:账号表要显示"他属于哪几组",分组要显示成员是谁 —— 只拉一半会让
 * 两个面板互相矛盾(人已经进组了,账号表还显示没进)。
 */
export async function loadAccounts(): Promise<void> {
  state.loading = true
  state.error = ''
  try {
    const [users, groups] = await Promise.all([api.adminUsers(), api.adminGroups()])
    state.users = users.users
    state.groups = groups.groups
  } catch (error) {
    state.error = (error as Error).message
  } finally {
    state.loading = false
  }
}

/** 输入:一次后端调用 + 成功提示语。行为:跑它 → 刷新两个列表 → 记提示;失败留在 error。输出:是否成功。 */
async function act(what: () => Promise<unknown>, notice: string): Promise<boolean> {
  state.error = ''
  state.notice = ''
  try {
    await what()
  } catch (error) {
    state.error = (error as Error).message
    return false
  }
  await loadAccounts()
  state.notice = notice
  return true
}

export function createUser(payload: {
  username: string
  password: string
  role: string
}): Promise<boolean> {
  return act(() => api.adminCreateUser(payload), `已建号:${payload.username}`)
}

export function deleteUser(user: UserOut): Promise<boolean> {
  return act(() => api.adminDeleteUser(user.user_id), `已删除账号:${user.username}`)
}

export function resetPassword(user: UserOut, password: string): Promise<boolean> {
  return act(() => api.adminResetPassword(user.user_id, password), `已改 ${user.username} 的密码`)
}

export function createGroup(name: string): Promise<boolean> {
  return act(() => api.adminCreateGroup(name), `已建组:${name}`)
}

export function deleteGroup(group: GroupOut): Promise<boolean> {
  return act(() => api.adminDeleteGroup(group.group_id), `已删除组:${group.name}(授权给它的文档回到人人可见)`)
}

export function setMembers(group: GroupOut, userIds: string[]): Promise<boolean> {
  return act(
    () => api.adminSetMembers(group.group_id, userIds),
    `已更新「${group.name}」的成员:${userIds.length} 人`,
  )
}

/** 输入:组 ID。行为:找它的名字(账号表里显示"他属于哪几类")。输出:名字;找不到给 ID。 */
export function groupName(groupId: string): string {
  return state.groups.find((group) => group.group_id === groupId)?.name ?? groupId
}

/** 输入:用户 ID。行为:找他的登录名(组成员 / 授权名单里显示)。输出:名字;找不到给 ID。 */
export function userName(userId: string): string {
  return state.users.find((user) => user.user_id === userId)?.username ?? userId
}

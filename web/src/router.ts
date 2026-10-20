/**
 * 路由:**hash 模式**。
 *
 * 为什么不用 history 模式:那要求服务端把未知路径全部重写成 index.html(SPA fallback)。
 * 这份产物有两个宿主 —— 后端的 `/static/app/`(Starlette 的 StaticFiles,不做 fallback)
 * 和桌面壳的 `file://`(根本没有服务端)。hash(`#/chat`)两边都不用配置就能跑。
 *
 * 守卫管四件事:**没登录去登录页**、**能力不够的页不进**、**进不去时落到他能看的第一页**、
 * **业务页入场前保证能力自述已读到**。
 * 再强调一次:这是"不给点",不是权限 —— 权限在后端
 * (`require_admin` / `require_reports` / `require_knowledge`)。
 *
 * **为什么按能力而不是按角色**:路由 meta 里写的是"这一页要什么能力"
 * (`needs: 'reports'`),能不能满足由 `/auth/me` 报的三个布尔值回答。写成
 * `roles: ['ops', 'admin']` 就等于把"哪些角色能看报表"抄进前端 —— 后端加第四个角色时,
 * 这里会静默地少一个页签(不报错,只是没人看得到那个页面)。
 *
 * 第三条(能力自述)是补出来的:**能力自述原来只由「登录页」与「对话页」挂载时拉一次**,
 * 于是"直接落在 `#/reports`"(在报表页按 F5、书签、上次关在这一页)时,看板读到一个空 store,
 * 只回一句「还没读到 capabilities,不知道该请求哪个窗口」,而且**点「刷新」也救不回来**。
 * 所以它该和"有没有登录"一样在守卫里解决,而不是指望每个新页面自己记得去拉。
 * 拉取失败(后端没起)**不拦导航** —— 让页面照常进去,由各页把"连不上"如实说出来。
 */

import { createRouter, createWebHashHistory } from 'vue-router'

import {
  authState,
  canUseKnowledge,
  canViewReports,
  isAdmin,
  loadMe,
} from './stores/auth'
import { capabilitiesState, loadCapabilities } from './stores/capabilities'
import AccountsView from './views/AccountsView.vue'
import ChatView from './views/ChatView.vue'
import LoginView from './views/LoginView.vue'
import RegisterView from './views/RegisterView.vue'
import MetricsView from './views/MetricsView.vue'
import FolderView from './views/FolderView.vue'
import KnowledgeView from './views/KnowledgeView.vue'

/** 一页要的那个能力(与 `stores/auth.ts` 的三个 computed 一一对应)。 */
type Need = 'knowledge' | 'reports' | 'manage'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/register', name: 'register', component: RegisterView },
    { path: '/chat', name: 'chat', component: ChatView, meta: { needs: 'knowledge' } },
    { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { needs: 'manage' } },
    // 二级页面:**一个目录里的文档**。`unfiled` 是未分类那一格的保留 ID(它也是目录)。
    {
      path: '/knowledge/:folder',
      name: 'folder',
      component: FolderView,
      meta: { needs: 'manage' },
    },
    { path: '/reports', name: 'reports', component: MetricsView, meta: { needs: 'reports' } },
    { path: '/accounts', name: 'accounts', component: AccountsView, meta: { needs: 'manage' } },
    { path: '/:rest(.*)', redirect: '/chat' },
  ],
})

/** 路由名 → 页面标题。写成一张表,新增页面时不会漏掉 document.title。 */
const TITLES: Record<string, string> = {
  login: '登录',
  register: '注册',
  chat: '对话',
  knowledge: '知识库',
  folder: '知识库',
  reports: '报表',
  accounts: '账号与权限',
}

router.afterEach((to) => {
  const name = typeof to.name === 'string' ? to.name : ''
  const label = TITLES[name]
  document.title = label === undefined ? '知源助手' : `${label} · 知源助手`
})

const NEEDS: Record<Need, () => boolean> = {
  knowledge: () => canUseKnowledge.value,
  reports: () => canViewReports.value,
  manage: () => isAdmin.value,
}

/** 输入:无。行为:找一个这个账号**确实能看**的落地页。输出:路由位置。
 *
 * 为什么要它:没有「报表」能力的人打不开报表页,不给他一个去处的话,登录后会被"重定向 → 再重定向"
 * 绕回 `/`(那又指向 `/chat`),浏览器只会显示一个空白页 —— 而他的报表明明是可看的。
 */
export function landingRoute() {
  if (canUseKnowledge.value) return { name: 'chat' }
  if (canViewReports.value) return { name: 'reports' }
  return { name: 'login' }
}

router.beforeEach(async (to) => {
  const auth = authState()
  if (!auth.ready) await loadMe()
  if (to.name === 'login' || to.name === 'register') return true
  if (auth.user === null) return { name: 'login', query: { next: to.fullPath } }
  const need = to.meta.needs as Need | undefined
  if (need !== undefined && !NEEDS[need]()) return landingRoute()
  // 业务页的入场券:没读到就当场补拉(读不到不拦,让页面把"连不上"说出来)。
  if (capabilitiesState().data === null) await loadCapabilities()
  return true
})

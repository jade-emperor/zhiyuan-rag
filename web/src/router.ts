/**
 * 路由:**hash 模式**。
 *
 * 为什么不用 history 模式:那要求服务端把未知路径全部重写成 index.html(SPA fallback)。
 * 这份产物有两个宿主 —— 后端的 `/static/app/`(Starlette 的 StaticFiles,不做 fallback)
 * 和桌面壳的 `file://`(根本没有服务端)。hash(`#/chat`)两边都不用配置就能跑。
 *
 * 守卫管三件事:**没登录去登录页**、**不是管理员不进管理页**、**业务页入场前保证能力自述已读到**。
 * 再强调一次:这是"不给点",不是权限 —— 权限在后端(`require_admin`)。
 *
 * 第三条是补出来的:**能力自述原来只由「登录页」与「对话页」挂载时拉一次**,于是"直接落在
 * `#/metrics`"(在可观测页按 F5、书签、上次关在这一页)时,看板读到一个空 store,只回一句
 * 「还没读到 capabilities,不知道该请求哪个窗口」,而且**点「刷新」也救不回来**(它读的还是
 * 同一个空 store)。能力自述是**每个业务页的入场券**(窗口白名单 / 上限 / 上传后缀都在里面),
 * 所以它该和"有没有登录"一样在守卫里解决,而不是指望每个新页面自己记得去拉。
 * 拉取失败(后端没起)**不拦导航** —— 让页面照常进去,由各页把"连不上"如实说出来。
 */

import { createRouter, createWebHashHistory } from 'vue-router'

import { authState, isAdmin, loadMe } from './stores/auth'
import { capabilitiesState, loadCapabilities } from './stores/capabilities'
import ChatView from './views/ChatView.vue'
import LoginView from './views/LoginView.vue'
import MetricsView from './views/MetricsView.vue'
import KnowledgeView from './views/KnowledgeView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/chat', name: 'chat', component: ChatView },
    { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { admin: true } },
    { path: '/metrics', name: 'metrics', component: MetricsView, meta: { admin: true } },
    { path: '/:rest(.*)', redirect: '/chat' },
  ],
})

router.beforeEach(async (to) => {
  const auth = authState()
  if (!auth.ready) await loadMe()
  if (to.name === 'login') return true
  if (auth.user === null) return { name: 'login', query: { next: to.fullPath } }
  if (to.meta.admin === true && !isAdmin.value) return { name: 'chat' }
  // 业务页的入场券:没读到就当场补拉(读不到不拦,让页面把"连不上"说出来)。
  if (capabilitiesState().data === null) await loadCapabilities()
  return true
})

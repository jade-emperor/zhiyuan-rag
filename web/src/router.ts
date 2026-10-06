/**
 * 路由:**hash 模式**。
 *
 * 为什么不用 history 模式:那要求服务端把未知路径全部重写成 index.html(SPA fallback)。
 * 这份产物有两个宿主 —— 后端的 `/static/app/`(Starlette 的 StaticFiles,不做 fallback)
 * 和桌面壳的 `file://`(根本没有服务端)。hash(`#/chat`)两边都不用配置就能跑。
 *
 * 守卫只管两件事:**没登录去登录页**、**不是管理员不进管理页**。
 * 再强调一次:这是"不给点",不是权限 —— 权限在后端(`require_admin`)。
 */

import { createRouter, createWebHashHistory } from 'vue-router'

import { authState, isAdmin, loadMe } from './stores/auth'
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
  return true
})

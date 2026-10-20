import { createApp } from 'vue'

import App from './App.vue'
import { onUnauthorized, seedBackendFromQuery } from './api/client'
import { router } from './router'
import { clearAuth, loadMe } from './stores/auth'
import { resetChat } from './stores/chat'
import { initUi } from './stores/ui'
import { initTheme } from './stores/theme'
import './styles/tokens.css'
import './styles/base.css'

// 401 的统一出口:清掉本地登录态 + 跳登录页。**注册在这里而不是 client.ts 里** ——
// 那一层只管发请求,不认识路由,也不认识"会话列表"这类界面状态。
onUnauthorized(() => {
  clearAuth()
  resetChat()
  void router.replace({ name: 'login' })
})

// 先认一次 `?backend=…`(桌面壳/书签),再让任何组件去读后端地址
seedBackendFromQuery()

// 主题在首帧之前落地,避免浅色闪一下
initTheme()

// 布局断点（侧栏/引用面板/底部弹层）在首帧前就算好，避免桌面布局闪一下
initUi()

const app = createApp(App)
app.use(router)
void loadMe().finally(() => app.mount('#app'))

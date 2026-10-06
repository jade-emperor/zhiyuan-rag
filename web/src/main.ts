import { createApp } from 'vue'

import App from './App.vue'
import { onUnauthorized } from './api/client'
import { router } from './router'
import { clearAuth, loadMe } from './stores/auth'
import { resetChat } from './stores/chat'
import './styles/tokens.css'
import './styles/base.css'

// 401 的统一出口:清掉本地登录态 + 跳登录页。**注册在这里而不是 client.ts 里** ——
// 那一层只管发请求,不认识路由,也不认识"会话列表"这类界面状态。
onUnauthorized(() => {
  clearAuth()
  resetChat()
  void router.replace({ name: 'login' })
})

const app = createApp(App)
app.use(router)
void loadMe().finally(() => app.mount('#app'))

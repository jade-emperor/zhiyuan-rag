import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前后端分离：构建产物先落在 web/dist（可被 Nginx / 静态服务器独立发布）；
// `npm run build` 末尾再把一份兼容副本同步进 Python 包，供桌面壳与一体化模式使用。
// 两份产物都不需要 Node 才能运行。
export default defineConfig({
  // 相对 base:同一份产物既能独立挂在任意路径，也能被桌面壳当本地文件打开。
  // 绝对 base('/static/app/')在 file:// 下会去找文件系统根目录,整个壳就白做了。
  base: './',
  plugins: [vue()],
  build: {
    // 相对 root(= web/)—— 刻意不用 node:url 拼绝对路径:那要引 @types/node,
    // 而这份配置里没有一处真的需要 node API。
    outDir: 'dist',
    emptyOutDir: true,
    // 不生成 sourcemap:产物是入库的,map 会让仓库里多出几倍的体积,
    // 而排障靠的是后端的 request_id 与账本(它们在服务端,不在打包产物里)。
    sourcemap: false,
  },
  server: {
    // 开发态默认打哪个后端。写在这里而不是源码里:源码里同样的值在构建产物里就变成
    // "写死的地址",而 app 的契约是**只知道一个可配置的后端地址**。
    port: 5173,
    strictPort: true,
  },
})

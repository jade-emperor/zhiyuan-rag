import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接落进 Python 包里的静态目录:Python 侧 / Docker 镜像因此**不需要 node**。
// 代价是「改了前端必须重新构建」—— 有契约盯着产物存在且不早于源码(见 check_m7_frontend.py)。
export default defineConfig({
  // 相对 base:同一份产物既能挂在 /static/app/ 下,也能被桌面壳当本地文件打开。
  // 绝对 base('/static/app/')在 file:// 下会去找文件系统根目录,整个壳就白做了。
  base: './',
  plugins: [vue()],
  build: {
    // 相对 root(= web/)—— 刻意不用 node:url 拼绝对路径:那要引 @types/node,
    // 而这份配置里没有一处真的需要 node API。
    outDir: '../src/project_rag/api/static/app',
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

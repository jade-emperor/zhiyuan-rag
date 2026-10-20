/**
 * 容器冒烟（API-only）：compose 里的 app 默认只提供接口，不再在 8080 发页面。
 *
 * 这一份不启动浏览器：它验的是「容器起来后，后端该在的入口在、前端托管该关的关」。
 * 页面渲染归 `browser_m7_frontend.cjs`（Vite / 假后端）和桌面壳那套管。
 *
 * 跑法：
 *   BASE=http://127.0.0.1:8080/ node scripts/acceptance/browser_container_smoke.cjs
 *
 * 期望：
 *   GET /health      200 {"status":"ok"}
 *   GET /            200 api-only JSON（不是 307 到页面）
 *   GET /docs        200 Swagger UI
 *   GET /static/app/ 404（前端由 Vite / Nginx 单独发）
 */

const path = require('path')

const REPO_ROOT = path.resolve(__dirname, '../..')
const { request } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BASE = process.env.BASE || 'http://127.0.0.1:8080/'
const ROOT = BASE.endsWith('/') ? BASE : `${BASE}/`

let failures = 0
const ok = (name, detail) => console.log(`  ✔ ${name} — ${detail}`)
const bad = (name, detail) => { failures += 1; console.log(`  ✘ ${name} — ${detail}`) }

;(async () => {
  const context = await request.newContext({ baseURL: ROOT })

  const health = await context.get('health')
  const healthBody = await health.text()
  if (health.status() === 200 && healthBody.includes('"ok"')) ok('容器 API 存活', `${health.status()} ${healthBody}`)
  else bad('容器 API 存活', `${health.status()} ${healthBody.slice(0, 120)}`)

  const root = await context.get('')
  const rootBody = await root.text()
  const rootJson = (() => { try { return JSON.parse(rootBody) } catch { return null } })()
  if (root.status() === 200 && rootJson?.mode === 'api-only' && rootJson?.docs === '/docs') {
    ok('根路径是 API-only 说明，不跳页面', `${root.status()} ${rootBody.replace(/\s+/g, ' ').slice(0, 100)}`)
  } else {
    bad('根路径是 API-only 说明', `${root.status()} ${rootBody.slice(0, 120)}`)
  }

  const docs = await context.get('docs')
  if (docs.status() === 200 && (docs.headers()['content-type'] || '').includes('text/html')) {
    ok('Swagger 文档可达', `${docs.status()} ${docs.headers()['content-type']}`)
  } else {
    bad('Swagger 文档可达', `${docs.status()} ${docs.headers()['content-type'] || '(无 content-type)'}`)
  }

  const staticPage = await context.get('static/app/')
  if (staticPage.status() === 404) ok('静态前端没有由容器托管', '404（符合 API-only）')
  else bad('静态前端没有由容器托管', `期望 404，实际 ${staticPage.status()}`)

  await context.dispose()
  console.log(failures ? `\n${failures} 条没过` : '\n容器 API 冒烟：全过')
  process.exit(failures ? 1 : 0)
})()

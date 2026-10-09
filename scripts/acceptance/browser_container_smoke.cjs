/**
 * 容器冒烟：用真 Chromium 打开**容器**（compose 里那个 app 服务，宿主 8080）那份前端，
 * 确认「页面真的渲染出来」，而不只是「文件在、HTTP 200」。
 *
 * 与 `browser_m7_frontend.cjs` 的分工：那几套打的是**假后端 + 壳的回环服务**（可脚本化地
 * 走完登录/提问/退出）；这一份打**真容器**，只验页面能出来、登录页渲染得对、控制台干净 ——
 * 登录之后的问答要有真账号口令（账号在 MySQL 里、口令是 PBKDF2 摘要），那一步归作者点。
 *
 * 跑法：LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *        BASE=http://127.0.0.1:8080/ node .cache/pw/container_smoke.cjs
 */
// Playwright 的位置可用 PLAYWRIGHT_MODULE 覆盖（默认是开发机 .cache 下那份一次性安装，
// 见 docs/frontend.md §7 的三步准备）；换机器时不改源码，设个环境变量即可。
// 仓库根：从本文件位置推出来（scripts/acceptance/ → 上两级），别人克隆下来不用改源码。
const path = require('path')
const REPO_ROOT = path.resolve(__dirname, '../..')

const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BASE = process.env.BASE || 'http://127.0.0.1:8080/'
const EXE = process.env.CHROME_BIN
  || path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT || path.join(REPO_ROOT, '.cache/m7_container_login.png')

let failures = 0
const ok = (name, detail) => console.log(`  ✔ ${name} — ${detail}`)
const bad = (name, detail) => { failures += 1; console.log(`  ✘ ${name} — ${detail}`) }

;(async () => {
  const browser = await chromium.launch({ executablePath: EXE, args: ['--no-sandbox'] })
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } })
  const consoleErrors = []
  const pageErrors = []
  const caps = []
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
  page.on('pageerror', (e) => pageErrors.push(String(e)))
  page.on('response', (r) => {
    if (r.url().includes('/api/v1/capabilities')) caps.push(`${r.status()} ${r.url()}`)
  })

  await page.goto(BASE, { waitUntil: 'networkidle' })
  const url = page.url()
  if (url.includes('/static/app/') && url.includes('#/login')) {
    ok('根路径 307 → 前端产物，hash 路由落到登录页', url)
  } else {
    bad('根路径 / 路由', `最终 URL 不是 /static/app/#/login：${url}`)
  }

  const card = page.locator('.card.login')
  if (await card.isVisible().catch(() => false)) ok('登录卡片渲染出来', '.card.login 可见')
  else bad('登录卡片', '.card.login 不可见')

  const user = page.locator('input[autocomplete="username"]')
  const pass = page.locator('input[autocomplete="current-password"]')
  if (await user.isVisible().catch(() => false) && await pass.isVisible().catch(() => false)) {
    ok('账号 / 口令两个输入框都在', 'autocomplete=username / current-password')
  } else {
    bad('输入框', '账号或口令框不可见')
  }

  const button = page.locator('button.primary.block')
  ok('提交按钮文案', (await button.innerText().catch(() => '(读不到)')).trim())

  if (caps.some((line) => line.startsWith('200'))) ok('能力自述来自容器', caps.join(' / '))
  else bad('能力自述', caps.length ? caps.join(' / ') : '页面根本没打 /api/v1/capabilities')

  await page.screenshot({ path: SHOT })
  ok('截图', SHOT)

  if (consoleErrors.length === 0) ok('正常路径无 console 报错', '0 条')
  else bad('console 报错', consoleErrors.join(' | '))
  if (pageErrors.length === 0) ok('无未捕获异常', '0 条')
  else bad('未捕获异常', pageErrors.join(' | '))

  await browser.close()
  console.log(failures ? `\n${failures} 条没过` : '\n容器冒烟：全过')
  process.exit(failures ? 1 : 0)
})()

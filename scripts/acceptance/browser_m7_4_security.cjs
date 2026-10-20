/**
 * 安全收口的浏览器实测：桌面壳场景（`file://` 打开本地产物）+ 安全收口在界面上的表现。
 *
 * **这台机器没有图形界面**，所以 `pywebview` 开窗那一步验不了（判定标准写在
 *）。但壳的**核心假设**可以验：产物是本地文件、后端地址可配、
 * 跨源能连上。真 Chromium 打开 `file://…/static/app/index.html` 就是那个场景 ——
 *
 *   0. 先用 `python -m project_rag.desktop --serve --backend …` 起**桌面壳自己的**回环服务，
 *      把打印出来的 URL 交给这个脚本（`SHELL_URL=…`）—— 验的就是壳真正会打开的那个地址；
 *   1. `?backend=` 被认下来（存进 localStorage）并从地址栏抹掉；
 *   2. 登录页渲染、后端「已连通」——**跨源**(file:// 的 Origin 是 null)也连得上；
 *   3. 用 admin 登录 → 提问 → 流式答案出来（WS 跨源也通）；
 *   4. 退出登录 → **服务端那枚令牌立刻 401**（不是只删了本地副本）；
 *   5. 登录连错到超限 → 界面显示「…过于频繁」并给出等待秒数（限流在界面上看得见）。
 *      （这句文案住在 `api/app.py:_throttle_text`：**登录/上传是 429 的拦截话术**，
 *       与问答那条 200 的兜底话术（「问得有点密集」）不是同一句。）
 *
 * 跑法(准备)：
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python -m project_rag.desktop --serve --backend http://127.0.0.1:18081
 *   SHELL_URL=<上一步打印的地址> LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_4_security.cjs
 */

// Playwright 的位置可用 PLAYWRIGHT_MODULE 覆盖（默认是开发机 .cache 下那份一次性安装，
// 的三步准备）；换机器时不改源码，设个环境变量即可。
// 仓库根：从本文件位置推出来（scripts/acceptance/ → 上两级），别人克隆下来不用改源码。
const path = require('path')
const REPO_ROOT = path.resolve(__dirname, '../..')

const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BACKEND = 'http://127.0.0.1:18081'
// **桌面壳真的会用的那个地址**:由 `project-rag-desktop --serve` 打印(回环 HTTP + ?backend=)。
// 为什么不是 file://:产物的 ES module 在 file:// 下会被 CORS 挡掉(实测过,页面白屏),
// 所以壳自己起一个回环服务 —— 这里就照着那个路径验。
const SHELL_URL = process.env.SHELL_URL || 'http://127.0.0.1:45285/index.html?backend=http://127.0.0.1:18081'
const EXE =
  process.env.CHROME_BIN ||
  path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT_PREFIX || path.join(REPO_ROOT, '.cache/m7_shell')

let failures = 0
function expect(name, condition, detail) {
  if (condition) console.log(`  ✔ ${name} — ${detail}`)
  else {
    failures += 1
    console.log(`  ✘ ${name} — ${detail}`)
  }
}

async function main() {
  const browser = await chromium.launch({ executablePath: EXE, args: ['--no-sandbox'] })
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))

  // ── 1. 桌面壳场景:本地文件 + ?backend= ────────────────────────────────
  await page.goto(SHELL_URL, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=知源助手', { timeout: 15000 })

  const stored = await page.evaluate(() => localStorage.getItem('project-rag.backend'))
  expect('?backend= 被记下来了', stored === BACKEND, `localStorage = ${stored}`)
  const cleaned = await page.evaluate(() => window.location.search)
  expect('?backend= 已从地址栏抹掉', cleaned === '', `search = ${cleaned || '(空)'}`)
  const connected = await page.locator('text=已连通').count()
  expect('本地文件也能连上后端(跨源)', connected === 1, '后端标记「已连通」')
  await page.screenshot({ path: `${SHOT}_1_file_login.png` })

  // ── 2. 登录 + 提问(WS 跨源) ───────────────────────────────────────────
  // **先等登录那一档的空额回来**:这条用例自己会用 11 次错误口令把那个 IP 的额度打光,
  // 而限额窗口内**连正确密码也进不去**(限流的应有之义,不是 bug)。于是"紧接着再跑一次"
  // 会卡在登录后那一页(实测:第二次跑就卡住了)。探到还能失败一次(401)再开始。
  for (let waited = 0; ; waited += 5) {
    const probe = await page.request.post(`${BACKEND}/api/v1/auth/login`, {
      data: { username: 'admin', password: 'bucket-probe' },
    })
    if (probe.status() === 401) break
    if (probe.status() !== 429) throw new Error(`探测登录额度时收到 ${probe.status()}`)
    if (waited >= 90) throw new Error('登录额度等了一分半还没回来 —— 限流器是不是卡住了?')
    console.log(`  … 上一次把登录额度用光了,等 ${5} 秒(累计 ${waited + 5}s)`)
    await page.waitForTimeout(5000)
  }

  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('text=Configuration', { timeout: 15000 })

  await page.locator('textarea').fill('每周加班最多多长时间')
  await page.getByRole('button', { name: '发送' }).click()
  await page.waitForSelector('.citations', { timeout: 20000 })
  const answer = await page.locator('.row.assistant .text').last().innerText()
  expect('本地壳里问答也通(WS 跨源)', answer.includes('每日不超过八小时'), answer.slice(0, 24))
  await page.screenshot({ path: `${SHOT}_2_file_chat.png` })

  // 到这里为止都是**正常路径**:不该有任何 console 报错。
  // (后面的负例 —— 故意错口令、故意超限 —— 会打出 401/429 的资源加载噪声,那是预期的,
  //  所以那时只断言"没有未捕获的异常"。)
  expect('正常路径上没有 console 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')

  // ── 3. 退出 → 服务端撤销 ──────────────────────────────────────────────
  const token = await page.evaluate(() => localStorage.getItem('project-rag.token'))
  expect('登录后本地有令牌', typeof token === 'string' && token.length > 10, `${String(token).slice(0, 8)}…`)

  await page.getByRole('button', { name: '退出' }).click()
  await page.waitForSelector('text=没有注册入口', { timeout: 15000 })
  const afterLocal = await page.evaluate(() => localStorage.getItem('project-rag.token'))
  expect('退出后本地令牌清掉了', afterLocal === null, `localStorage = ${afterLocal}`)

  const reused = await page.request.get(`${BACKEND}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(
    '退出后旧令牌在服务端也失效(401)',
    reused.status() === 401,
    `GET /auth/me → ${reused.status()}`,
  )

  // ── 4. 登录限流在界面上看得见 ─────────────────────────────────────────
  for (let i = 0; i < 11; i += 1) {
    await page.getByLabel('用户名').fill('admin')
    await page.getByLabel('密码').fill('definitely-wrong')
    await page.getByRole('button', { name: '登录' }).click()
    await page.waitForTimeout(120)
  }
  await page.waitForFunction(
    () => /过于频繁/.test(document.body.innerText),
    undefined,
    { timeout: 10000 },
  )
  const alertText = await page.locator('.alert.error').first().innerText()
  expect(
    '超限时界面说的是「等几秒」(可执行)',
    /过于频繁/.test(alertText) && /等\s*约?\s*\d+\s*秒/.test(alertText),
    alertText.slice(0, 48),
  )
  await page.screenshot({ path: `${SHOT}_3_rate_limited.png` })

  expect('负例阶段没有未捕获的异常', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(failures === 0 ? '\n桌面壳场景浏览器实测:全部通过' : `\n桌面壳场景浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

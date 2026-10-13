/**
 * M7.1 的浏览器实测（人工那一步的脚本化版本）。
 *
 * 对着 `.cache/m7_fake_backend.py`（真 uvicorn + 假依赖，18081）跑一遍真实用户流程，
 * 每条都同时看**渲染结果**与**后端收到的请求**：
 *
 *   1. `/` 被 307 到 /static/app/，登录页渲染出来；
 *   2. 管理员登录 → 左侧会话列表有 2 条、顶部有 3 个页签；
 *   3. 点会话 → 历史回填（2 轮）；
 *   4. 新会话 → 提问 → 「依据 N 条」先到、答案逐字出、引用卡片带页码 7 与「页码未知」；
 *   5. 改成「关键字检索」再问 → 后端收到的是 mode=keyword（不是权重）；
 *   6. 退出 → 员工登录 → 顶部只剩「对话」。
 *
 * 跑法(两件一次性准备:playwright 包 + 一个 Chromium;都放 `.cache/`,不进仓库)::
 *
 *   mkdir -p .cache/pw && cd .cache/pw && npm i playwright --cache ../.cache/npm-cache
 *   # 下载 Chrome for Testing(国内走 npmmirror;cdn.playwright.dev 会转到 Google Storage,很慢)
 *   curl -L -o .cache/chrome/headless-shell.zip \
 *     https://registry.npmmirror.com/-/binary/chrome-for-testing/<版本>/linux64/chrome-headless-shell-linux64.zip
 *   # 这台机器缺 libnss3 / libasound2,非 root 装不了 → 下 .deb 解到本地,用 LD_LIBRARY_PATH 指过去
 *   cd .cache/pwlibs && apt-get download libnss3 libasound2t64 libnspr4 && \
 *     for d in *.deb; do dpkg-deb -x "$d" root/; done
 *
 *   # 一个终端起假后端,另一个终端跑这个脚本:
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_frontend.cjs
 *
 * ⚠️ **容器里没有中文字体**:中文在截图里会渲染成方框(豆腐块)。那是环境缺字体,
 * 不是界面坏了 —— 看布局、看行为都不受影响(见 `docs/frontend.md` §7)。
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

const BASE = 'http://127.0.0.1:18081/'
const EXE = process.env.CHROME_BIN
  || path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT_PREFIX || path.join(REPO_ROOT, '.cache/m7_ui')

let failures = 0
function ok(name, detail) {
  console.log(`  ✔ ${name} — ${detail}`)
}
function bad(name, detail) {
  failures += 1
  console.log(`  ✘ ${name} — ${detail}`)
}
function expect(name, condition, detail) {
  if (condition) ok(name, detail)
  else bad(name, detail)
}

async function main() {
  const browser = await chromium.launch({ executablePath: EXE, args: ['--no-sandbox'] })
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`))
  const failedRequests = []
  page.on('requestfailed', (request) =>
    failedRequests.push(`${request.url()} ${request.failure() && request.failure().errorText}`),
  )

  // ── 1. 根路径 → 前端 app ──────────────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=RAG Assistant', { timeout: 15000 })
  // 路由守卫会带上 ?next=…,所以只断言"落在 /static/app/ 下的登录页"
  expect(
    '根路径跳到前端 app',
    page.url().includes('/static/app/#/login'),
    `url=${page.url()}`,
  )
  await page.screenshot({ path: `${SHOT}_1_login.png` })

  // 后端地址默认取本页 origin（假后端就在同一个 origin 上）
  const connected = await page.locator('text=已连通').count()
  expect('登录页探到后端', connected === 1, `「已连通」出现 ${connected} 次`)

  // ── 2. 管理员登录 ────────────────────────────────────────────────────
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('text=Configuration', { timeout: 15000 })

  const sessions = await page.locator('.sidebar .item').count()
  expect('管理员会话列表', sessions === 2, `左侧 ${sessions} 条会话`)
  const tabs = await page.locator('header .tab').allInnerTexts()
  // P0-6:管理员的页签是四个(对话 / 知识库 / 报表 / 账号与权限)
  expect(
    '管理员看到四个页签',
    tabs.length === 4 &&
      tabs.includes('知识库') &&
      tabs.includes('报表') &&
      tabs.includes('账号与权限'),
    `页签 ${JSON.stringify(tabs)}`,
  )
  const roleTag = await page.locator('header .who').innerText()
  expect('角色标签显示管理员', roleTag.includes('管理员'), roleTag.replace(/\n/g, ' '))
  await page.screenshot({ path: `${SHOT}_2_chat.png` })

  // ── 3. 点会话 → 历史回填 ─────────────────────────────────────────────
  await page.locator('.sidebar .item', { hasText: '每周加班最多多长时间' }).click()
  await page.waitForSelector('.messages .row', { timeout: 10000 })
  const rows = await page.locator('.messages .row').count()
  expect('切换会话回填历史', rows === 4, `${rows} 条消息（2 轮问答）`)

  // ── 4. 新会话 → 提问 → 引用与页码 ────────────────────────────────────
  await page.getByRole('button', { name: '+ 新会话' }).click()
  await page.locator('textarea').fill('每周加班最多多长时间')
  await page.getByRole('button', { name: '发送' }).click()
  await page.waitForSelector('.citations', { timeout: 20000 })
  const answer = await page.locator('.row.assistant .text').last().innerText()
  expect('流式答案渲染', answer.includes('每日不超过八小时'), answer.slice(0, 40))
  const citations = await page.locator('.citation').count()
  expect('引用卡片', citations === 2, `${citations} 条引用`)
  const pageTag = await page.locator('.citation .tag.ok').first().innerText()
  expect('PDF 引用带页码', pageTag.includes('第 7 页'), pageTag)
  const unknown = await page.locator('.citation .tag.soft', { hasText: '页码未知' }).count()
  expect('md 引用显示页码未知', unknown === 1, `「页码未知」出现 ${unknown} 次`)
  await page.screenshot({ path: `${SHOT}_3_answer.png` })

  // ── 5. 参数面板真的改了请求 ──────────────────────────────────────────
  await page.locator('select').first().selectOption('keyword')
  const weightsHidden = await page.locator('text=两路权重').count()
  expect('单路时权重滑条收起', weightsHidden === 0, `「两路权重」出现 ${weightsHidden} 次`)
  await page.locator('textarea').fill('年假怎么申请')
  await page.getByRole('button', { name: '发送' }).click()
  await page.waitForTimeout(2500)
  // 这一条同时验两件事:① 面板改了真的发出去;② 发出去的形状是**对的**
  // ——单路只发 mode、不发权重(同时发会被后端 422,界面上就是一条红字)。
  const errors = await page.locator('.messages .alert.error').allInnerTexts()
  expect('关键字检索这一问被后端接受', errors.length === 0, errors.join(' | ') || '没有错误提示')
  const keywordAnswer = await page.locator('.row.assistant .text').last().innerText()
  const lastCitations = await page
    .locator('.row.assistant')
    .last()
    .locator('.citation')
    .count()
  // 假后端的关键字索引是**空的**:所以"只走关键字"这一档必然拒答、且没有引用;
  // 要是前端偷偷把两路都发了(或没发 mode),向量那一路会把它答出来 —— 这条断言就是这么看见 mode 的。
  expect(
    '关键字检索确实只走了关键字那一路',
    keywordAnswer.includes('信息不足') && lastCitations === 0,
    `答案「${keywordAnswer.slice(0, 16)}」/ 引用 ${lastCitations} 条`,
  )

  // ── 6. 员工登录只剩「对话」 ─────────────────────────────────────────
  await page.getByRole('button', { name: '退出' }).click()
  await page.waitForSelector('text=没有注册入口', { timeout: 10000 })
  await page.getByLabel('用户名').fill('employee')
  await page.getByLabel('密码').fill('emp-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('text=Configuration', { timeout: 15000 })
  const empTabs = await page.locator('header .tab').allInnerTexts()
  expect('员工只见「对话」(知识库页里有上传/删除/授权,所以那一页只给管理员)', empTabs.length === 1 && empTabs[0] === '对话', JSON.stringify(empTabs))
  const empSessions = await page.locator('.sidebar .item').count()
  expect('员工的会话列表是自己的', empSessions === 1, `${empSessions} 条`)
  await page.screenshot({ path: `${SHOT}_4_employee.png` })

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(failures === 0 ? '\n浏览器实测:全部通过' : `\n浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

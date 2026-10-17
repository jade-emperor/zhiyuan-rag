/**
 * 可见范围的浏览器实测:角色分流 + 账号与权限页 + 文档可见范围。
 *
 * 对着 `scripts/acceptance/serve_fake_backend.py`(真 uvicorn + 假依赖 + 假权限存储,18081)跑:
 *
 *   1. 管理员登录 → 四个页签(对话 / 知识库 / 报表 / 账号与权限),账号页列出三个账号与「销售」组;
 *   2. 建一个新组 → 出现在列表里(后端真收到了 PUT/POST);
 *   3. 知识库页那张**受限文档**卡片带「限定可见」标记 → 点「权限」→ 弹层里「销售」是勾上的
 *      → 清空名单保存 → 标记消失、弹层说「人人可见」;
 *   4. **运维登录** → 页签是「对话 + 报表」(他要能问答);手输 `#/knowledge` 会被守卫送回;
 *   5. 运维能读知识库清单与报表(200),而**管理动作**(建号 / 删文档 / 授权)是 403;
 *   6. **组侧授权**:「销售」组能看哪些文档(三态)、勾上「看不到」的那份并保存
 *      (这一段排在文档侧授权之前 —— 那一段会把名单清空);
 *   6. 0 条 JS 报错 / 0 个失败请求。
 *
 * 跑法(准备)：
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_p0_6_access.cjs
 */

const path = require('node:path')

const REPO_ROOT = path.resolve(__dirname, '../..')

const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BASE = 'http://127.0.0.1:18081/'
const EXE =
  process.env.CHROME_BIN ||
  path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT_PREFIX || path.join(REPO_ROOT, '.cache/p0_6_access')

let failures = 0
function expect(name, condition, detail) {
  if (condition) console.log(`  ✔ ${name} — ${detail}`)
  else {
    failures += 1
    console.log(`  ✘ ${name} — ${detail}`)
  }
}

async function login(page, username, password) {
  // 等的是**顶部导航**(登录成功的标志),而不是某一页的内容 ——
  // 不同角色落在不同页(运维现在落在「对话」),等某一页的控件会等不到。
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('用户名').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('header .who', { timeout: 15000 })
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

  // ── 1. 管理员:四个页签 + 账号页 ───────────────────────────────────────
  await login(page, 'admin', 'admin-pass')
  const tabs = await page.locator('header .tab').allInnerTexts()
  expect(
    '管理员看到四个页签',
    tabs.length === 4 && tabs.join(',') === '对话,知识库,报表,账号与权限',
    JSON.stringify(tabs),
  )
  const roleTag = await page.locator('header .who').innerText()
  expect('角色标签是「管理员」', roleTag.includes('管理员'), roleTag.replace(/\n/g, ' '))

  await page.getByRole('link', { name: '账号与权限' }).click()
  await page.waitForSelector('h1:has-text("账号与权限")', { timeout: 15000 })
  await page.waitForSelector('tbody tr', { timeout: 15000 })

  const users = await page.locator('section', { hasText: '账号' }).locator('tbody tr td:first-child').allInnerTexts()
  expect(
    '账号表列出三个账号',
    ['admin', 'employee', 'ops'].every((name) => users.some((cell) => cell.includes(name))),
    JSON.stringify(users.map((t) => t.replace(/\n/g, ' '))),
  )
  const groupNames = await page
    .locator('section', { hasText: '分组' })
    .locator('tbody tr td:first-child')
    .allInnerTexts()
  expect(
    '分组表里有种子的「销售」组',
    groupNames.some((cell) => cell.includes('销售')),
    JSON.stringify(groupNames.map((t) => t.replace(/\n/g, ' '))),
  )
  const opsRow = await page.locator('tbody tr', { hasText: 'ops' }).first().innerText()
  expect('运维那一行标着「运维(问答 + 报表)」', opsRow.includes('运维'), opsRow.replace(/\n/g, ' | '))
  await page.screenshot({ path: `${SHOT}_1_accounts.png` })

  // ── 2. 建一个组(后端真的收到了)──────────────────────────────────────
  // 组名带时间戳:假后端是**进程级**的,同一个进程里重复跑这条用例不该被"重名 400"卡住。
  const newGroup = `财务${Date.now() % 100000}`
  await page.locator('section', { hasText: '分组' }).locator('input[placeholder*="组名"]').fill(newGroup)
  await page.getByRole('button', { name: '建组' }).click()
  await page.waitForFunction(
    (name) => document.body.innerText.includes(`已建组:${name}`),
    newGroup,
    { timeout: 15000 },
  )
  const afterCreate = await page
    .locator('section', { hasText: '分组' })
    .locator('tbody tr td:first-child')
    .allInnerTexts()
  expect(
    '新组出现在列表里',
    afterCreate.some((cell) => cell.includes(newGroup)),
    JSON.stringify(afterCreate.map((t) => t.replace(/\n/g, ' '))),
  )

  // ── 3. 组侧授权:这个组能看哪些文档(按组定制) ──────────────────
  // **必须排在文档侧那段之前**:那一段会清空「内部报价单」的名单(它是有意要验"回到人人可见"),
  // 一清就没有"已授权本组"这个状态可看了
  await page.getByRole('link', { name: '账号与权限' }).click()
  await page.waitForSelector('tbody tr', { timeout: 15000 })
  // 用「有没有那个按钮」定位分组行:账号表的"所属组"列里也会出现「销售」,
  // 只按文本找会点到账号行上(第一次就是这么超时的)
  const groupRow = page
    .locator('tbody tr', { has: page.getByRole('button', { name: '文档权限' }) })
    .filter({ hasText: '销售' })
    .first()
  await groupRow.getByRole('button', { name: '文档权限' }).click()
  const groupDialog = page.locator('.dialog[aria-label="组的文档权限"]')
  await groupDialog.waitFor({ timeout: 15000 })
  // 弹层挂上 ≠ 数据到了:行是拿到三态之后才渲染的,不等它读到的是一份空列表
  await groupDialog.locator('.row').first().waitFor({ timeout: 15000 })
  const groupRows = await groupDialog.locator('.row').allInnerTexts()
  const joined = groupRows.map((text) => text.replace(/\n/g, ' ')).join(' || ')
  expect(
    '组侧授权页三态齐全(已授权 / 看不到 / 人人可见)',
    joined.includes('已授权本组') && joined.includes('看不到') && joined.includes('人人可见'),
    joined.slice(0, 240),
  )
  expect(
    '看不到的那份点了「取消后回到人人可见」的提醒',
    joined.includes('取消后这份会回到人人可见'),
    joined.slice(0, 240),
  )
  // 把「员工手册.md」(现在看不到)勾上 → 保存 → 变成已授权本组
  const handBook = groupDialog.locator('.row', { hasText: '员工手册.md' }).first()
  await handBook.locator('input[type=checkbox]').check()
  await groupDialog.getByRole('button', { name: '保存（整份名单）' }).click()
  await page.waitForTimeout(600)
  const afterGrant = (await groupDialog.locator('.row', { hasText: '员工手册.md' }).first().innerText()).replace(/\n/g, ' ')
  expect('勾上 + 保存之后那份变成了「已授权本组」', afterGrant.includes('已授权本组'), afterGrant)
  const notice = await groupDialog.locator('.alert.info').first().innerText()
  expect('保存后如实回报改了什么', notice.includes('新授权 1 份'), notice)
  await page.screenshot({ path: `${SHOT}_4_group_documents.png` })
  await groupDialog.getByRole('button', { name: '关闭' }).click()
  await page.waitForTimeout(400)
  // 回知识库:下一段(文档侧授权弹层)在这一页上
  await page.getByRole('link', { name: '知识库' }).click()
  await page.waitForSelector('.doc', { timeout: 15000 })

  // ── 3. 文档可见范围:卡片标记 → 权限弹层 → 清空名单 ────────────────────
  await page.getByRole('link', { name: '知识库' }).click()
  await page.waitForSelector('.doc', { timeout: 15000 })
  const quoteCard = page.locator('.doc', { hasText: '内部报价单.md' }).first()
  const cardText = await quoteCard.innerText()
  expect('受限文档的卡片标着「限定可见」', cardText.includes('限定可见'), cardText.replace(/\n/g, ' | ').slice(0, 120))

  await quoteCard.getByRole('button', { name: '权限' }).click()
  await page.waitForSelector('div[aria-label="文档权限"]', { timeout: 15000 })
  const dialog = page.locator('div[aria-label="文档权限"]')
  // 等规则真的读回来:徽章从「读取中…」变成结论,否则读到的会是加载中的中间态。
  await dialog.locator('.tag', { hasText: /限定|人人可见/ }).waitFor({ timeout: 15000 })
  const dialogText = await dialog.innerText()
  expect(
    '弹层说清现状(限定:只有名单里的人能看)',
    dialogText.includes('限定'),
    dialogText.replace(/\n/g, ' | ').slice(0, 160),
  )
  const salesChecked = await dialog
    .locator('label', { hasText: '销售' })
    .locator('input[type=checkbox]')
    .isChecked()
  expect('「销售」组是勾上的(后端读回来的)', salesChecked === true, `checked=${salesChecked}`)
  await page.screenshot({ path: `${SHOT}_2_acl_dialog.png` })

  await dialog.getByRole('button', { name: '清空名单(= 取消限制)' }).click()
  await dialog.getByRole('button', { name: '保存(整份名单)' }).click()
  await page.waitForFunction(
    () => document.body.innerText.includes('人人可见'),
    undefined,
    { timeout: 15000 },
  )
  const afterSave = await dialog.innerText()
  expect(
    '保存后如实说「回到人人可见」',
    afterSave.includes('人人可见'),
    afterSave.replace(/\n/g, ' | ').slice(0, 160),
  )
  await dialog.getByRole('button', { name: '关闭' }).click()
  await page.waitForTimeout(1200)
  const cardAfter = await page.locator('.doc', { hasText: '内部报价单.md' }).first().innerText()
  expect('卡片上的「限定可见」跟着消失', !cardAfter.includes('限定可见'), cardAfter.replace(/\n/g, ' | ').slice(0, 120))

  // ── 4. 运维:能问答 + 能看报表;管理动作被拦 ──────────────────
  const opsPage = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  const opsErrors = []
  opsPage.on('pageerror', (error) => opsErrors.push(error.message))
  await login(opsPage, 'ops', 'ops-pass')
  const opsTabs = await opsPage.locator('header .tab').allInnerTexts()
  expect(
    '运维看到「对话 + 报表」两个页签(不再只有报表)',
    opsTabs.join(',') === '对话,报表',
    JSON.stringify(opsTabs),
  )
  const opsRole = await opsPage.locator('header .who').innerText()
  expect('角色标签是「运维」', opsRole.includes('运维'), opsRole.replace(/\n/g, ' '))

  await opsPage.goto(`${BASE}#/knowledge`, { waitUntil: 'domcontentloaded' })
  await opsPage.waitForTimeout(800)
  expect(
    '手输 #/knowledge 被守卫送回去(运维管不了文档)',
    /#\/(chat|reports)/.test(opsPage.url()),
    opsPage.url(),
  )

  // 运维的报表能读(页面渲染出请求数);而"能读清单"不等于"能管"
  await opsPage.goto(`${BASE}#/reports`, { waitUntil: 'domcontentloaded' })
  await opsPage.waitForSelector('.metric', { timeout: 15000 })
  const opsCalls = await opsPage.evaluate(async (base) => {
    const token = window.localStorage.getItem('project-rag.token')
    const withToken = { Authorization: `Bearer ${token}` }
    const get = async (path) => (await fetch(`${base}api/v1${path}`, { headers: withToken })).status
    const del = async (path) =>
      (await fetch(`${base}api/v1${path}`, { method: 'DELETE', headers: withToken })).status
    return {
      documents: await get('/documents'),
      users: await get('/admin/users'),
      remove: await del('/documents/内部报价单.md'),
    }
  }, BASE)
  expect(
    '运维能读知识库清单(他要能问答,先得知道库里有啥)',
    opsCalls.documents === 200,
    `status=${opsCalls.documents}`,
  )
  expect(
    '运维的管理动作被拦(建号 / 删文档都要管理员)',
    opsCalls.users === 403 && opsCalls.remove === 403,
    JSON.stringify(opsCalls),
  )
  await opsPage.screenshot({ path: `${SHOT}_3_ops_reports.png` })
  expect('运维那一页没有 JS 报错', opsErrors.length === 0, opsErrors.slice(0, 2).join(' | ') || '无')

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(failures === 0 ? '\n角色与可见范围浏览器实测:全部通过' : `\n角色与可见范围浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})

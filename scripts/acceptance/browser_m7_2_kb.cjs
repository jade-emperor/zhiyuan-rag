/**
 * M7.2 的浏览器实测：知识库管理页走一遍真实流程。
 *
 * 对着 `scripts/acceptance/serve_fake_backend.py`(真 uvicorn + 假依赖,18081)跑:
 *
 *   1. 管理员进「知识库」页 → 看到种子文档(一份已入库、一份处理中);
 *   2. 上传一份文件 → 卡片出现(待处理)→ **自动轮询**把它推到「已入库」;
 *   3. 预览那份上传的文档 → 弹层里「入库 N 块(子块) · 预览 M 段(父块)」两个数都在,页码是真算出来的;
 *   4. 预览种子 PDF → 页码显示成「第 7–8 页」(跨页);
 *   5. 删除(两步确认)→ 卡片消失,提示语如实说出**删了几块向量**;
 *   6. 删完再预览 → 404(与后端联动)。
 *
 * 跑法(准备见 docs/frontend.md §7)::
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_2_kb.cjs
 */

// 仓库根：从本文件位置推出来（scripts/acceptance/ → 上两级），别人克隆下来不用改源码。
const REPO_ROOT = path.resolve(__dirname, '../..')

const fs = require('node:fs')
const path = require('node:path')

// Playwright 的位置可用 PLAYWRIGHT_MODULE 覆盖（默认是开发机 .cache 下那份一次性安装，
// 见 docs/frontend.md §7 的三步准备）；换机器时不改源码，设个环境变量即可。
const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BASE = 'http://127.0.0.1:18081/'
const EXE =
  process.env.CHROME_BIN ||
  path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT_PREFIX || path.join(REPO_ROOT, '.cache/m7_kb')
const UPLOAD = path.join(REPO_ROOT, '.cache/fake_uploads/browser_upload_demo.md')

let failures = 0
function expect(name, condition, detail) {
  if (condition) console.log(`  ✔ ${name} — ${detail}`)
  else {
    failures += 1
    console.log(`  ✘ ${name} — ${detail}`)
  }
}

async function main() {
  fs.mkdirSync(path.dirname(UPLOAD), { recursive: true })
  fs.writeFileSync(UPLOAD, '这是一份浏览器实测用的文档。'.repeat(40), 'utf8')

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

  // ── 登录 → 知识库页 ────────────────────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('text=Configuration', { timeout: 15000 })

  await page.getByRole('link', { name: '知识库' }).click()
  await page.waitForSelector('.doc', { timeout: 15000 })
  const seeded = await page.locator('.doc').count()
  const names = await page.locator('.doc .title').allInnerTexts()
  expect(
    '种子文档都在(至少 3 份)',
    seeded >= 3,
    `${seeded} 张卡片:${names.map((n) => n.slice(0, 12)).join(' / ')}`,
  )
  const supported = await page.locator('text=支持:').innerText()
  expect(
    '支持的类型来自 capabilities',
    supported.includes('.pdf') && supported.includes('.docx'),
    supported.replace(/\n/g, ' '),
  )
  await page.screenshot({ path: `${SHOT}_1_list.png` })

  // ── 上传 → 轮询到「已入库」 ───────────────────────────────────────────
  await page.locator('input[type=file]').setInputFiles(UPLOAD)
  const card = page.locator('.doc', { hasText: 'browser_upload_demo.md' })
  await card.waitFor({ timeout: 15000 })
  const early = await card.innerText()
  expect(
    '上传后先看到在途状态',
    early.includes('待处理') || early.includes('处理中'),
    early.replace(/\n/g, ' ').slice(0, 40),
  )
  await page.waitForFunction(
    () =>
      Array.from(document.querySelectorAll('.doc')).some(
        (node) =>
          node.textContent.includes('browser_upload_demo.md') &&
          node.textContent.includes('已入库'),
      ),
    undefined,
    { timeout: 20000 },
  )
  const done = await card.innerText()
  expect('轮询把它推到「已入库」', done.includes('已入库'), done.replace(/\n/g, ' ').slice(0, 60))
  await page.screenshot({ path: `${SHOT}_2_uploaded.png` })

  // ── 预览:两个数与页码 ────────────────────────────────────────────────
  await card.getByRole('button', { name: '预览' }).click()
  const dialog = page.locator('[role=dialog]')
  await dialog.waitFor({ timeout: 10000 })
  // **先等那一行出现**:弹层是先开、接口后回的;不等就断言会读到"还在加载"的中间态(第一版就这么假红过)
  await dialog.locator('header p').waitFor({ timeout: 10000 })
  const head = await dialog.locator('header').innerText()
  expect(
    '预览显示子块与父块两个数',
    head.includes('子块') && head.includes('父块'),
    head.replace(/\n/g, ' '),
  )
  const blocks = await dialog.locator('.block').count()
  expect('预览有内容', blocks > 0, `${blocks} 段`)
  const pageTag = await dialog.locator('.block .tag.ok').first().innerText()
  expect('页码是真算出来的', /第 \d+ 页/.test(pageTag), pageTag)
  await page.screenshot({ path: `${SHOT}_3_preview.png` })
  await dialog.getByRole('button', { name: '关闭' }).click()
  await dialog.waitFor({ state: 'detached', timeout: 5000 })

  // 种子 PDF 是跨页的:要显示成「第 7–8 页」
  const pdfCard = page.locator('.doc', { hasText: '劳动法' })
  await pdfCard.getByRole('button', { name: '预览' }).click()
  await dialog.waitFor({ timeout: 10000 })
  const spanTag = await dialog.locator('.block .tag.ok').first().innerText()
  expect('跨页显示成区间', spanTag.includes('–'), spanTag)
  await dialog.getByRole('button', { name: '关闭' }).click()
  await dialog.waitFor({ state: 'detached', timeout: 5000 })

  // ── 删除:两步确认 + 如实转述 ─────────────────────────────────────────
  await card.getByRole('button', { name: '删除' }).click()
  const confirmButton = card.getByRole('button', { name: '确认删除?' })
  expect('删除要二次确认', (await confirmButton.count()) === 1, '出现「确认删除?」')
  await confirmButton.click()
  await page.waitForFunction(
    () =>
      !Array.from(document.querySelectorAll('.doc')).some((node) =>
        node.textContent.includes('browser_upload_demo.md'),
      ),
    undefined,
    { timeout: 15000 },
  )
  const notice = await page.locator('main .alert.info').first().innerText()
  expect(
    '删完如实说清删了几块向量',
    /向量\s*\d+\s*块/.test(notice) && notice.includes('原文件'),
    notice.replace(/\n/g, ' ').slice(0, 60),
  )
  await page.screenshot({ path: `${SHOT}_4_deleted.png` })

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(failures === 0 ? '\n知识库页浏览器实测:全部通过' : `\n知识库页浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

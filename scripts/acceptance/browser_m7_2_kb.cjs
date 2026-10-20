/**
 * 知识库**两级页面**的浏览器实测：走一遍真实流程。
 *
 * 对着 `scripts/acceptance/serve_fake_backend.py`(真 uvicorn + 假依赖,18081)跑:
 *
 *   1. 管理员进「知识库」页 → **一级是目录卡**(公司制度 / 产品资料 / 未分类),读数来自对账;
 *   2. 点「公司制度」→ **二级是文档表格**,五种 state 各按 `state` 显示;
 *   3. 上传一份文件到这一格 → 行出现(在途)→ **自动轮询**把它推到「已入库」;
 *   4. 预览 → 弹层里「入库 N 块(子块) · 预览 M 段(父块)」两个数都在,页码是真算出来的;
 *   5. 预览种子 PDF → 页码显示成「第 7–8 页」(跨页);
 *   6. 删除(两步确认)→ 行消失,提示语如实说出**删了几块向量**;
 *   7. 未登记的文件在「未分类」里,而且**没有删除按钮**(它没有登记行)。
 *
 * 跑法(准备)：
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_2_kb.cjs
 */

const fs = require('node:fs')
const path = require('node:path')

// 仓库根：从本文件位置推出来（scripts/acceptance/ → 上两级），别人克隆下来不用改源码。
const REPO_ROOT = path.resolve(__dirname, '../..')

// Playwright 的位置可用 PLAYWRIGHT_MODULE 覆盖（默认是开发机 .cache 下那份一次性安装，
// 的三步准备）；换机器时不改源码，设个环境变量即可。
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

/** 输入:页面。行为:回一级页面(目录卡)并等它渲染完。输出:无。 */
async function backToFolders(page) {
  await page.goto(`${BASE}static/app/#/knowledge`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.folder', { timeout: 15000 })
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

  // ── 登录 → 知识库一级页面(目录) ───────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('.workspace', { timeout: 15000 })

  await page.getByRole('link', { name: '知识库' }).click()
  await page.waitForSelector('.folder', { timeout: 15000 })

  const folders = await page.locator('.folder h3').allInnerTexts()
  expect(
    '一级是目录列表(公司制度 / 产品资料 / 未分类)',
    ['公司制度', '产品资料', '未分类'].every((name) => folders.includes(name)),
    folders.join(' / '),
  )
  expect(
    '目录卡带份数(读数来自后端对账)',
    /份/.test(await page.locator('.folder footer').first().innerText()),
    (await page.locator('.folder footer').first().innerText()).replace(/\n/g, ' '),
  )
  await page.screenshot({ path: `${SHOT}_1_folders.png` })

  // ── 二级:公司制度里的文档表格 ────────────────────────────────────────
  await page.locator('.folder', { hasText: '公司制度' }).click()
  await page.waitForSelector('table tbody tr', { timeout: 15000 })

  const crumb = await page.locator('.crumbs').innerText()
  expect(
    '二级页有面包屑',
    crumb.includes('知识库') && crumb.includes('公司制度'),
    crumb.replace(/\n/g, ' '),
  )

  const rowOf = (name) => page.locator('tbody tr', { hasText: name })
  const rows = await page.locator('tbody tr').allInnerTexts()
  const rowText = (name) => rows.find((text) => text.includes(name)) || ''
  const expectations = [
    ['员工手册.md', '文件已变更', '登记 4096 字节、磁盘 5000 → 库里的内容是旧的'],
    ['没有入库成功的.pdf', '库内没有块', '登记说 processed,向量库里一块都没有'],
  ]
  for (const [name, badge, why] of expectations) {
    expect(
      `${name} → 「${badge}」`,
      rowText(name).includes(badge),
      `${why};行:${rowText(name).replace(/\n/g, ' ').slice(0, 70)}`,
    )
  }
  const mismatchRow = rowText('没有入库成功的.pdf')
  expect(
    '两个块数并排(库里实际 / 登记说的)',
    mismatchRow.includes('0') && mismatchRow.includes('12'),
    mismatchRow.replace(/\n/g, ' ').slice(0, 60),
  )
  const refreshed = await page.locator('.page-actions .faint').last().innerText()
  expect('页面上有「上次刷新」时间', /上次刷新\s*\d{1,2}:\d{2}:\d{2}/.test(refreshed), refreshed)
  await page.screenshot({ path: `${SHOT}_2_folder.png` })

  // ── 上传 → 轮询到「已入库」 ───────────────────────────────────────────
  await page.locator('input[type=file]').setInputFiles(UPLOAD)
  const row = rowOf('browser_upload_demo.md')
  await row.waitFor({ timeout: 15000 })
  const early = await row.innerText()
  // **只在"还没入库完成"这一点上断言**:入库是异步的(假后端故意慢 1.2 秒),所以此刻
  // 这一行必然是"在途"或"刚落盘还没登记"之一 —— 断言具体哪一个会与轮询抢跑。
  expect(
    '上传后这一行立刻出现,不必等入库完成',
    !early.includes('已入库'),
    early.replace(/\n/g, ' ').slice(0, 40),
  )
  await page.waitForFunction(
    () =>
      Array.from(document.querySelectorAll('tbody tr')).some(
        (node) =>
          node.textContent.includes('browser_upload_demo.md') &&
          node.textContent.includes('已入库'),
      ),
    undefined,
    { timeout: 20000 },
  )
  const done = await row.innerText()
  expect('轮询把它推到「已入库」', done.includes('已入库'), done.replace(/\n/g, ' ').slice(0, 60))
  await page.screenshot({ path: `${SHOT}_3_uploaded.png` })

  // ── 预览:两个数与页码 ────────────────────────────────────────────────
  await row.getByRole('button', { name: '预览' }).click()
  const dialog = page.locator('[role=dialog]')
  await dialog.waitFor({ timeout: 10000 })
  // **先等那一行出现**:弹层是先开、接口后回的;不等就断言会读到"还在加载"的中间态
  await dialog.locator('header p').waitFor({ timeout: 10000 })
  const head = await dialog.locator('header').innerText()
  expect(
    '预览显示子块与父块两个数',
    head.includes('子块') && head.includes('父块'),
    head.replace(/\n/g, ' '),
  )
  const blocks = await dialog.locator('.block').count()
  expect('预览有内容', blocks > 0, `${blocks} 段`)
  await page.screenshot({ path: `${SHOT}_4_preview.png` })
  await dialog.getByRole('button', { name: '关闭' }).click()
  await dialog.waitFor({ state: 'detached', timeout: 5000 })

  // 种子 PDF 在「产品资料」目录里：跨页页码要显示成「第 7–8 页」
  await backToFolders(page)
  await page.locator('.folder', { hasText: '产品资料' }).click()
  await page.waitForSelector('table tbody tr', { timeout: 15000 })
  await rowOf('劳动法').getByRole('button', { name: '预览' }).click()
  await dialog.waitFor({ timeout: 10000 })
  const spanTag = await dialog.locator('.block .tag.ok').first().innerText()
  expect('跨页显示成区间', spanTag.includes('–'), spanTag)
  await dialog.getByRole('button', { name: '关闭' }).click()
  await dialog.waitFor({ state: 'detached', timeout: 5000 })

  // 回到上传所在目录继续删；两步确认与提示语都在这一页上
  await backToFolders(page)
  await page.locator('.folder', { hasText: '公司制度' }).click()
  await page.waitForSelector('table tbody tr', { timeout: 15000 })

  // ── 删除:两步确认 + 如实转述 ─────────────────────────────────────────
  await row.getByRole('button', { name: '删除' }).click()
  const confirmButton = row.getByRole('button', { name: '确认删除?' })
  expect('删除要二次确认', (await confirmButton.count()) === 1, '出现「确认删除?」')
  await confirmButton.click()
  await page.waitForFunction(
    () =>
      !Array.from(document.querySelectorAll('tbody tr')).some((node) =>
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
  await page.screenshot({ path: `${SHOT}_5_deleted.png` })

  // ── 未分类:未登记的文件在那里,而且删不了 ───────────────────────────
  await backToFolders(page)
  await page.locator('.folder', { hasText: '未分类' }).click()
  await page.waitForSelector('table tbody tr', { timeout: 15000 })
  const unregistered = rowOf('未登记的语料.md')
  expect(
    '未登记的文件没有「删除」按钮(它没有登记行)',
    (await unregistered.getByRole('button', { name: '删除' }).count()) === 0,
    '只有只读的「预览」',
  )
  expect(
    '未登记的行给出可执行的下一步',
    (await unregistered.innerText()).includes('project-rag ingest'),
    (await unregistered.innerText()).replace(/\n/g, ' ').slice(0, 80),
  )
  await page.screenshot({ path: `${SHOT}_6_unfiled.png` })

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(
    failures === 0
      ? '\n知识库两级页面浏览器实测:全部通过'
      : `\n知识库两级页面浏览器实测:${failures} 条失败`,
  )
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

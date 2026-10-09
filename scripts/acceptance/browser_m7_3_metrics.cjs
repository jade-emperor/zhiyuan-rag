/**
 * M7.3 的浏览器实测：管理员可观测看板。
 *
 * 对着 `scripts/acceptance/serve_fake_backend.py`(真 uvicorn + 假账本,18081)跑:
 *
 *   1. 管理员进「可观测」→ 窗口按钮是 capabilities 报的那五个,默认 24h;
 *   2. KPI 卡片的数字与假账本对得上,而且**比率带分子分母**(拒答 2/24);
 *   3. 分段表:`total_ms` 的 p95 有值(n=24 ≥ 20),`rerank_ms` 的 p95 是 `—`(n=3 < 20)
 *      —— 而且那一格的 title 说明了门槛;`—` 绝不能被画成 0;
 *   4. 切到 `30d` → 条数变成 25、冷启动 1,最慢榜第一条挂「冷启动」徽章;
 *   5. 0 条 JS 报错 / 0 个失败请求。
 *
 * 跑法(准备见 docs/frontend.md §7)::
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_3_metrics.cjs
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
const EXE =
  process.env.CHROME_BIN ||
  path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT = process.env.SHOT_PREFIX || path.join(REPO_ROOT, '.cache/m7_metrics')

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
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })
  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`))
  const failedRequests = []
  page.on('requestfailed', (request) =>
    failedRequests.push(`${request.url()} ${request.failure() && request.failure().errorText}`),
  )

  async function card(label) {
    const node = page.locator('.metric', { hasText: label }).first()
    return { value: await node.locator('.value').innerText(), detail: await node.locator('.detail').innerText() }
  }

  // ── 登录 → 可观测 ──────────────────────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('text=Configuration', { timeout: 15000 })

  await page.getByRole('link', { name: '可观测' }).click()
  await page.waitForSelector('table', { timeout: 15000 })

  const windows = await page.locator('.windows button').allInnerTexts()
  expect(
    '窗口按钮来自 capabilities',
    windows.join(',') === '1h,6h,24h,7d,30d',
    JSON.stringify(windows),
  )

  // ── 24h:数字与分子分母 ────────────────────────────────────────────────
  const requests24 = await card('请求数')
  expect('请求数与假账本一致', requests24.value === '24', requests24.value)
  expect('冷热条数分开显示', requests24.detail.includes('冷启动 0') && requests24.detail.includes('热 24'), requests24.detail)

  const refused = await card('拒答率')
  expect('拒答率带分子分母', refused.detail.includes('2/24'), refused.detail)
  const truncated = await card('截断率')
  expect('截断率带分子分母', truncated.detail.includes('3/24'), truncated.detail)
  const aborted = await card('中断率')
  expect('中断率带分子分母', aborted.detail.includes('2/24'), aborted.detail)
  const faq = await card('FAQ 命中率')
  expect('FAQ 命中率带分子分母', faq.detail.includes('4/24'), faq.detail)
  await page.screenshot({ path: `${SHOT}_1_24h.png` })

  // ── 分段表:null 画成 —,且说明门槛 ──────────────────────────────────
  const totalRow = page.locator('tbody tr', { hasText: 'total_ms' }).first()
  const totalP95 = await totalRow.locator('td').nth(3).innerText()
  expect('n≥20 的分段给出 p95', /s$|ms$/.test(totalP95), `total_ms p95 = ${totalP95}`)

  const rerankRow = page.locator('tbody tr', { hasText: 'rerank_ms' }).first()
  const rerankN = await rerankRow.locator('td').nth(1).innerText()
  const rerankP95Cell = rerankRow.locator('td').nth(3)
  const rerankP95 = await rerankP95Cell.innerText()
  const rerankTitle = (await rerankP95Cell.getAttribute('title')) || ''
  expect('样本不够的分段显示 —（不是 0）', rerankP95 === '—', `n=${rerankN} → p95 = ${rerankP95}`)
  expect('那一格解释了门槛', rerankTitle.includes('样本不够') && /n≥20/.test(rerankTitle), rerankTitle)

  // ── 切窗口:30d 才看得见那条冷启动 ────────────────────────────────────
  await page.locator('.windows button', { hasText: '30d' }).click()
  await page.waitForFunction(
    () => {
      const node = Array.from(document.querySelectorAll('.metric')).find((el) =>
        el.textContent.includes('请求数'),
      )
      return node && node.querySelector('.value').textContent.trim() === '25'
    },
    undefined,
    { timeout: 15000 },
  )
  const requests30 = await card('请求数')
  expect('切窗口后条数跟着变', requests30.value === '25', requests30.value)
  expect('冷启动条数跟着变', requests30.detail.includes('冷启动 1'), requests30.detail)

  const slowHead = page.locator('section', { hasText: '最慢的几条' }).locator('tbody tr').first()
  const slowCells = await slowHead.locator('td').allInnerTexts()
  expect(
    '最慢榜第一条标了冷启动',
    slowCells.some((cell) => cell.includes('冷启动')) && slowCells.some((cell) => cell.includes('9.20 s')),
    slowCells.map((c) => c.replace(/\n/g, ' ')).join(' | '),
  )
  await page.screenshot({ path: `${SHOT}_2_30d.png` })

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  await browser.close()
  console.log(failures === 0 ? '\n可观测看板浏览器实测:全部通过' : `\n可观测看板浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

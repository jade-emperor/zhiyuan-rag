/**
 * 报表页的浏览器实测：（延迟 + 消耗）。运维与管理员可见。
 *
 * 对着 `scripts/acceptance/serve_fake_backend.py`(真 uvicorn + 假账本,18081)跑:
 *
 *   1. 管理员进「报表」→ 窗口按钮是 capabilities 报的那五个,默认 24h;
 *   2. KPI 卡片的数字与假账本对得上,而且**比率带分子分母**(拒答 2/24);
 *   3. 分段表:`total_ms` 的 p95 有值(n=24 ≥ 20),`rerank_ms` 的 p95 是 `—`(n=3 < 20)
 *      —— 而且那一格的 title 说明了门槛;`—` 绝不能被画成 0;
 *   4. 切到 `30d` → 条数变成 25、冷启动 1,最慢榜第一条挂「冷启动」徽章;
 *   5. **直接落在 `#/reports`**(在这一页按 F5 / 书签 / 上次关在这一页)也自己把能力读回来
 *      —— 不经过「对话」页,不能停在「还没读到 capabilities」那句红字上;
 *   6. **能力那一枪打空了**(后端当时不在)点「刷新」要能自救:先如实说"连不上",
 *      后端回来说话就恢复,而不是拿着一个空的窗口名一直说"还没读到 capabilities";
 *   7. 0 条 JS 报错 / 0 个失败请求。
 *
 * 跑法(准备)：
 *
 *   RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *   LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *     node scripts/acceptance/browser_m7_3_metrics.cjs
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

  // ── 登录 → 报表 ──────────────────────────────────────────────────────
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.getByLabel('用户名').fill('admin')
  await page.getByLabel('密码').fill('admin-pass')
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForSelector('.workspace', { timeout: 15000 })

  await page.getByRole('link', { name: '报表' }).click()
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

  // 这一窗里有 24 条热 + 1 条冷启动,而那条冷启动**也是拒答**:拒答率的分母必须仍是
  // 热行的 24(2/24),不是 25 —— 冷启动里用户等的是模型加载,混进来这一版的成绩
  // 就会随"这周重启过几次"漂。
  const refused30 = await card('拒答率')
  expect(
    '冷启动那次拒答没被算进拒答率(分母仍是热行 24)',
    refused30.detail.includes('2/24'),
    refused30.detail,
  )
  const coldNote = await page.locator('.cold-note').innerText()
  expect(
    '冷启动自己那一套数单独报出来',
    coldNote.includes('只数热行') && /拒答\s*1/.test(coldNote),
    coldNote.replace(/\n/g, ' '),
  )

  const slowHead = page.locator('section', { hasText: '最慢的几条' }).locator('tbody tr').first()
  const slowCells = await slowHead.locator('td').allInnerTexts()
  expect(
    '最慢榜第一条标了冷启动',
    slowCells.some((cell) => cell.includes('冷启动')) && slowCells.some((cell) => cell.includes('9.20 s')),
    slowCells.map((c) => c.replace(/\n/g, ' ')).join(' | '),
  )
  await page.screenshot({ path: `${SHOT}_2_30d.png` })

  // ── 消耗区:三档 × 两时段,金额真的算出来了 ──────────────────────
  const costSection = page.locator('section', { hasText: '消耗' }).first()
  const costText = (await costSection.innerText()).replace(/\n/g, ' ')
  // **此刻窗口是 30d**(上面刚切过):假账本在 30d 里是 25 行,其中 24 条热 + 1 条冷启动,
  // 冷启动那条也是 rag 行、也带 token —— 所以这里数到 25,不是 24。
  expect(
    '消耗区给出模型调用次数(跟着窗口走)',
    /模型调用\s*25\s*次/.test(costText),
    costText.slice(0, 120),
  )
  expect(
    '输入 token 那一格说明了其中命中缓存多少(单价差 50 倍)',
    /输入 token/.test(costText) && /其中命中缓存\s*[1-9]/.test(costText),
    costText.slice(0, 200),
  )
  // 时段名**不写死哪一个**:假账本的 25 行按"行龄"分布在最近 10 小时 + 一条 30 小时前,
  // 它们落高峰还是空闲取决于你**几点**跑这条用例 —— 断言该盯"时段列在、金额非零",
  // 而不是盯某一个具体时段(那样它在半夜跑就会假红)。
  expect(
    '按模型 × 时段列出金额(真金白银,不是 0)',
    costText.includes('deepseek-flash') &&
      /(高峰时段|空闲时段)/.test(costText) &&
      /CNY\s*0\.0[0-9]+/.test(costText),
    costText.slice(0, 240),
  )
  expect(
    '假后端配了单价 → 不出现「未配置单价」',
    !costText.includes('未配置单价'),
    costText.slice(0, 200),
  )

  expect('没有 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || '无')
  expect('没有失败请求', failedRequests.length === 0, failedRequests.slice(0, 2).join(' | ') || '无')

  // ── 直接落在 #/reports:在报表页按 F5 也要能用 ──────────────────────────
  // 这条钉的是用户报过的死局:能力自述以前只由「登录页」与「对话页」挂载时拉一次,
  // 于是"上次关在这一页 / 书签 / 手输 #/reports"直接进来时,看板读到一个空 store,
  // 只回一句「还没读到 capabilities,不知道该请求哪个窗口」—— 而「刷新」读的还是
  // 同一个空 store,救不回来。修法在路由守卫(业务页入场前补拉),这条就是它的反面。
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('h1:has-text("报表")', { timeout: 15000 })
  let buttonsAfterReload = []
  try {
    // 坏版本里窗口按钮永远不会出现(store 是空的)→ 等超时,下面那条断言变红
    await page.waitForSelector('.windows button', { timeout: 5000 })
  } catch {
    /* 交给断言去说 */
  }
  buttonsAfterReload = await page.locator('.windows button').allInnerTexts()
  const boxAfterReload = (await page.locator('.alert.error').allInnerTexts()).join(' ')
  expect(
    '直接落在 #/reports 也自己补拉能力(不经过对话页)',
    buttonsAfterReload.join(',') === '1h,6h,24h,7d,30d' && !boxAfterReload.includes('还没读到'),
    `窗口 ${JSON.stringify(buttonsAfterReload)};红字 ${JSON.stringify(boxAfterReload.slice(0, 60))}`,
  )

  // ── 能力那一枪打空了(后端当时不在)→「刷新」必须能自救 ──────────────────
  // 把 capabilities 连掐两次(守卫那次 + loadMetrics 里那次补拉),逼出"读不到能力"的状态;
  // 之后放行,点一次「刷新」就该恢复。**注意这条故意制造失败请求**,所以放在
  // 「没有失败请求」那两条断言之后。
  let aborts = 0
  await page.route('**/api/v1/capabilities', (route) => {
    if (aborts < 2) {
      aborts += 1
      return route.abort()
    }
    return route.continue()
  })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('h1:has-text("报表")', { timeout: 15000 })
  await page.waitForSelector('.alert.error', { timeout: 15000 })
  const boxWhenDown = (await page.locator('.alert.error').allInnerTexts()).join(' ')
  expect(
    '能力打空时如实说「连不上」,不拿「还没读到 capabilities」盖住',
    boxWhenDown.includes('连不上后端'),
    boxWhenDown.slice(0, 80),
  )
  await page.getByRole('button', { name: '刷新' }).click()
  let healed = []
  try {
    await page.waitForSelector('.windows button', { timeout: 15000 })
    healed = await page.locator('.windows button').allInnerTexts()
  } catch {
    /* 交给断言去说 */
  }
  expect(
    '点「刷新」能把能力补回来(后端恢复后不是死局)',
    healed.join(',') === '1h,6h,24h,7d,30d',
    JSON.stringify(healed),
  )
  await page.screenshot({ path: `${SHOT}_3_selfheal.png` })
  await page.unroute('**/api/v1/capabilities')

  await browser.close()
  console.log(failures === 0 ? '\n报表看板浏览器实测:全部通过' : `\n报表看板浏览器实测:${failures} 条失败`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

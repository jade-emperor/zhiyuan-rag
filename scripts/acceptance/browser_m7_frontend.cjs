/**
 * 聊天工作台（Vue 3 + TS + Vite 三栏 UI）的浏览器验收。
 *
 * 这是「人工那一步」的脚本化版本：对着**假后端**（真 uvicorn + 假依赖，18081）跑一遍
 * 真实用户流程，每条断言都同时看**渲染结果**与**交互行为**，不看像素（容器里没有中文字体，
 * 截图里中文是方框 —— 那是环境，不是界面坏了）。
 *
 * ── 前置条件 ─────────────────────────────────────────────────────────
 * 1. 假后端在跑，且它服务的是**最新构建产物**（改了 `web/src` 要先 `cd web && npm run build`）：
 *
 *        RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
 *
 *    账号：admin/admin-pass、employee/emp-pass、ops/ops-pass（本脚本用前两个）。
 *    假数据要点：管理员 2 条会话（第一条 2 轮、带引用快照）；FAQ/向量检索可答；
 *    **关键字索引是空的** —— 切到 keyword 必然拒答，低置信度那条就靠它。
 * 2. 一次性准备（都放 `.cache/`，不进版本库；换机器时用环境变量覆盖路径）：
 *
 *        mkdir -p .cache/pw && cd .cache/pw && npm i playwright --cache ../.cache/npm-cache
 *        # Chromium（国内走 npmmirror；cdn.playwright.dev 会转到 Google Storage，很慢）
 *        #   .cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell
 *        # 缺 libnss3 / libasound2 而没 root：下 .deb 解到 .cache/pwlibs/root，用 LD_LIBRARY_PATH 指过去
 *
 * ── 跑法 ─────────────────────────────────────────────────────────────
 *
 *     cd /home/user1/projects/project_rag
 *     LD_LIBRARY_PATH=$PWD/.cache/pwlibs/root/usr/lib/x86_64-linux-gnu \
 *       node scripts/acceptance/browser_m7_frontend.cjs
 *
 *     # 可选环境变量：
 *     #   BASE=http://127.0.0.1:8080/   打真容器（账号要真实存在；手动清单见 docs/UI_ACCEPTANCE.md，该目录不随仓库发布）
 *     #   PLAYWRIGHT_MODULE=...         换 playwright 包位置
 *     #   CHROME_BIN=...                换 Chromium
 *     #   UI_SHOT_DIR=...               截图目录（默认 .cache/ui-shots）
 *
 * ── 覆盖点（每条失败都会打印可执行的排查方向）────────────────────────────
 *  1. 1440×900：`/` 307 到 `/static/app/`、登录页可达、admin 登录；
 *  2. 三栏：`.sidebar` 260 / `.panel` 320 / 1440 无横向滚动；
 *  3. 会话：左侧 2 条、点第一条回填 4 条消息（2 轮）、行内 `.cite-ref[data-cite]` 2 个、
 *     hover 浮层 `.cite-tip` 带来源/页码/摘要；
 *  4. 引用：点行内引用 → 右栏 `.panel` 自动加载 `.block`；「查看原文」可切换；
 *     `页码未知` 如实显示；历史首答 rerank=0.62 应出 `.confidence-medium`；
 *  5. 新对话：流式答案落地、引用 2 条、操作栏四个动作、**生成中不可重新生成**、
 *     旧轮「重新生成」禁用并说明原因；
 *  6. 输入框：120 次重复的超长文本高度 ≤200；
 *  7. 低置信度：keyword 模式拒答 → `.low-confidence` 逐字文案 + 两个动作 +
 *     「换个问法」把焦点放回 `textarea[aria-label=输入问题]`；
 *  8. 暗色：顶栏主题按钮（title「切换到深色主题」）→ `data-theme=dark` → 再切回 light；
 *  9. 375×780：无横向滚动、侧栏抽屉（打开/Escape 关闭）、引用底部弹层、输入区仍可用；
 * 10. 生成中「停止」→「已停止本次生成」提示，且不残留生成中状态；
 * 11. 全程：console error / pageerror / requestfailed 为空，且**没有 alert/confirm 弹窗**。
 *
 * ── 背景：流式是 WebSocket，不是 SSE ─────────────────────────────────
 * `GET /api/v1/stream` 是 **WebSocket** 路由：令牌只能走 query（浏览器不能给 WS 设头）、
 * 服务端发 `sources` / `token` / `done` / `error` 四种 JSON 帧、认证失败先回 error 再 close(1008)。
 * 所以这里不能用 `fetch` + `EventSource` 去断言流式；页面自己必须连 WS，本脚本用 DOM 结果
 * （`.md` 渐进出字、`.citation` 落地、操作栏出现）来判断「流式真的走完了」。
 * 后端这一层是冻结的，改成 SSE 不在本次范围内。
 *
 * 所有截图写到 `.cache/ui-shots/browser-*.png`（不进版本库）。
 * 断言失败不会中断后续场景；最后汇总通过/失败/未验证并 `process.exit(1)`。
 *
 * 本脚本**只测不改**：发现 app bug 会红并把文件:行号写进失败详情，不修源码。
 */

const fs = require('fs')
const path = require('path')

// 仓库根：从本文件位置推出来（scripts/acceptance/ → 上两级），换机器不用改源码。
const REPO_ROOT = path.resolve(__dirname, '../..')

const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE
  || path.join(REPO_ROOT, '.cache/pw/node_modules/playwright'),
)

const BASE = process.env.BASE || 'http://127.0.0.1:18081/'
const EXE = process.env.CHROME_BIN
  || path.join(REPO_ROOT, '.cache/chrome/chrome-headless-shell-linux64/chrome-headless-shell')
const SHOT_DIR = process.env.UI_SHOT_DIR || path.join(REPO_ROOT, '.cache/ui-shots')

let checks = 0
let failures = 0
let unverified = 0
const failed = []
const unverifiedNames = []
const appBugs = []

function ok(name, detail) {
  console.log(`  ✔ ${name} — ${detail}`)
}

function bad(name, detail, hint) {
  failures += 1
  failed.push(name)
  console.log(`  ✘ ${name} — ${detail}${hint ? `；${hint}` : ''}`)
}

function markUnverified(name, detail) {
  checks += 1
  unverified += 1
  unverifiedNames.push(`${name}（${detail}）`)
  console.log(`  ○ ${name} — 未验证：${detail}`)
}

/**
 * 输入:名称 / 条件 / 观测到的事实 / 失败时给的方向。
 * 行为:通过时只打印观测事实，失败时再补上「该查哪里」。
 * 输出:无（累计计数）。
 */
function expect(name, condition, detail, hint) {
  checks += 1
  if (condition) ok(name, detail)
  else bad(name, detail, hint)
}

/** 一个场景抛异常（例如等不到登录页）时，记一条失败但**继续**跑后面的场景。 */
async function scenario(title, body) {
  console.log(`\n${title}`)
  try {
    await body()
  } catch (error) {
    checks += 1
    const message = String(error && error.message ? error.message : error).split('\n')[0]
    bad(`[场景中断] ${title}`, message, '后续场景仍会继续跑，最后汇总')
  }
}

/** 输入:选择器。行为:等它出现/消失。输出:是否等到（不抛）。 */
async function waitFor(page, selector, timeout = 10000, state = 'visible') {
  try {
    await page.waitForSelector(selector, { timeout, state })
    return true
  } catch {
    return false
  }
}

async function count(page, selector) {
  try {
    return await page.locator(selector).count()
  } catch {
    return 0
  }
}

async function text(page, selector) {
  try {
    return (await page.locator(selector).first().innerText()).trim()
  } catch {
    return ''
  }
}

async function allText(page, selector) {
  try {
    return (await page.locator(selector).allInnerTexts()).map((item) => item.trim())
  } catch {
    return []
  }
}

async function shot(page, name) {
  try {
    await page.screenshot({ path: path.join(SHOT_DIR, name) })
  } catch (error) {
    console.log(`  （截图 ${name} 失败：${error.message}）`)
  }
}

async function main() {
  fs.mkdirSync(SHOT_DIR, { recursive: true })
  const browser = await chromium.launch({ executablePath: EXE, args: ['--no-sandbox'] })
  // colorScheme 固定成 light：主题默认是「跟随系统」，固定之后「切到深色」这条才可复现。
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    colorScheme: 'light',
  })
  const page = await context.newPage()

  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  const failedRequests = []
  page.on('requestfailed', (request) =>
    failedRequests.push(
      `${request.url()} ${request.failure() ? request.failure().errorText : '(原因未知)'}`,
    ),
  )
  // 新交互禁止用 alert/confirm：出现即记录（并 dismiss，避免脚本挂住）。
  const dialogs = []
  page.on('dialog', async (dialog) => {
    dialogs.push(`${dialog.type()}: ${dialog.message()}`)
    try {
      await dialog.dismiss()
    } catch {
      /* 已经关掉了 */
    }
  })

  // ── 1. 入口、登录页、admin 登录 ─────────────────────────────────────
  await scenario('[1] 1440×900：/ 307 → /static/app/ → 登录页 → admin', async () => {
    const root = await page.request.get(BASE, { maxRedirects: 0 })
    const location = root.headers()['location'] || ''
    expect(
      '根路径 307 到前端 app',
      root.status() === 307 && /\/static\/app\/?$/.test(location),
      `status=${root.status()} location=${location || '(无)'}`,
      '期望 307 + Location /static/app/；查后端 GET /（api/app.py）',
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const loginReady = await waitFor(page, '.login input[autocomplete="username"]', 15000)
    expect(
      '登录页可达',
      loginReady && /\/static\/app\/#\/login/.test(page.url()),
      `url=${page.url()}`,
      '应落在 /static/app/#/login；检查 307 落点与 hash 路由守卫',
    )
    const title = await page.title()
    expect('登录页标题是「登录」', title.includes('登录'), `title=「${title}」`)
    const backendText = await text(page, '.login .backend button')
    expect(
      '登录页探到后端（已连通）',
      backendText.includes('已连通'),
      `后端按钮=「${backendText}」`,
      '应含「已连通」；查 GET /api/v1/capabilities 与 LoginView 的 backendOk',
    )

    await page.getByLabel('用户名').fill('admin')
    await page.getByLabel('密码').fill('admin-pass')
    await page.getByRole('button', { name: '登录' }).click()
    const entered = await waitFor(page, '.workspace', 15000)
    await waitFor(page, '.history .item', 10000)
    expect(
      'admin 登录进入对话工作台',
      entered && page.url().includes('#/chat') && (await count(page, '.sidebar')) === 1,
      `url=${page.url()}，.sidebar=${await count(page, '.sidebar')}`,
      '登录后应落在 #/chat 并渲染 .sidebar；查 auth.login / 路由守卫',
    )
    const tabs = await allText(page, '.header .tab')
    expect(
      '管理员看到四个页签',
      tabs.length === 4 && ['对话', '知识库', '报表', '账号与权限'].every((tab) => tabs.includes(tab)),
      `页签 ${JSON.stringify(tabs)}`,
      '页签按 /auth/me 的能力布尔值渲染（AppHeader）',
    )
  })

  // ── 2. 三栏布局 ───────────────────────────────────────────────────
  await scenario('[2] 三栏：260 / 自适应 / 320，1440 无横向滚动', async () => {
    const sidebar = await page.locator('.sidebar').boundingBox()
    expect(
      '左侧栏宽 260',
      sidebar !== null && Math.round(sidebar.width) === 260,
      sidebar === null ? '.sidebar 没有盒子' : `${sidebar.width}px`,
      '期望 260；查 ChatView 的 .sidebar-pane 与 tokens.css 的 --sidebar',
    )
    const messageArea = await count(page, '.messages[role="log"]')
    expect(
      '消息区存在',
      messageArea === 1,
      `.messages[role=log]=${messageArea}`,
      '查 MessageList.vue 根节点（role=log）',
    )

    await page.getByRole('button', { name: '引用原文面板' }).click()
    const panelOpen = await waitFor(page, '.panel', 5000)
    const panel = panelOpen ? await page.locator('.panel').boundingBox() : null
    expect(
      '打开引用面板后宽 320',
      panel !== null && Math.round(panel.width) === 320,
      panel === null ? '.panel 没打开' : `${panel.width}px`,
      '期望 320；查 ChatTopBar 的 togglePanel / ui.panelOpen 与 --panel',
    )
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(
      '1440 无横向滚动',
      overflow <= 0,
      `scrollWidth-innerWidth=${overflow}px`,
      '三栏合计超过 1440 时查 --sidebar/--panel 与 .main 的 min-width',
    )
    await shot(page, 'browser-01-1440.png')
    await page.getByRole('button', { name: '关闭引用面板' }).click()
    await waitFor(page, '.panel', 3000, 'detached')
  })

  // ── 3. 会话列表 + 历史回填 + 行内引用浮层 ───────────────────────────
  await scenario('[3] 会话：2 条 / 回填 2 轮 / 行内引用 2 个 / hover 浮层', async () => {
    const sessions = await count(page, '.history .item')
    expect(
      '左侧 2 条会话',
      sessions === 2,
      `${sessions} 条`,
      '数据来自 GET /api/v1/sessions（假后端 SESSIONS）；查 SessionList 的 groupedSessions',
    )

    await page.locator('.history .item-main').first().click()
    const loaded = await waitFor(page, '.msg', 10000)
    const total = await count(page, '.msg')
    expect(
      '点第一条 → 4 条消息（2 轮）',
      loaded && total === 4,
      `${total} 条（user=${await count(page, '.msg.user')} / assistant=${await count(page, '.msg.assistant')}）`,
      '会话 1111… 的 GET /history 应有 2 轮；chat.ts openSession 每轮 push 两条',
    )
    expect(
      '2 条提问 + 2 条回答',
      (await count(page, '.msg.user')) === 2 && (await count(page, '.msg.assistant')) === 2,
      `user=${await count(page, '.msg.user')} assistant=${await count(page, '.msg.assistant')}`,
    )
    const inline = await count(page, '.msg.assistant .cite-ref[data-cite]')
    expect(
      '行内引用 .cite-ref[data-cite] 2 个',
      inline === 2,
      `${inline} 个`,
      '答案里的 [1][2] 由 utils/markdown.ts 转成带 data-cite 的按钮',
    )

    await page.locator('.cite-ref[data-cite="1"]').first().hover()
    const tipShown = await waitFor(page, '.cite-tip', 3000)
    const tipFile = tipShown ? await text(page, '.cite-tip .tip-file') : ''
    const tipMeta = tipShown ? await text(page, '.cite-tip .tip-meta') : ''
    const tipPreview = tipShown ? await text(page, '.cite-tip .tip-preview') : ''
    expect(
      'hover 行内引用出现 .cite-tip 浮层',
      tipShown,
      tipShown ? '已出现' : '未出现',
      '查 MessageList 的 showTip / mouseover 与 .cite-tip',
    )
    expect(
      '浮层里有来源与页码',
      tipFile.includes('劳动法') && tipMeta.includes('bagu_ai') && tipMeta.includes('第 7 页'),
      `文件名=「${tipFile}」，来源/页码=「${tipMeta.replace(/\n/g, ' ')}」`,
      '来源取 citation.source、页码取 pageLabel(citation)',
    )
    expect(
      '浮层里有摘要',
      tipPreview.length >= 10,
      `摘要=${tipPreview.length} 字：「${tipPreview.slice(0, 40)}」`,
      '来自 citation.preview（shorten 130 字）',
    )
    await page.mouse.move(2, 2)
    await shot(page, 'browser-02-1440-history.png')

    const regens = page.locator('.msg.assistant [aria-label="重新生成"]')
    const regenCount = await regens.count()
    const firstDisabled = regenCount >= 1 ? await regens.first().isDisabled() : false
    const firstTitle = regenCount >= 1 ? (await regens.first().getAttribute('title')) || '' : ''
    expect(
      '旧轮「重新生成」禁用并说明原因',
      regenCount === 2 && firstDisabled && firstTitle.includes('最后一轮'),
      `共 ${regenCount} 个，第一个 disabled=${firstDisabled}，title=「${firstTitle}」`,
      '会话是追加式的，只允许重跑最后一轮（MessageList.canRegenerate）',
    )
  })

  // ── 4. 引用面板：原文块 / 页码未知 / rerank 置信档 ─────────────────
  await scenario('[4] 引用：右栏原文块 / 页码未知 / rerank=0.62 的置信档', async () => {
    await page.locator('.cite-ref[data-cite="1"]').first().click()
    const panelOpen = await waitFor(page, '.panel', 5000)
    const blocksLoaded = await waitFor(page, '.panel .block', 8000)
    expect(
      '点行内引用 → 右栏打开并自动加载原文块',
      panelOpen && blocksLoaded,
      `panel=${panelOpen} block=${blocksLoaded}`,
      '查 GET /api/v1/documents/{id}/chunks 与 CitationPanel 的 watch(immediate)',
    )
    const panelMeta = await text(page, '.panel .meta-row')
    expect(
      '面板显示 PDF 页码',
      panelMeta.includes('第 7 页'),
      panelMeta.replace(/\n/g, ' '),
      'citation.page=7 → 「第 7 页」',
    )
    const facts = await text(page, '.panel .facts')
    expect(
      '面板显示重排分 0.620（数据侧确实是 rerank）',
      facts.includes('重排分') && facts.includes('0.620'),
      facts.replace(/\n/g, ' | ') || '没有 .panel .facts',
      '查 CitationPanel 的 facts 与假数据 Citation(rerank_score=0.62)',
    )
    await shot(page, 'browser-03-1440-citation.png')

    const tags = await allText(page, '.citation .line .tag')
    expect(
      '引用卡片 2 条：第 7 页 + 页码未知',
      tags.filter((tag) => tag === '第 7 页').length === 1
        && tags.filter((tag) => tag === '页码未知').length === 1,
      JSON.stringify(tags),
      'page=null 必须写「页码未知」，不许拿 0/1 顶（utils/citations.pageLabel）',
    )

    const unknownCard = page.locator('.citation', { hasText: '页码未知' }).first()
    const unknownCardCount = await unknownCard.count()
    expect(
      'md 引用卡片上就看得到「页码未知」',
      unknownCardCount === 1,
      `匹配 ${unknownCardCount} 条`,
      'md 引用（员工手册.md）的 page=null；查 CitationItem 的 page computed',
    )
    // 引用行是 role="button"（整行可点），「查看原文」是行内的 .open 提示；
    // 点 .open 而不是整行，确认这个入口本身也能打开面板。
    await unknownCard.locator('.open').click()
    const unknownInPanel = await waitFor(page, '.panel .meta-row .tag:has-text("页码未知")', 5000)
    expect(
      '点「查看原文」后右栏仍是「页码未知」',
      unknownInPanel,
      `面板 meta=「${(await text(page, '.panel .meta-row')).replace(/\n/g, ' ')}」`,
      'CitationPanel 的 meta-row 用 pageLabel(citation)',
    )
    const mdBlocks = await waitFor(page, '.panel .block', 8000)
    expect(
      'md 引用（页码未知）也加载出原文块',
      mdBlocks,
      `block=${await count(page, '.panel .block')}`,
      '查 /documents/{id}/chunks 与 block.page',
    )

    const medium = await count(page, '.msg.assistant .confidence-medium')
    if (medium !== 1) {
      appBugs.push(
        '历史首答 rerank=0.62 不出 .confidence-medium：'
        + 'web/src/stores/chat.ts:299-313 的 openSession 用 blankMessage 重建消息（channel=null、request=null），'
        + 'web/src/utils/confidence.ts:108 对 channel !== "rag" 直接返回 unrated —— 引用上的 rerank_score=0.62 被丢掉',
      )
    }
    expect(
      '历史首答 rerank=0.62 → .confidence-medium（AI 建议）',
      medium === 1,
      `出现 ${medium} 个 .confidence-medium`,
      '面板已显示「重排分 0.620」，但历史消息没有 channel → assessConfidence 判 unrated。'
        + 'APP BUG：web/src/stores/chat.ts:305（openSession 不带 channel）'
        + ' + web/src/utils/confidence.ts:108（channel !== "rag" 直接 unrated）',
    )
  })

  // ── 5. 新对话：流式 / 引用 / 操作栏 / 生成中状态 ────────────────────
  await scenario('[5] 新对话：流式答案 / 引用 2 条 / 四个动作 / 生成中不可重新生成', async () => {
    await page.getByRole('button', { name: '新建对话' }).click()
    const welcome = await waitFor(page, '.welcome', 5000)
    expect(
      '新建对话清空消息区与引用面板',
      welcome && (await count(page, '.msg')) === 0 && (await count(page, '.panel')) === 0,
      `welcome=${welcome} messages=${await count(page, '.msg')} panel=${await count(page, '.panel')}`,
      'newChat() 应 closePanel 并清空 messages',
    )

    // 生成中状态只持续几十毫秒（假后端流式），必须在**页面内**连续采样，避免每次
    // locator 往返的竞态：记录「生成中是否出现过操作栏 / 可点的重新生成」。
    await page.evaluate(() => {
      const record = {
        samples: 0,
        streamingSamples: 0,
        waitingSamples: 0,
        waitingTexts: [],
        actionsWhileStreaming: 0,
        enabledRegenWhileStreaming: 0,
        stopSeen: 0,
        done: false,
      }
      window.__dsGenRecord = record
      const tick = () => {
        const list = document.querySelectorAll('.msg.assistant')
        const last = list[list.length - 1]
        if (last !== undefined) {
          record.samples += 1
          const waiting = last.querySelector('.waiting')
          if (waiting !== null) {
            record.waitingSamples += 1
            const waitingText = (waiting.innerText || '').trim().split('\n')[0]
            if (waitingText.length > 0 && !record.waitingTexts.includes(waitingText)) {
              record.waitingTexts.push(waitingText)
            }
          }
          if (last.querySelector('.streaming-actions') !== null) {
            record.streamingSamples += 1
            if (last.querySelector('.actions') !== null) record.actionsWhileStreaming += 1
            if (last.querySelector('.actions [aria-label="重新生成"]:not([disabled])') !== null) {
              record.enabledRegenWhileStreaming += 1
            }
            if (document.querySelector('.composer .stop') !== null) record.stopSeen += 1
          } else if (last.querySelector('.actions') !== null) {
            record.done = true
            return
          }
        }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })

    await page.getByLabel('输入问题').fill('加班上限是多少？')
    await page.getByRole('button', { name: '发送' }).click()
    const finished = await waitFor(page, '.msg.assistant .actions', 30000)
    // 给页面内采样器一帧：即使它在生成结束后才第一次采样，也能把 done/采样数记下来。
    // 「生成是否结束」只认 DOM（finished），不认采样器 —— 采样器只负责瞬态观察。
    await page.waitForTimeout(150)
    const record = await page.evaluate(() => window.__dsGenRecord)

    // 生成中只持续几十毫秒：**能采到才断言，采不到记「未验证」**（不把时序当失败）。
    if (record.waitingSamples === 0) {
      markUnverified(
        '首 token 前有等待态（.waiting）',
        `采样 ${record.samples} 帧，没抓到 .waiting（假后端流式太快）；首 token 感知请按 docs/UI_ACCEPTANCE.md（该目录不随仓库发布） 用真后端看`,
      )
    } else {
      expect(
        '首 token 前有等待态（.waiting）',
        record.waitingTexts.some(
          (item) => item.includes('正在检索资料') || item.includes('已找到'),
        ),
        `等待态 ${record.waitingSamples} 帧：${JSON.stringify(record.waitingTexts)}`,
        'MessageList 的 .waiting 在第一个 token 前应显示「正在检索资料…」或「已找到 N 条依据，正在生成…」',
      )
    }
    if (record.streamingSamples === 0) {
      markUnverified(
        '生成中状态出现过（.streaming-actions）',
        `采样 ${record.samples} 帧，没抓到生成中状态（假后端流式太快）`,
      )
      markUnverified(
        '生成中操作栏未出现（重新生成点不到）',
        `采样 ${record.samples} 帧，没抓到生成中状态`,
      )
      markUnverified(
        '生成中没有可用的「重新生成」',
        `采样 ${record.samples} 帧，没抓到生成中状态`,
      )
      markUnverified(
        '生成中发送键换成「停止」',
        `采样 ${record.samples} 帧，没抓到生成中状态`,
      )
    } else {
      expect(
        '生成中状态出现过（.streaming-actions）',
        record.streamingSamples > 0,
        `采样 ${record.samples} 帧、生成中 ${record.streamingSamples} 帧`,
        '生成中应有 .streaming-actions 与输入区的「停止」',
      )
      expect(
        '生成中操作栏未出现（重新生成点不到）',
        record.actionsWhileStreaming === 0,
        `生成中 ${record.streamingSamples} 帧，操作栏出现 ${record.actionsWhileStreaming} 帧`,
        'MessageList 的 footer 是 v-if="!message.streaming"',
      )
      expect(
        '生成中没有可用的「重新生成」',
        record.enabledRegenWhileStreaming === 0,
        `生成中 ${record.enabledRegenWhileStreaming} 帧出现可点的重新生成`,
        'canRegenerate 必须排除 chat.generating / message.streaming',
      )
      expect(
        '生成中发送键换成「停止」',
        record.stopSeen > 0,
        `生成中 ${record.stopSeen} 帧看到 .composer .stop`,
        'Composer 的「停止」由 v-if="chat.generating" 控制',
      )
    }
    expect(
      '生成结束（操作栏出现）',
      finished,
      `finished=${finished}，采样 ${record.samples} 帧（生成中 ${record.streamingSamples} 帧，done=${record.done}）`,
      '30s 内没等到 .msg.assistant .actions；查 WS done 帧与 chat.generating',
    )

    const answers = await allText(page, '.msg.assistant .md')
    const answer = answers.length > 0 ? answers[answers.length - 1] : ''
    expect(
      '流式答案落地到 .md',
      answer.includes('每日不超过八小时'),
      `答案=「${answer.slice(0, 60)}」`,
      '假后端 FakeLLM 的正文应逐字进 .md',
    )
    const lastAssistant = page.locator('.msg.assistant').last()
    const citations = await lastAssistant.locator('.citation').count()
    expect(
      '本轮引用卡片 2 条',
      citations === 2,
      `${citations} 条`,
      '假后端 max_context=2；引用从 WS done 帧 / sources 帧落地',
    )

    const actionLabels = await lastAssistant
      .locator('.actions button')
      .evaluateAll((buttons) =>
        buttons.map((button) => button.getAttribute('aria-label') || button.getAttribute('title') || ''),
      )
      .catch(() => [])
    expect(
      '操作栏有复制 / 重新生成 / 点赞 / 点踩',
      ['复制回答', '重新生成', '有帮助', '没帮助'].every((label) => actionLabels.includes(label)),
      JSON.stringify(actionLabels),
      '看 MessageList 的 footer.actions',
    )
    const lastRegen = lastAssistant.locator('.actions [aria-label="重新生成"]')
    const regenDisabled = (await lastRegen.count()) !== 1 || (await lastRegen.isDisabled())
    expect(
      '生成结束后最后一轮「重新生成」可用',
      !regenDisabled,
      `重新生成 disabled=${regenDisabled}`,
      'canRegenerate 只允许最后一轮，且生成中禁用',
    )
    await shot(page, 'browser-04-1440-answer.png')
  })

  // ── 6. 输入框高度上限 ─────────────────────────────────────────────
  await scenario('[6] 输入框：120 次重复的超长文本，高度 ≤200', async () => {
    const box = page.getByLabel('输入问题')
    await box.fill('测试输入高度。'.repeat(120))
    await page.waitForTimeout(250)
    const height = await box.evaluate((element) => element.getBoundingClientRect().height)
    expect(
      '超长输入被夹到 200px 以内',
      height > 60 && height <= 200,
      `textarea 高 ${Math.round(height)}px`,
      '>60 说明确实长高了，≤200 是 Composer.autoGrow 的上限 / CSS max-height',
    )
    await box.fill('')
    await page.waitForTimeout(100)
  })

  // ── 7. 低置信度（keyword 索引为空 → 拒答）────────────────────────
  await scenario('[7] 低置信度：keyword 拒答 → 文案 / 两个动作 / 换个问法聚焦', async () => {
    await page.getByRole('button', { name: '检索设置' }).click()
    const settingsOpen = await waitFor(page, '.settings-overlay.open', 5000)
    const dialogCount = await count(page, '[role="dialog"][aria-label="检索设置"]')
    expect(
      '检索设置抽屉打开',
      settingsOpen && dialogCount === 1,
      `overlay.open=${settingsOpen} role=dialog=${dialogCount}`,
      '查 ChatTopBar 的 openSettings 与 ChatView 的 settings-overlay',
    )
    await page.getByLabel('检索方式').selectOption('keyword')
    await page.waitForTimeout(200)
    const modeValue = await page.getByLabel('检索方式').inputValue().catch(() => '(读不到)')
    expect(
      '检索方式切到 keyword',
      modeValue === 'keyword',
      `当前值=${modeValue}`,
      'select 的 value 应为 keyword（选项文案「关键字检索(BM25)」）',
    )
    const chips = await allText(page, '.topbar .chip')
    expect(
      '顶栏显示 BM25（单路）',
      chips.some((chip) => chip.includes('BM25')),
      JSON.stringify(chips),
      'ChatTopBar.modeLabel(keyword)=BM25',
    )
    await page.getByRole('button', { name: '关闭设置' }).click()
    const settingsClosed = await waitFor(page, '.settings-overlay', 3000, 'detached')
    expect(
      '设置抽屉关闭',
      settingsClosed,
      `settings-overlay 还在 DOM=${!settingsClosed}`,
      '关闭后 .settings-overlay 应从 DOM 移除（v-if）',
    )

    await page.getByLabel('输入问题').fill('一个假后端检索不到的问题')
    await page.getByRole('button', { name: '发送' }).click()
    const lowShown = await waitFor(page, '.low-confidence', 20000)
    expect(
      '拒答出现 .low-confidence 卡片',
      lowShown,
      `20s 内出现=${lowShown}`,
      '假后端关键字索引为空 → done 帧 refused=true；MessageList 的 low-confidence 分支应渲染',
    )
    const lowText = lowShown ? await text(page, '.low-confidence') : ''
    expect(
      '文案逐字包含「当前知识库中未能找到充分依据」',
      lowText.includes('当前知识库中未能找到充分依据'),
      `文案=「${lowText.replace(/\n/g, ' | ').slice(0, 80)}」`,
      'MessageList 的 .low-head strong 必须逐字是这句',
    )
    const lowButtons = lowShown
      ? await page.locator('.low-confidence').last().getByRole('button').allInnerTexts()
        .then((items) => items.map((item) => item.trim()))
        .catch(() => [])
      : []
    expect(
      '两个动作：换个问法 / 上传相关文档',
      lowButtons.length === 2
        && lowButtons.includes('换个问法')
        && lowButtons.includes('上传相关文档'),
      JSON.stringify(lowButtons),
      '看 MessageList 的 .low-actions（两个按钮）',
    )
    await shot(page, 'browser-05-1440-low-confidence.png')

    await page.locator('.low-confidence').last().getByRole('button', { name: '换个问法' }).click()
    await page.waitForTimeout(300)
    const focus = await page.evaluate(() => {
      const element = document.activeElement
      return {
        tag: element ? element.tagName : '',
        label: element ? element.getAttribute('aria-label') : '',
      }
    })
    expect(
      '「换个问法」把焦点放回输入框',
      focus.tag === 'TEXTAREA' && focus.label === '输入问题',
      JSON.stringify(focus),
      'ChatView.focusComposer → Composer.focus()；textarea 的 aria-label 是「输入问题」',
    )
  })

  // ── 8. 暗色模式 ───────────────────────────────────────────────────
  await scenario('[8] 暗色模式：切换到深色 → 再切回浅色', async () => {
    const toggle = page.locator('button.theme-toggle')
    const toggleCount = await toggle.count()
    const title = (await toggle.getAttribute('title')) || ''
    expect(
      '顶栏主题按钮在（title 形如「切换到深色主题」）',
      toggleCount === 1 && /切换到深色主题/.test(title),
      `count=${toggleCount} title=「${title}」`,
      'ThemeToggle 的 title 表示切换目标（深色/浅色）',
    )
    const before = await page.evaluate(() => document.documentElement.dataset.theme)
    await toggle.click()
    const dark = await page
      .waitForFunction(() => document.documentElement.dataset.theme === 'dark', null, { timeout: 3000 })
      .then(() => true)
      .catch(() => false)
    const afterDark = await page.evaluate(() => document.documentElement.dataset.theme)
    expect(
      '点击后 data-theme=dark',
      dark && before === 'light',
      `点击前=${before}（context 固定 colorScheme=light），点击后=${afterDark}`,
      '查 stores/theme.ts 的 apply() 与 ThemeToggle.toggleTheme',
    )
    await shot(page, 'browser-06-dark.png')
    await toggle.click()
    const light = await page
      .waitForFunction(() => document.documentElement.dataset.theme === 'light', null, { timeout: 3000 })
      .then(() => true)
      .catch(() => false)
    expect(
      '再切回 data-theme=light',
      light,
      `当前=${await page.evaluate(() => document.documentElement.dataset.theme)}`,
      '两档来回切换；theme.ts setTheme 应写 localStorage 与 <html data-theme>',
    )
    expect(
      '暗色切换过程中没有 console error',
      consoleErrors.length === 0,
      `${consoleErrors.length} 条${consoleErrors.slice(0, 2).join(' | ')}`,
      '看浏览器控制台与 MessageList/ThemeToggle 的运行时异常',
    )
  })

  // ── 9. 375×780 移动端 ─────────────────────────────────────────────
  await scenario('[9] 375×780：无横向 / 抽屉 / 底部引用弹层 / 输入区仍可用', async () => {
    await page.setViewportSize({ width: 375, height: 780 })
    await page.waitForTimeout(500)
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(
      '375 无横向滚动',
      overflow <= 0,
      `scrollWidth-innerWidth=${overflow}px`,
      '查 .messages 的 min-width / 顶栏 chips / 引用卡片',
    )
    const sidebarPanes = await count(page, '.sidebar-pane')
    const drawers = await count(page, '.drawer-left')
    expect(
      '侧栏收进抽屉（不再是常驻三栏）',
      sidebarPanes === 0 && drawers === 1,
      `sidebar-pane=${sidebarPanes} drawer-left=${drawers}`,
      'ui.compact 由 matchMedia(≤1180) 驱动；ChatView 的 v-if="!ui.compact"',
    )

    const composerBox = await page.locator('.composer').boundingBox()
    const sendVisible = await page.getByRole('button', { name: '发送' }).isVisible().catch(() => false)
    expect(
      '输入区与发送按钮仍可见',
      sendVisible
        && composerBox !== null
        && composerBox.width > 0
        && composerBox.y + composerBox.height <= 781,
      `sendVisible=${sendVisible} composer=${composerBox ? `${composerBox.x},${composerBox.y} ${composerBox.width}x${composerBox.height}` : '无盒子'}（视口 375×780）`,
      '查 Composer 的 padding 与 max-width；输入区不能被抽屉/弹层永久遮挡',
    )

    await page.getByRole('button', { name: '打开对话列表' }).click()
    const drawerOpen = await waitFor(page, '.drawer-left.open', 3000)
    expect(
      '打开对话列表 → .drawer-left.open',
      drawerOpen,
      `打开=${drawerOpen}`,
      'ChatTopBar 的 toggleSidebar → ui.drawerOpen；ChatView 的 .drawer-left 加 .open',
    )
    await page.waitForTimeout(300) // 等 0.18s 的抽屉滑入动画，截图才是打开后的样子
    await shot(page, 'browser-07-375-drawer.png')
    await page.keyboard.press('Escape')
    const drawerClosed = await waitFor(page, '.drawer-left.open', 3000, 'detached')
    const drawerAria = await page.locator('.drawer-left').getAttribute('aria-hidden')
    expect(
      'Escape 关闭抽屉',
      drawerClosed && drawerAria === 'true',
      `open class 仍在=${!drawerClosed}，aria-hidden=${drawerAria}`,
      'ChatView.onKeydown 的 Escape 分支（先设置抽屉、再右栏）',
    )

    const cards = await count(page, '.citation')
    expect(
      '消息里有可点的引用卡片',
      cards >= 1,
      `.citation=${cards}`,
      '上一步的答案应带 2 条引用；若为 0 查 WS done 帧的 citations',
    )
    await page.locator('.citation').first().click()
    const sheetOpen = await waitFor(page, '.panel-overlay.sheet.open', 5000)
    const panelVisible = await page.locator('.panel').isVisible().catch(() => false)
    expect(
      '点引用 → 底部弹层 .panel-overlay.sheet.open',
      sheetOpen && panelVisible,
      `sheet=${sheetOpen} panel=${panelVisible}`,
      'ui.mobile(≤768) 下引用面板走 .sheet；selectCitation 应打开面板',
    )
    const sheetBlocks = await waitFor(page, '.panel .block', 8000)
    expect(
      '弹层里也加载出原文块',
      sheetBlocks,
      `block=${await count(page, '.panel .block')}`,
      '同上：CitationPanel 的 watch(immediate) 拉 /documents/{id}/chunks',
    )
    await page.waitForTimeout(300) // 等 0.18s 的底部弹层滑入动画
    await shot(page, 'browser-08-375-panel.png')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  })

  // ── 10. 生成中「停止」────────────────────────────────────────────
  await scenario('[10] 状态：生成中点「停止」→ 已停止提示、无残留生成中', async () => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.waitForTimeout(400)
    await page.getByRole('button', { name: '新建对话' }).click()
    // 假后端流式 <100ms，人手点不到「停止」；在页面内看到生成中状态就点一次。
    await page.evaluate(() => {
      const state = { clicked: false }
      window.__dsStop = state
      const tick = () => {
        const last = [...document.querySelectorAll('.msg.assistant')].pop()
        if (last && last.querySelector('.streaming-actions') !== null && !state.clicked) {
          const stop = document.querySelector('.composer .stop')
          if (stop !== null) {
            state.clicked = true
            stop.click()
          }
        }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    await page.getByLabel('输入问题').fill('加班上限是多少？')
    await page.getByRole('button', { name: '发送' }).click()
    await page.waitForTimeout(1500)
    const clicked = await page.evaluate(() => window.__dsStop.clicked)
    const stoppedAlert = await text(page, '.msg.assistant .alert.info')
    if (!clicked) {
      markUnverified(
        '生成中「停止」→ 出现「已停止本次生成」提示',
        '没能在生成中点到「停止」（假后端流式太快）；手工验请用真后端',
      )
      markUnverified('停止后没有残留的生成中状态', '同上：没有真正触发停止')
    } else {
      expect(
        '生成中「停止」→ 出现「已停止本次生成」提示',
        stoppedAlert.includes('已停止本次生成'),
        `自动点到停止=${clicked}，提示=「${stoppedAlert.replace(/\n/g, ' ')}」`,
        'Composer 的「停止」调用 chat.stop()；MessageList 的 message.stopped 分支应显示「已停止本次生成。」',
      )
      const streamingLeft = await count(page, '.streaming-actions')
      const composerStopLeft = await count(page, '.composer .stop')
      const caretLeft = await count(page, '.caret')
      expect(
        '停止后没有残留的生成中状态',
        streamingLeft === 0 && composerStopLeft === 0 && caretLeft === 0,
        `.streaming-actions=${streamingLeft} .composer .stop=${composerStopLeft} .caret=${caretLeft}`,
        'stop() 应清掉 message.streaming 与 chat.generating',
      )
    }
  })

  // ── 11. 全程零报错、零弹窗 ────────────────────────────────────────
  await scenario('[11] 全程：console / pageerror / 失败请求 / 弹窗都为空', async () => {
    expect(
      '没有 console error',
      consoleErrors.length === 0,
      `${consoleErrors.length} 条${consoleErrors.slice(0, 3).join(' | ')}`,
      '看控制台第一条 error 的堆栈与来源文件',
    )
    expect(
      '没有未捕获异常（pageerror）',
      pageErrors.length === 0,
      `${pageErrors.length} 条${pageErrors.slice(0, 3).join(' | ')}`,
      'pageerror 通常来自事件处理或渲染期异常',
    )
    expect(
      '没有失败请求（requestfailed）',
      failedRequests.length === 0,
      `${failedRequests.length} 条${failedRequests.slice(0, 3).join(' | ')}`,
      '注意 WS 重连、/api/v1 的 aborted 请求',
    )
    expect(
      '没有浏览器弹窗（dialog）',
      dialogs.length === 0,
      `${dialogs.length} 条${dialogs.slice(0, 3).join(' | ')}`,
      '新交互禁止用 window.alert/confirm/prompt',
    )
    const blocking = dialogs.filter(
      (item) => item.startsWith('alert:') || item.startsWith('confirm:'),
    )
    expect(
      '没有 alert / confirm（新交互禁止用它们）',
      blocking.length === 0,
      `${blocking.length} 条${blocking.join(' | ')}`,
      '重命名/删除等二次确认必须在页面内做（SessionList 的 .confirm）',
    )
  })

  await browser.close()

  const passed = checks - failures - unverified
  console.log(`\n浏览器验收：${checks} 项检查 / ${passed} 通过 / ${failures} 失败 / ${unverified} 未验证`)
  if (failures > 0) {
    console.log('失败项：')
    for (const name of failed) console.log(`  - ${name}`)
  }
  if (appBugs.length > 0) {
    console.log('\n发现的 app bug（本脚本只测不改，源码未动）：')
    for (const bug of appBugs) console.log(`  - ${bug}`)
  }
  if (unverified > 0) {
    console.log('未验证项（环境/时序限制，不是通过）：')
    for (const item of unverifiedNames) console.log(`  - ${item}`)
  }
  console.log('\n复现：见本文件头部「跑法」；失败项先看它打印的 class/接口提示。')
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('脚本本身出错:', error)
  process.exit(2)
})

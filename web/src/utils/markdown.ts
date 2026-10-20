/**
 * 自研 Markdown 渲染器（零依赖）。
 *
 * 为什么自己写而不是装 `markdown-it`：交付环境的 npm registry 不可达，CI 又用
 * `npm ci` —— 新增依赖会直接卡住构建。这个项目只需要「聊天答案」这一档 Markdown：
 * 标题 / 列表 / 引用 / 表格 / 围栏代码 / 行内格式 / 引用标签。
 *
 * 安全模型（比功能重要）：
 * 1. **不输出原文 HTML**。所有文本先 `escapeHtml`，标签只由本文件产生；
 * 2. 链接只放行 `http(s)` / `mailto` / 站内相对路径，`javascript:` 一律丢弃；
 * 3. 图片只放行 `http(s)` 与 `data:image/*`；
 * 4. 引用标签只认 `[n]`，且必须在本次引用列表里 —— 认不出的保留成灰色文本，
 *    不猜、不跳到别处。
 *
 * 流式渲染的容错：未闭合的围栏代码按「到结尾都是代码」处理，未闭合的强调符按普通
 * 文本处理。这样 token 逐段到达时不会闪出半截标题。
 */

import type { Citation } from '../api/types'

import { escapeHtml, highlight } from './highlight'

export interface RenderOptions {
  /** 本次答案的引用清单；没有就走「未知引用」的灰色样式。 */
  citations?: readonly Citation[]
}

export interface RenderResult {
  html: string
  /** 代码块原文，按 `data-code` 下标取 —— 复制按钮不把整段代码塞进属性里。 */
  codes: string[]
}

interface Context {
  codes: string[]
  citations: Map<number, Citation>
}

/** 行内特殊字符：遇到它们才进入行内语法分支。 */
const INLINE_SPECIAL = /[`!\[\]*_~\n<\\]/g

const LIST_RE = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/
const FENCE_RE = /^\s{0,3}```\s*([^\s`]*)[^\n]*$/
const FENCE_END_RE = /^\s{0,3}```\s*$/
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/
const HR_RE = /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/
const QUOTE_RE = /^\s{0,3}>\s?(.*)$/
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/

function safeUrl(raw: string): string | null {
  const url = raw.trim()
  if (url.length === 0) return null
  if (/^(https?:|mailto:)/i.test(url)) return url
  // 站内相对路径：`/x`、`#x`、`x`。**`//host` 是协议相对外链**，不能当站内路径放行。
  if (url.startsWith('//')) return null
  if (/^[/#]/.test(url) || /^[^:\s]+(?:[/?#]|$)/.test(url)) return url
  return null
}

function safeImageUrl(raw: string): string | null {
  const url = raw.trim()
  if (/^https?:/i.test(url)) return url
  if (/^data:image\/(png|jpe?g|gif|webp);base64,/i.test(url)) return url
  return null
}

function renderCitation(index: number, context: Context): string {
  const citation = context.citations.get(index)
  if (citation === undefined) {
    return `<span class="cite-ref unknown" title="引用 [${index}] 不在本次引用清单里">[${index}]</span>`
  }
  const label = `查看引用 ${index}：${citation.source}`
  return (
    `<button type="button" class="cite-ref" data-cite="${index}" ` +
    `aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">[${index}]</button>`
  )
}

/** 行内渲染：只处理答案里真会出现的语法，不追求完整 CommonMark。 */
function renderInline(text: string, context: Context): string {
  let out = ''
  let i = 0
  while (i < text.length) {
    const rest = text.slice(i)

    // 反斜杠转义：\* 输出字面星号
    if (rest[0] === '\\' && rest.length > 1) {
      out += escapeHtml(rest[1])
      i += 2
      continue
    }

    // 行内代码：内部不再解析任何 Markdown
    const code = /^`([^`\n]+)`/.exec(rest)
    if (code !== null) {
      out += `<code>${escapeHtml(code[1])}</code>`
      i += code[0].length
      continue
    }

    // 图片
    const image = /^!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(rest)
    if (image !== null) {
      const src = safeImageUrl(image[2])
      if (src !== null) {
        out += `<img src="${escapeHtml(src)}" alt="${escapeHtml(image[1])}" loading="lazy" />`
      } else {
        out += escapeHtml(image[1])
      }
      i += image[0].length
      continue
    }

    // 链接：[文字](地址)
    const link = /^\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(rest)
    if (link !== null) {
      const href = safeUrl(link[2])
      const label = renderInline(link[1], context)
      if (href !== null) {
        const external = /^https?:/i.test(href)
        const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : ''
        out += `<a href="${escapeHtml(href)}"${attrs}>${label}</a>`
      } else {
        out += label
      }
      i += link[0].length
      continue
    }

    // 引用标签 [n]（必须在列表里才变成可点按钮）
    const cite = /^\[(\d{1,3})\]/.exec(rest)
    if (cite !== null) {
      out += renderCitation(Number(cite[1]), context)
      i += cite[0].length
      continue
    }

    // 加粗
    if (rest.startsWith('**')) {
      const end = rest.indexOf('**', 2)
      if (end > 2) {
        out += `<strong>${renderInline(rest.slice(2, end), context)}</strong>`
        i += end + 2
        continue
      }
    }

    // 删除线
    if (rest.startsWith('~~')) {
      const end = rest.indexOf('~~', 2)
      if (end > 2) {
        out += `<del>${renderInline(rest.slice(2, end), context)}</del>`
        i += end + 2
        continue
      }
    }

    // 斜体：星号两侧不能是空白，避免把乘号/通配符吃掉
    if (rest[0] === '*') {
      const italic = /^\*([^*\n]+)\*/.exec(rest)
      if (italic !== null && !/^\s|\s$/.test(italic[1])) {
        out += `<em>${renderInline(italic[1], context)}</em>`
        i += italic[0].length
        continue
      }
    }

    if (rest[0] === '\n') {
      out += '<br />'
      i += 1
      continue
    }

    // 普通文本：吃到下一个特殊字符为止（< 也在这里被转义，原文 HTML 不会执行）
    INLINE_SPECIAL.lastIndex = i
    const special = INLINE_SPECIAL.exec(text)
    const end = special === null ? text.length : special.index
    if (end > i) {
      out += escapeHtml(text.slice(i, end))
      i = end
    } else {
      out += escapeHtml(text[i])
      i += 1
    }
  }
  return out
}

function renderCodeBlock(code: string, language: string, context: Context): string {
  const lang = language.trim().toLowerCase()
  const index = context.codes.push(code) - 1
  const label = lang.length > 0 ? lang : 'text'
  return (
    '<div class="code-block">' +
    `<div class="code-head"><span class="code-lang">${escapeHtml(label)}</span>` +
    `<button type="button" class="code-copy" data-code="${index}" aria-label="复制代码">复制</button></div>` +
    `<pre><code>${highlight(code, lang)}</code></pre>` +
    '</div>'
  )
}

/** 把列表项里的单段落换成紧凑形式（避免每项外面多一层 <p>）。 */
function tighten(html: string): string {
  const match = /^<p>([\s\S]*)<\/p>\s*$/.exec(html)
  if (match === null) return html
  if (match[1].includes('<p>')) return html
  return match[1]
}

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  const cells: string[] = []
  let current = ''
  for (let i = 0; i < trimmed.length; i += 1) {
    const char = trimmed[i]
    if (char === '\\' && trimmed[i + 1] === '|') {
      current += '|'
      i += 1
    } else if (char === '|') {
      cells.push(current.trim())
      current = ''
    } else {
      current += char
    }
  }
  cells.push(current.trim())
  return cells
}

function isTableStart(lines: string[], index: number): boolean {
  if (!lines[index].includes('|')) return false
  const next = lines[index + 1]
  return next !== undefined && TABLE_SEP_RE.test(next)
}

function renderTable(lines: string[], start: number, context: Context): { html: string; next: number } {
  const header = splitRow(lines[start])
  const body: string[][] = []
  let i = start + 2
  while (i < lines.length && lines[i].trim().length > 0 && lines[i].includes('|')) {
    body.push(splitRow(lines[i]))
    i += 1
  }
  const head = header.map((cell) => `<th>${renderInline(cell, context)}</th>`).join('')
  const rows = body
    .map((cells) => {
      const tds = header
        .map((_, column) => `<td>${renderInline(cells[column] ?? '', context)}</td>`)
        .join('')
      return `<tr>${tds}</tr>`
    })
    .join('')
  const html =
    '<div class="table-wrap"><table>' +
    `<thead><tr>${head}</tr></thead>` +
    (rows.length > 0 ? `<tbody>${rows}</tbody>` : '') +
    '</table></div>'
  return { html, next: i }
}

function renderList(lines: string[], start: number, context: Context): { html: string; next: number } {
  const first = LIST_RE.exec(lines[start])
  if (first === null) return { html: '', next: start + 1 }
  const baseIndent = first[1].length
  const ordered = /\d/.test(first[2])
  const startNumber = ordered ? Number.parseInt(first[2], 10) || 1 : 1
  const items: string[][] = []
  let i = start

  while (i < lines.length) {
    const match = LIST_RE.exec(lines[i])
    if (match === null) break
    const indent = match[1].length
    if (indent < baseIndent) break
    if (indent === baseIndent) {
      items.push([match[3]])
      i += 1
      continue
    }
    // 更深缩进 = 上一层某项的续行（嵌套列表也在这里，保持相对缩进）
    const lastItem = items[items.length - 1]
    if (lastItem === undefined) break
    const strip = Math.min(indent, baseIndent + 2)
    lastItem.push(lines[i].slice(strip))
    i += 1
  }

  // 续行之后可能还有空行 + 缩进块（例如列表项里的代码块）：
  // 这里只吸收紧跟的缩进行，避免把下一个段落误吞进列表。
  while (i < lines.length) {
    if (lines[i].trim().length > 0) break
    const next = lines[i + 1]
    if (next === undefined || !/^\s{2,}\S/.test(next)) break
    const lastItem = items[items.length - 1]
    if (lastItem === undefined) break
    lastItem.push('')
    i += 1
    while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
      lastItem.push(lines[i].slice(baseIndent + 2))
      i += 1
    }
  }

  const rendered = items.map((item) => {
    let content = item.join('\n')
    let prefix = ''
    const task = /^\[([ xX])\]\s+/.exec(content)
    if (task !== null) {
      content = content.slice(task[0].length)
      prefix =
        '<input type="checkbox" disabled' +
        (task[1].toLowerCase() === 'x' ? ' checked' : '') +
        ' aria-label="任务项" /> '
    }
    const inner = tighten(renderBlocks(content.split('\n'), context))
    return `<li>${prefix}${inner}</li>`
  })

  const tag = ordered ? 'ol' : 'ul'
  const startAttr = ordered && startNumber !== 1 ? ` start="${startNumber}"` : ''
  return { html: `<${tag}${startAttr}>${rendered.join('')}</${tag}>`, next: i }
}

function isBlockStart(lines: string[], index: number): boolean {
  const line = lines[index]
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    HR_RE.test(line) ||
    QUOTE_RE.test(line) ||
    LIST_RE.test(line) ||
    isTableStart(lines, index)
  )
}

function renderBlocks(lines: string[], context: Context): string {
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]

    if (line.trim().length === 0) {
      i += 1
      continue
    }

    // 围栏代码（流式时可能没有闭合：到结尾都算代码）
    const fence = FENCE_RE.exec(line)
    if (fence !== null) {
      const language = fence[1] ?? ''
      const body: string[] = []
      i += 1
      while (i < lines.length && !FENCE_END_RE.test(lines[i])) {
        body.push(lines[i])
        i += 1
      }
      if (i < lines.length) i += 1
      out.push(renderCodeBlock(body.join('\n'), language, context))
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading !== null) {
      const level = heading[1].length
      out.push(`<h${level}>${renderInline(heading[2], context)}</h${level}>`)
      i += 1
      continue
    }

    if (HR_RE.test(line)) {
      out.push('<hr />')
      i += 1
      continue
    }

    // 引用块：去掉 > 前缀后递归解析
    if (QUOTE_RE.test(line)) {
      const inner: string[] = []
      while (i < lines.length) {
        const quote = QUOTE_RE.exec(lines[i])
        if (quote === null) break
        inner.push(quote[1])
        i += 1
      }
      out.push(`<blockquote>${renderBlocks(inner, context)}</blockquote>`)
      continue
    }

    // 表格
    if (isTableStart(lines, i)) {
      const table = renderTable(lines, i, context)
      out.push(table.html)
      i = table.next
      continue
    }

    // 列表
    if (LIST_RE.test(line)) {
      const list = renderList(lines, i, context)
      out.push(list.html)
      i = list.next
      continue
    }

    // 段落：直到空行或下一个块级起点
    const paragraph: string[] = [line]
    i += 1
    while (i < lines.length && lines[i].trim().length > 0 && !isBlockStart(lines, i)) {
      paragraph.push(lines[i])
      i += 1
    }
    out.push(`<p>${renderInline(paragraph.join('\n'), context)}</p>`)
  }
  return out.join('\n')
}

/**
 * 输入:Markdown 源码 + 可选引用清单。
 * 行为:先做块级切分（代码/标题/列表/表格/引用/段落），再在行内解析格式与 `[n]`。
 * 输出:`{ html, codes }` —— html 已全量转义，codes 供复制按钮使用。
 */
export function renderMarkdown(source: string, options: RenderOptions = {}): RenderResult {
  const context: Context = {
    codes: [],
    citations: new Map((options.citations ?? []).map((item) => [item.index, item])),
  }
  const normalized = source.replace(/\r\n?/g, '\n')
  const html = renderBlocks(normalized.split('\n'), context)
  return { html, codes: context.codes }
}

/** 输入:Markdown 源码。行为:压成一行纯文本（用于摘要/会话别名兜底）。输出:纯文本。 */
export function stripMarkdown(source: string): string {
  return source
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\[(\d+)\]/g, ' ')
    .replace(/[`*_~#>|-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

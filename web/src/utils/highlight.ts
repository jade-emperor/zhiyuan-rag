/**
 * 轻量语法高亮：不引第三方库（npm registry 在交付环境不可达，且 CI 用 `npm ci`）。
 *
 * 原则：
 * 1. **只做装饰**。所有片段先 `escapeHtml` 再拼标签，绝不把原文当 HTML 输出 ——
 *    Markdown 的 HTML 是关闭的，这里是第二道闸。
 * 2. 认不出的语言按纯文本输出，不猜。猜错比不高亮更伤可读性。
 * 3. 类名固定（`.hl-keyword` 等），颜色全部来自 CSS 变量，暗色主题自动跟随。
 */

/** 输入:任意文本。行为:转义 & < > " '。输出:可安全放进 HTML 的文本。 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

interface LanguageSpec {
  keywords: Set<string>
  types: Set<string>
  /** `//` 与 `/* *\/` 注释 */
  slash: boolean
  /** `#` 行注释 */
  hash: boolean
  /** `--` 行注释（SQL） */
  dash: boolean
  /** `<!-- -->` 注释 + 标签着色 */
  markup: boolean
  /** 属性名（`foo:`）着色为函数色（CSS/YAML） */
  property: boolean
}

const JS_KEYWORDS = [
  'as', 'async', 'await', 'break', 'case', 'catch', 'class', 'const', 'continue',
  'debugger', 'default', 'delete', 'do', 'else', 'export', 'extends', 'finally',
  'for', 'from', 'function', 'get', 'if', 'implements', 'import', 'in', 'instanceof',
  'interface', 'let', 'new', 'of', 'return', 'set', 'static', 'super', 'switch',
  'this', 'throw', 'try', 'type', 'typeof', 'var', 'void', 'while', 'with', 'yield',
]
const TS_TYPES = [
  'any', 'boolean', 'never', 'number', 'object', 'string', 'symbol', 'unknown',
  'void', 'bigint', 'Record', 'Partial', 'Readonly', 'Array', 'Promise', 'Map',
  'Set', 'Date', 'RegExp', 'Error',
]
const PY_KEYWORDS = [
  'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def',
  'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if',
  'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise',
  'return', 'try', 'while', 'with', 'yield',
]
const PY_TYPES = [
  'bool', 'bytes', 'dict', 'float', 'frozenset', 'int', 'list', 'object', 'set',
  'str', 'tuple', 'None', 'True', 'False', 'self', 'cls',
]
const SHELL_KEYWORDS = [
  'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'do', 'done', 'case', 'esac',
  'function', 'in', 'return', 'export', 'local', 'echo', 'cd', 'test', 'set', 'unset',
]
const SQL_KEYWORDS = [
  'select', 'from', 'where', 'and', 'or', 'not', 'insert', 'into', 'values',
  'update', 'set', 'delete', 'create', 'table', 'alter', 'drop', 'index', 'join',
  'left', 'right', 'inner', 'outer', 'on', 'group', 'by', 'order', 'having',
  'limit', 'offset', 'as', 'distinct', 'union', 'all', 'case', 'when', 'then',
  'else', 'end', 'null', 'is', 'like', 'in', 'exists', 'between', 'primary',
  'key', 'foreign', 'references', 'default', 'asc', 'desc', 'count', 'sum', 'avg',
  'min', 'max',
]
const JAVA_KEYWORDS = [
  'abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char',
  'class', 'const', 'continue', 'default', 'do', 'double', 'else', 'enum',
  'extends', 'final', 'finally', 'float', 'for', 'if', 'implements', 'import',
  'instanceof', 'int', 'interface', 'long', 'native', 'new', 'package', 'private',
  'protected', 'public', 'return', 'short', 'static', 'strictfp', 'super',
  'switch', 'synchronized', 'this', 'throw', 'throws', 'transient', 'try', 'void',
  'volatile', 'while', 'record', 'sealed', 'var',
]
const GO_KEYWORDS = [
  'break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else',
  'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import', 'interface', 'map',
  'package', 'range', 'return', 'select', 'struct', 'switch', 'type', 'var',
]
const RUST_KEYWORDS = [
  'as', 'async', 'await', 'break', 'const', 'continue', 'crate', 'dyn', 'else',
  'enum', 'extern', 'fn', 'for', 'if', 'impl', 'in', 'let', 'loop', 'match',
  'mod', 'move', 'mut', 'pub', 'ref', 'return', 'self', 'Self', 'static', 'struct',
  'super', 'trait', 'type', 'unsafe', 'use', 'where', 'while',
]
const YAML_KEYWORDS = ['true', 'false', 'null', 'yes', 'no', 'on', 'off']
const CSS_KEYWORDS = ['important', 'media', 'supports', 'keyframes', 'font-face', 'import', 'from', 'to']
const MARKDOWN_KEYWORDS = ['true', 'false', 'null']

const SETS = {
  javascript: { keywords: [...JS_KEYWORDS, ...TS_TYPES], types: TS_TYPES },
  python: { keywords: PY_KEYWORDS, types: PY_TYPES },
  shell: { keywords: SHELL_KEYWORDS, types: [] },
  sql: { keywords: SQL_KEYWORDS, types: [] },
  java: { keywords: JAVA_KEYWORDS, types: [] },
  go: { keywords: GO_KEYWORDS, types: [] },
  rust: { keywords: RUST_KEYWORDS, types: [] },
  yaml: { keywords: YAML_KEYWORDS, types: [] },
  css: { keywords: CSS_KEYWORDS, types: [] },
  markdown: { keywords: MARKDOWN_KEYWORDS, types: [] },
  json: { keywords: ['true', 'false', 'null'], types: [] },
} as const

type SpecName = keyof typeof SETS

const FLAGS: Record<SpecName, Omit<LanguageSpec, 'keywords' | 'types'>> = {
  javascript: { slash: true, hash: false, dash: false, markup: false, property: false },
  python: { slash: false, hash: true, dash: false, markup: false, property: false },
  shell: { slash: false, hash: true, dash: false, markup: false, property: false },
  sql: { slash: false, hash: false, dash: true, markup: false, property: false },
  java: { slash: true, hash: false, dash: false, markup: false, property: false },
  go: { slash: true, hash: false, dash: false, markup: false, property: false },
  rust: { slash: true, hash: false, dash: false, markup: false, property: false },
  yaml: { slash: false, hash: true, dash: false, markup: false, property: true },
  css: { slash: true, hash: false, dash: false, markup: false, property: true },
  markdown: { slash: false, hash: false, dash: false, markup: false, property: false },
  json: { slash: false, hash: false, dash: false, markup: false, property: false },
}

const ALIASES: Record<string, SpecName> = {
  js: 'javascript',
  javascript: 'javascript',
  jsx: 'javascript',
  ts: 'javascript',
  tsx: 'javascript',
  typescript: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  py: 'python',
  python: 'python',
  sh: 'shell',
  bash: 'shell',
  shell: 'shell',
  zsh: 'shell',
  console: 'shell',
  sql: 'sql',
  mysql: 'sql',
  postgres: 'sql',
  java: 'java',
  go: 'go',
  golang: 'go',
  rust: 'rust',
  rs: 'rust',
  yaml: 'yaml',
  yml: 'yaml',
  css: 'css',
  scss: 'css',
  less: 'css',
  json: 'json',
  jsonc: 'json',
  md: 'markdown',
  markdown: 'markdown',
}

/** 规范化的语言名（认不出原样返回，交给纯文本分支）。 */
export function normalizeLang(raw: string | undefined): string {
  const value = (raw ?? '').trim().toLowerCase().split(/[\s,{]/)[0] ?? ''
  return ALIASES[value] ?? value
}

function specFor(lang: string): LanguageSpec | null {
  const name = lang as SpecName
  if (!(name in SETS)) return null
  const set = SETS[name]
  return {
    keywords: new Set(set.keywords),
    types: new Set(set.types),
    ...FLAGS[name],
  }
}

function commentSource(spec: LanguageSpec): string | null {
  const parts: string[] = []
  if (spec.slash) parts.push(String.raw`\/\/[^\n]*`, String.raw`\/\*[\s\S]*?\*\/`)
  if (spec.hash) parts.push(String.raw`#[^\n]*`)
  if (spec.dash) parts.push(String.raw`--[^\n]*`)
  return parts.length > 0 ? parts.join('|') : null
}

const STRING_SOURCE = String.raw`"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\x60(?:\\.|[^\x60\\])*\x60`
const NUMBER_SOURCE = String.raw`\b0[xX][0-9a-fA-F]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`
const WORD_SOURCE = String.raw`[A-Za-z_$][\w$]*`

/** HTML/XML：整段标签着色，标签内文本按普通文本转义。 */
function highlightMarkup(code: string): string {
  let out = ''
  let index = 0
  const tag = /<!--[\s\S]*?-->|<\/?[A-Za-z][^>]*>/g
  let match: RegExpExecArray | null
  while ((match = tag.exec(code)) !== null) {
    out += escapeHtml(code.slice(index, match.index))
    const text = match[0]
    if (text.startsWith('<!--')) out += `<span class="hl-comment">${escapeHtml(text)}</span>`
    else out += `<span class="hl-keyword">${escapeHtml(text)}</span>`
    index = match.index + text.length
  }
  out += escapeHtml(code.slice(index))
  return out
}

/**
 * 输入:源码 + 语言名。行为:按语言切词并套上 `.hl-*` span。
 * 输出:可安全放进 `<code>` 的 HTML。
 */
export function highlight(code: string, language?: string): string {
  const lang = normalizeLang(language)
  if (lang === 'html' || lang === 'xml' || lang === 'vue' || lang === 'svg') {
    return highlightMarkup(code)
  }

  const spec = specFor(lang)
  if (spec === null) return escapeHtml(code)

  const comment = commentSource(spec)
  const parts: string[] = []
  if (comment !== null) parts.push(`(?<comment>${comment})`)
  parts.push(`(?<string>${STRING_SOURCE})`)
  parts.push(`(?<number>${NUMBER_SOURCE})`)
  if (spec.property) parts.push('(?<property>[A-Za-z_][\\w-]*)(?=\\s*:)')
  parts.push(`(?<word>${WORD_SOURCE})`)
  const regex = new RegExp(parts.join('|'), 'g')

  let out = ''
  let last = 0
  let match: RegExpExecArray | null
  while ((match = regex.exec(code)) !== null) {
    out += escapeHtml(code.slice(last, match.index))
    const groups = match.groups ?? {}
    const text = match[0]
    if (groups.comment !== undefined) {
      out += `<span class="hl-comment">${escapeHtml(text)}</span>`
    } else if (groups.string !== undefined) {
      out += `<span class="hl-string">${escapeHtml(text)}</span>`
    } else if (groups.number !== undefined) {
      out += `<span class="hl-number">${escapeHtml(text)}</span>`
    } else if (groups.property !== undefined) {
      out += `<span class="hl-function">${escapeHtml(text)}</span>`
    } else {
      const next = code[match.index + text.length]
      if (spec.keywords.has(text)) out += `<span class="hl-keyword">${escapeHtml(text)}</span>`
      else if (spec.types.has(text)) out += `<span class="hl-type">${escapeHtml(text)}</span>`
      else if (next === '(') out += `<span class="hl-function">${escapeHtml(text)}</span>`
      else out += escapeHtml(text)
    }
    last = match.index + text.length
  }
  out += escapeHtml(code.slice(last))
  return out
}

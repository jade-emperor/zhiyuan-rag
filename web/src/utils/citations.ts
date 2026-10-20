/**
 * 引用展示的纯函数：页码话术、来源名、表格识别、关键词高亮。
 *
 * 三条口径：
 * 1. **`page === null` 就是「不知道」**（md/txt 没有页码，老块也可能没有）——
 *    显示「页码未知」，绝不拿 0/1 顶上；
 * 2. 来源标签用 `source`（知识库名），文件名用 `doc_id` —— 两者不是一回事；
 * 3. 高亮只对**纯文本**做，先转义再加 `<mark>`，不会把预览里的尖括号变成标签。
 */

import type { Citation } from '../api/types'

import { escapeHtml } from './highlight'

/** 输入:引用。行为:按 page 是否为 null 选话术。输出:页码标签。 */
export function pageLabel(citation: Citation): string {
  return citation.page === null ? '页码未知' : `第 ${citation.page} 页`
}

/** 输入:引用。行为:取文件名；没有就退回 doc_id 本身。输出:短文件名。 */
export function fileName(citation: Citation): string {
  const value = citation.doc_id || citation.source || '未命名来源'
  const normalized = value.replace(/\\/g, '/')
  const parts = normalized.split('/')
  return parts[parts.length - 1] || value
}

/** 输入:一段预览文本。行为:看它是不是 Markdown 表格（连续行都含 |）。输出:是否像表格。 */
export function looksLikeTable(text: string): boolean {
  const lines = text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
  if (lines.length < 2) return false
  const pipeRows = lines.filter((line) => line.includes('|'))
  return pipeRows.length >= 2 && pipeRows.length / lines.length >= 0.6
}

/** 输入:表格文本。行为:按行/列切成二维数组（去掉外侧竖线）。输出:最多 rows 行的预览。 */
export function tablePreview(text: string, rows = 4, columns = 5): string[][] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.includes('|'))
    .slice(0, rows)
    .map((line) =>
      line
        .replace(/^\|/, '')
        .replace(/\|$/, '')
        .split('|')
        .map((cell) => cell.trim())
        .slice(0, columns),
    )
}

/**
 * 输入:问题。行为:切成用于高亮的关键词（去重、去停用词、长度 ≥2）。
 * 中文没有空格，整句会被当成一个词 —— 长度 ≥4 的中文串再拆成 2-gram，
 * 这样「加班上限是多少」也能在原文里点亮「加班」「上限」「多少」。
 * 输出:词列表。
 */
export function queryTerms(query: string, limit = 10): string[] {
  const stop = new Set([
    '的',
    '了',
    '是',
    '在',
    '和',
    '与',
    '或',
    '有',
    '吗',
    '呢',
    '怎么',
    '什么',
    '如何',
    '为什么',
    '请问',
    '一下',
    '可以',
    '需要',
    'the',
    'and',
    'for',
    'with',
    'what',
    'how',
  ])
  const seen = new Set<string>()
  const terms: string[] = []
  const push = (term: string): boolean => {
    if (term.length < 2 || stop.has(term) || seen.has(term)) return false
    seen.add(term)
    terms.push(term)
    return terms.length >= limit
  }
  for (const raw of query.toLowerCase().split(/[\s,，。?!！？；;:：、"'“”‘’()（）[\]【】<>《》/\\|]+/)) {
    const term = raw.trim()
    if (term.length < 2) continue
    if (/[\u4e00-\u9fff]/.test(term) && term.length >= 4) {
      // 先给完整短句（≤6 字时），再给 2-gram；两者都去重
      if (term.length <= 6 && push(term)) break
      for (let i = 0; i < term.length - 1; i += 1) {
        if (push(term.slice(i, i + 2))) break
      }
      if (terms.length >= limit) break
      continue
    }
    if (push(term)) break
  }
  return terms
}

/**
 * 输入:原文 + 关键词。行为:先转义，再用 `<mark class="term">` 包住命中词。
 * 输出:安全 HTML（只含 mark 标签）。命中词按长度倒序，避免短词先吃掉长词。
 */
export function highlightTerms(text: string, terms: readonly string[]): string {
  const escaped = escapeHtml(text)
  const candidates = [...terms]
    .filter((term) => term.length > 0)
    .sort((left, right) => right.length - left.length)
  if (candidates.length === 0) return escaped
  const pattern = candidates
    .map((term) => escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|')
  if (pattern.length === 0) return escaped
  return escaped.replace(new RegExp(`(${pattern})`, 'gi'), '<mark class="term">$1</mark>')
}

/** 输入:预览长度与上限。行为:截断并补省略号。输出:展示文本。 */
export function shorten(text: string, max = 160): string {
  if (text.length <= max) return text
  return `${text.slice(0, max)}…`
}

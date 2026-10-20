/**
 * 置信度与相关性展示口径。
 *
 * **为什么不能直接拿 `score` 比 0.8 / 0.5**：后端的 `Citation.score` 是**召回/融合分**，
 * 四种模式的量纲完全不同（推导见维护者本地台账 `docs/STATUS.md`，**不随仓库发布**；这里不依赖它才读得懂）：
 *
 * - `vector`：余弦相似度，量级 0~1；
 * - `keyword`：BM25 原始分，量级可以到十几；
 * - `hybrid + rrf`：`Σ w/(k+rank)`，量级 ≈0.01；
 * - `hybrid + weighted`：两路 min-max 后加权，0~1，但**是同一批候选里的相对分**。
 *
 * 所以本文件只认三种「可解释的绝对量」：
 * 1. `channel === 'faq'`：标准答案，且过了 FAQ 的阈值与覆盖率门槛 → 高；
 * 2. `rerank_score`：CrossEncoder 的 sigmoid 输出，**恒在 0~1** → 直接用；
 * 3. `vector` 单路的 `score`：余弦相似度，夹到 0~1 → 用，但只作为「相似度」说明。
 *
 * 其余模式（BM25 / RRF / weighted）**不给绝对档位**，改为「未标定」+ 本次结果内的
 * 相对条。这是刻意的诚实：把 RRF 的 0.016 标成「低置信」会让每一次正常问答都报警。
 */

import type { Citation, FusionMode, RetrievalMode } from '../api/types'

export type ConfidenceLevel = 'high' | 'medium' | 'low' | 'unrated'

export type ConfidenceBasis = 'faq' | 'rerank' | 'vector' | 'refused' | 'relative' | 'unrated'

export interface ConfidenceInput {
  channel: string | null
  refused: boolean
  citations: readonly Citation[]
  /** 这一问实际用的检索方式（消息创建时的快照；旧消息没有就是 undefined）。 */
  mode?: RetrievalMode
  fusion?: FusionMode
}

export interface ConfidenceAssessment {
  level: ConfidenceLevel
  basis: ConfidenceBasis
  /** 0~1 的绝对分；null = 这一问没有可解释的绝对分。 */
  value: number | null
  /** 徽章上的短标签；高置信度为空串（不打扰）。 */
  badge: string
  /** 鼠标悬停/详情里的解释。 */
  detail: string
}

/** 高 / 中 / 低的边界。high 是严格大于 0.8，medium 含 0.5。 */
export function bandOf(value: number): ConfidenceLevel {
  if (value > 0.8) return 'high'
  if (value >= 0.5) return 'medium'
  return 'low'
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

function maxRerank(citations: readonly Citation[]): number | null {
  const values = citations
    .map((item) => item.rerank_score)
    .filter((value): value is number => value !== null && Number.isFinite(value))
  return values.length > 0 ? Math.max(...values) : null
}

/** 输入:一次回答的通道/拒答/引用/检索参数。行为:按可解释的量纲给置信档。输出:档位 + 依据 + 解释。 */
export function assessConfidence(input: ConfidenceInput): ConfidenceAssessment {
  if (input.channel === 'greeting' || input.channel === 'throttled') {
    return {
      level: 'unrated',
      basis: 'unrated',
      value: null,
      badge: '',
      detail: '寒暄 / 限流',
    }
  }

  if (input.refused) {
    return {
      level: 'low',
      basis: 'refused',
      value: null,
      badge: '资料不足',
      detail: '资料不足',
    }
  }

  if (input.channel === 'faq') {
    return {
      level: 'high',
      basis: 'faq',
      value: null,
      badge: '',
      detail: 'FAQ',
    }
  }

  if (input.channel === 'rag' && input.citations.length === 0) {
    return {
      level: 'low',
      basis: 'refused',
      value: null,
      badge: '资料不足',
      detail: '资料不足',
    }
  }

  // 历史消息来自 `/history`：那里没有 channel 字段（只有 question/answer/citations）。
  // 有 citations 就说明这一轮走过知识库检索 —— 按 rag 评估，而不是因为"channel 未知"
  // 就把 rerank 分丢掉（那会让切回旧会话后置信度提示凭空消失）。
  const ragLike = input.channel === 'rag' || input.citations.length > 0
  if (!ragLike) {
    return {
      level: 'unrated',
      basis: 'unrated',
      value: null,
      badge: '',
      detail: '',
    }
  }

  const rerank = maxRerank(input.citations)
  if (rerank !== null) {
    const value = clamp01(rerank)
    const level = bandOf(value)
    return {
      level,
      basis: 'rerank',
      value,
      badge: level === 'high' ? '' : level === 'medium' ? 'AI 建议' : '建议核实',
      detail:
        level === 'high'
          ? `重排 ${value.toFixed(3)}`
          : level === 'medium'
            ? `重排 ${value.toFixed(3)} · 建议核实`
            : `重排 ${value.toFixed(3)}`,
    }
  }

  // 单路向量：IP 检索的是 L2 归一化向量，分数就是余弦相似度。
  if (input.mode === 'vector') {
    const value = clamp01(Math.max(...input.citations.map((item) => item.score)))
    const level = bandOf(value)
    return {
      level,
      basis: 'vector',
      value,
      badge: level === 'high' ? '' : level === 'medium' ? 'AI 建议' : '建议核实',
      detail: `余弦 ${value.toFixed(3)}`,
    }
  }

  return {
    level: 'unrated',
    basis: 'relative',
    value: null,
    badge: '',
    detail: '未标定',
  }
}

export interface Strength {
  /** 0~1 的条宽；null = 不做条。 */
  value: number | null
  relative: boolean
  hint: string
}

/**
 * 输入:一条引用 + 本次全部引用 + 置信依据。
 * 行为:能算绝对分就用绝对分，否则按本次最大分归一化成相对条。
 * 输出:进度条数值与解释。
 */
export function citationStrength(
  citation: Citation,
  all: readonly Citation[],
  assessment: ConfidenceAssessment,
): Strength {
  if (assessment.basis === 'rerank' && citation.rerank_score !== null) {
    return {
      value: clamp01(citation.rerank_score),
      relative: false,
      hint: '重排分',
    }
  }
  if (assessment.basis === 'vector') {
    return {
      value: clamp01(citation.score),
      relative: false,
      hint: '余弦',
    }
  }
  const maximum = Math.max(...all.map((item) => item.score), 0)
  if (maximum <= 0) {
    return { value: null, relative: true, hint: '' }
  }
  return {
    value: clamp01(citation.score / maximum),
    relative: true,
    hint: '相对强度',
  }
}

/** 输入:引用 + 置信档。行为:选一个分数文案（没有分数就不编）。输出:显示字符串。 */
export function scoreText(citation: Citation, assessment: ConfidenceAssessment): string {
  if (assessment.basis === 'rerank' && citation.rerank_score !== null) {
    return citation.rerank_score.toFixed(3)
  }
  return citation.score.toFixed(3)
}

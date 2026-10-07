/**
 * 后端出参的 TypeScript 形状 —— 与 `src/project_rag/api/schemas.py` 一一对应。
 *
 * **为什么不从 OpenAPI 生成**:生成要跑一次代码生成、产物也要入库,而这一层现在只有
 * 十来个模型。真正需要它的时候是「接口数量 × 团队人数」变大以后(见 docs/frontend.md §5)。
 * 在那之前,这里的类型是**手写的镜像**,靠 `check_m7_frontend.py` 的契约盯着
 * 「字段名与 schemas.py 一致」—— 手写不等于可以漂。
 */

export type Role = 'employee' | 'admin'
export type RetrievalMode = 'vector' | 'keyword' | 'hybrid'
export type FusionMode = 'rrf' | 'weighted'
/**
 * 比后端的 Channel 多两个值,都是**前置拦截**(不是 FAQ 也不是知识库):
 * `greeting`(寒暄)/ `throttled`(触发限流,回一句"稍后再试"的兜底话术)。
 */
export type Channel = 'faq' | 'rag' | 'greeting' | 'throttled'

export interface ModelInfo {
  id: string
  label: string
  default: boolean
}

/** 服务端当前生效的采样参数 —— 前端**只读展示**。 */
export interface ModelSettings {
  temperature: number
  thinking: string
}

export interface RetrievalDefaults {
  mode: RetrievalMode
  fusion: FusionMode
  k: number
  max_context: number | null
  bm25_weight: number
  vector_weight: number
  rrf_k: number
  rerank: boolean
}

export interface RetrievalLimits {
  k_max: number
  max_context_max: number
  rrf_k_max: number
}

export interface RetrievalCapabilities {
  modes: RetrievalMode[]
  fusions: FusionMode[]
  defaults: RetrievalDefaults
  limits: RetrievalLimits
  rerank_available: boolean
}

/** `GET /api/v1/capabilities` —— 前端不许写死的东西全在这里。 */
export interface MetricsMinN {
  p50: number
  p95: number
  p99: number
}

/** 报表能力:窗口白名单 / 默认窗口 / 最慢榜上限 / 分位数的样本量门槛。 */
export interface MetricsCapabilities {
  windows: string[]
  default_window: string
  top_max: number
  min_n: MetricsMinN
}

export interface Capabilities {
  suffixes: string[]
  max_upload_mb: number
  models: ModelInfo[]
  model_settings: ModelSettings
  retrieval: RetrievalCapabilities
  metrics: MetricsCapabilities
  roles: Role[]
}

export interface LoginOut {
  token: string
  username: string
  role: Role
  expires_in: number
}

export interface Me {
  username: string
  role: Role
  is_admin: boolean
}

export interface Citation {
  index: number
  source: string
  doc_id: string
  preview: string
  score: number
  rerank_score: number | null
  /** None = 不知道(md/txt 或 P0-5 之前入库的老块)—— 显示「页码未知」,不许拿 0/1 顶。 */
  page: number | null
}

export interface HistoryTurn {
  question: string
  answer: string
}

export interface HistoryOut {
  session_id: string
  rounds: HistoryTurn[]
}

export interface SessionSummary {
  session_id: string
  title: string
  rounds: number
  created_at: string
  updated_at: string
}

export interface SessionsOut {
  sessions: SessionSummary[]
}

export interface DeletedOut {
  session_id: string
  deleted: number
}

/** 一次提问带的检索旋钮。`null` 一律表示「用服务端配置的默认值」。 */
export interface QueryRequest {
  query: string
  session_id?: string
  source_filter?: string | null
  k?: number
  // mode 与两个权重**不能同时给**(后端 422):模式已经决定了跑哪几路。
  mode?: RetrievalMode
  fusion?: FusionMode
  bm25_weight?: number
  vector_weight?: number
  rerank?: boolean
  max_context?: number
  rrf_k?: number
}

export interface QueryResponse {
  answer: string
  channel: Channel
  refused: boolean
  truncated: boolean
  citations: Citation[]
  faq_score: number | null
  session_id: string | null
}

/** 文档状态是闭集(与 `domain/documents.DOC_STATUSES` 一致)。 */
export type DocStatus = 'pending' | 'processing' | 'processed' | 'failed' | 'deleting'

export interface DocumentRecord {
  document_id: string
  source: string
  filename: string
  status: DocStatus
  chunks: number
  size_bytes: number
  created_at: string
  error: string | null
  /** 只有 `processed` 才是"能被检索到"。清单卡片上要显示这个,不是显示状态就完事。 */
  searchable: boolean
}

export interface DocumentsOut {
  documents: DocumentRecord[]
  /** 原文件落在哪 —— 排障时要知道去哪儿找(由后端告诉前端,不是前端猜)。 */
  uploads_dir: string
}

/** 删除结果:**后端把做了什么如实说清**(删了几块 / 文件在不在 / 登记行在不在)。 */
export interface DeletedDocument {
  document_id: string
  deleted_chunks: number
  removed_file: boolean
  removed_record: boolean
}

export interface ChunkPreview {
  index: number
  /** 这一段里第一个有页码的子块的页;null = 不知道(md/txt)。**不许拿 0 顶**。 */
  page: number | null
  /** 最后一个有页码的子块的页;与 page 不同 = 这一段跨页。 */
  last_page: number | null
  chars: number
  text: string
}

export interface DocumentPreview {
  document_id: string
  /** 折叠后的父块数(预览显示的单位)。 */
  parent_blocks: number
  /** 入库写进去的子块数(登记表的 chunks 同口径)。两个数不一样是正常的。 */
  child_chunks: number
  returned: number
  truncated: boolean
  blocks: ChunkPreview[]
}

/** 一个分段的分布。`n` 必须一起看:没有样本量的分位数读不出东西。 */
export interface SegmentStats {
  name: string
  n: number
  /** null = 样本不够(门槛见 `Capabilities.metrics.min_n`)—— **不是 0**,别折算。 */
  p50: number | null
  p95: number | null
  p99: number | null
  maximum: number | null
}

/** 最慢榜的一条。**含冷启动** —— 那是用户真实等到的。 */
export interface SlowRow {
  request_id: string
  channel: string
  cold: boolean
  total_ms: number
}

/** `GET /api/v1/admin/metrics` —— 一个时间窗的延迟汇总。 */
export interface MetricsOut {
  window: string
  rows: number
  cold_rows: number
  warm_rows: number
  refused: number
  truncated: number
  aborted: number
  faq_hits: number
  rag_answers: number
  segments: SegmentStats[]
  slowest: SlowRow[]
}

/** `/api/v1/stream` 的四种帧 —— 与 `api/app.py` 的 `_frame` 一一对应。 */
export type StreamFrame =
  | { type: 'sources'; citations: Citation[] }
  | { type: 'token'; text: string }
  | {
      type: 'done'
      answer: string
      channel: Channel
      refused: boolean
      truncated: boolean
      citations: Citation[]
      session_id: string | null
    }
  | { type: 'error'; detail: string }

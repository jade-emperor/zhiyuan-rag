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
/** 比后端的 Channel 多一个 greeting:寒暄是前置拦截,不是 FAQ 也不是知识库。 */
export type Channel = 'faq' | 'rag' | 'greeting'

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
export interface Capabilities {
  suffixes: string[]
  max_upload_mb: number
  models: ModelInfo[]
  model_settings: ModelSettings
  retrieval: RetrievalCapabilities
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

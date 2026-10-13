/**
 * 后端出参的 TypeScript 形状 —— 与 `src/project_rag/api/schemas.py` 一一对应。
 *
 * **为什么不从 OpenAPI 生成**:生成要跑一次代码生成、产物也要入库,而这一层现在只有
 * 十来个模型。真正需要它的时候是「接口数量 × 团队人数」变大以后(见 docs/frontend.md §5)。
 * 在那之前,这里的类型是**手写的镜像**,靠 `check_m7_frontend.py` 的契约盯着
 * 「字段名与 schemas.py 一致」—— 手写不等于可以漂。
 */

/** 三个角色(与 `domain/auth.py` 的 USER_ROLES 一一对应)。**前端不比较角色字符串** ——
 * 页签按 `/auth/me` 报的三个能力布尔值渲染(见 `Me`),加第四个角色时这里不用动。 */
export type Role = 'employee' | 'ops' | 'admin'
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

/**
 * `/api/v1/auth/me` —— 当前账号与**三个能力**。
 *
 * 三个布尔值是真源(后端 `domain/auth.py` 的三张表算出来的):前端按"能不能"渲染页签,
 * 而不是自己写 `role === 'ops' || role === 'admin'` —— 那种抄写会在加角色时静默漏掉一个页签。
 */
export interface Me {
  username: string
  role: Role
  is_admin: boolean
  can_view_reports: boolean
  can_use_knowledge: boolean
  can_manage: boolean
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

/**
 * 「这份文档现在到底怎么样」—— 清单卡片要显示的那一个字段(与 `domain/documents.DOC_STATES` 一致)。
 *
 * 它比 `status` 多出四个**不一致态**:登记表是一份没人失效的缓存(CLI 灌完不写它、
 * rm 一个文件不写它、drop 一个集合更不写它),所以"已入库"必须由**库里有没有块**说了算。
 * 非 `processed` 的行 `state` 原样等于 `status`(在途/失败的行,登记表就是真相)。
 */
export type DocState =
  | DocStatus
  | 'ok'
  | 'changed'
  | 'not_indexed'
  | 'file_missing'
  | 'unregistered'

export interface DocumentRecord {
  document_id: string
  source: string
  filename: string
  /** 登记表说的状态;`unregistered` = 磁盘上有、登记表没有(那种行不是"状态",是"没登记")。 */
  status: DocStatus | 'unregistered'
  /** 要显示的是这个,不是 `status` —— 两个字段分岔时 `state` 才是现实。 */
  state: DocState
  /** **登记表**记的块数(那次入库写进去多少)。 */
  chunks: number
  /** 登记时记下的字节数(0 = 当年没记过,判不了"文件改没改")。 */
  size_bytes: number
  /** 磁盘上现在的字节数(0 = 不在)。与 `size_bytes` 并排显示才看得出"变了没有"。 */
  disk_size_bytes: number
  on_disk: boolean
  created_at: string
  error: string | null
  /** **按库内实际块数算**:登记说 processed 不算数 —— 那正是"显示已入库却搜不到"的成因。 */
  searchable: boolean
  /** **向量库里**实际的块数(与 `chunks` 不是一个数:相等才是"对得上")。 */
  indexed_chunks: number
  /**
   * 这份文档有没有被收窄过(P0-6)。**谁看得见是另一回事** —— 清单已经按可见范围过滤过,
   * 员工看到的每一行他都能看;这个字段只用来标一句"这是限定的"。
   */
  restricted: boolean
}

/** 清单的一句话汇总(与逐行 `state` 同一个口径:后端数一遍,前端不再自己数)。 */
export interface DocumentSummary {
  total: number
  ok: number
  changed: number
  not_indexed: number
  file_missing: number
  unregistered: number
  /** 在途与失败(pending/processing/failed/deleting)—— 它们不是"对不上",是"还没完"。 */
  in_flight: number
}

export interface DocumentsOut {
  documents: DocumentRecord[]
  summary: DocumentSummary
  /** 原文件落在哪 —— 排障时要知道去哪儿找(由后端告诉前端,不是前端猜)。 */
  uploads_dir: string
  /** 语料**源材料**目录:清单里那些「未入库」的文件就在这儿(删除时不动这个目录)。 */
  corpus_dir: string
}

/** 删除结果:**后端把做了什么如实说清**(删了几块 / 文件在不在 / 登记行在不在)。 */
export interface DeletedDocument {
  document_id: string
  deleted_chunks: number
  removed_file: boolean
  removed_record: boolean
  /** true = 磁盘上那份是**语料源材料**,故意没动(删库不删源);与 `removed_file` 是两件事。 */
  kept_source: boolean
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

/** `GET /api/v1/reports/metrics` —— 一个时间窗的延迟汇总。 */
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

/**
 * 消耗报表里的一个模型 + **一个时段**(P0-7)。
 *
 * 同一个模型最多两行:`peak`(高峰)/ `off_peak`(空闲)—— provider 的空闲时段打折,
 * 单价差 2 倍,所以不能合成一行。
 *
 * `cached_input_tokens` 是输入里**命中 prompt cache** 的那部分(包含在 `input_tokens` 里,
 * 不是额外的);它的单价与未命中差 **50 倍**(0.02 vs 1 元/百万)。
 * `total_cost` 为 `null` = **这个模型没配单价**(不是花了 0 元)。
 */
export interface CostLine {
  model: string
  period: string
  calls: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  total_cost: number | null
}

/** 一个时段的合计(与 provider 的价目表一行行对得上)。 */
export interface PeriodTotal {
  period: string
  calls: number
  input_tokens: number
  output_tokens: number
  total_cost: number
}

/**
 * `GET /api/v1/reports/cost` —— 一个时间窗的消耗。
 *
 * `all_priced` 与四个"缺口"计数必须一起读,`total_cost` 才是可解释的:
 * `unmetered_calls`(没量到 token)、`period_unknown_calls`(读不到发生时间 → 按高峰计)、
 * `cache_unknown_calls`(没回报缓存命中数 → 整段按未命中价计)、`unpriced_models`(没配单价)。
 */
export interface CostOut {
  window: string
  currency: string
  metered_calls: number
  unmetered_calls: number
  period_unknown_calls: number
  cache_unknown_calls: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  total_tokens: number
  total_cost: number
  all_priced: boolean
  unpriced_models: string[]
  periods: PeriodTotal[]
  lines: CostLine[]
}

/** 账号列表里的一行(管理员页)。**不含密码或它的哈希**。 */
export interface UserOut {
  user_id: string
  username: string
  role: Role
  is_admin: boolean
  /** 他所属的组(哪几类员工)。 */
  group_ids: string[]
}

export interface UsersOut {
  users: UserOut[]
}

/** 建号结果。**不回显密码**(它不是"确认",是"记下来给谁看"的问题)。 */
export interface CreateUserOut {
  user_id: string
  username: string
  role: Role
}

/** 删除结果(账号 / 组通用):`deleted=false` = 本来就没有(删除是幂等的)。 */
export interface DeletedSubjectOut {
  deleted: boolean
}

/** 一个组(一类员工)。`member_ids` 是成员的用户 ID。 */
export interface GroupOut {
  group_id: string
  name: string
  member_ids: string[]
}

export interface GroupsOut {
  groups: GroupOut[]
}

/**
 * 一份文档的可见范围。
 *
 * `restricted=false` 且两个名单都空 = **没有规则 = 人人可见**(默认状态)。
 * 两个名单都空地 PUT 回去 = 取消限制(不是"谁都看不到")。
 */
export interface DocumentAclOut {
  document_id: string
  restricted: boolean
  user_ids: string[]
  group_ids: string[]
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

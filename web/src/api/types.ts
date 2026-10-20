/**
 * 后端出参的 TypeScript 形状 —— 与 `src/project_rag/api/schemas.py` 一一对应。
 *
 * **为什么不从 OpenAPI 生成**:生成要跑一次代码生成、产物也要入库,而这一层现在只有
 * 十来个模型。真正需要它的时候是「接口数量 × 团队人数」变大以后。
 * 在那之前,这里的类型是**手写的镜像**,靠一条契约盯着「字段名与 schemas.py 一致」——
 * 手写不等于可以漂。
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
  registration: RegistrationCapabilities
}

/** 自助注册的可用性(前端据此决定显示还是隐藏注册入口)。 */
export interface RegistrationCapabilities {
  enabled: boolean
  captcha_required: boolean
  invite_required: boolean
  min_password_chars: number
}

/** 一张图形验证码。`image` 是 data URI,直接给 `<img src>`。 */
export interface CaptchaOut {
  ticket: string
  image: string
  expires_in: number
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

export interface CitationImage {
  /** 稳定身份:图片字节的 SHA-256(OSS 对象 key 里的那一段)。 */
  key: string
  /** 本次响应现签的临时 URL;空串 = 这次签不出来,界面按"暂时取不到"处理。 */
  url: string
}

export interface Citation {
  index: number
  source: string
  doc_id: string
  preview: string
  score: number
  rerank_score: number | null
  /** None = 不知道(md/txt 没有页码,早期入库的老块也可能没有)—— 显示「页码未知」,不许拿 0/1 顶。 */
  page: number | null
  /** 这一条引用所属父块里的图;空数组 = 没有图/老数据/图片功能没开。 */
  images: CitationImage[]
}

export interface HistoryTurn {
  question: string
  answer: string
  /**
   * 当轮的引用卡片(快照:序号 / 来源 / 页码 / 分数 / 预览)。
   *
   * 老账(那时还没存引用)给空数组 —— 界面按"这一轮的出处没留下"处理,而不是假装没有出处。
   */
  citations: Citation[]
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
  /** 归属人的登录名。`scope=mine` 时是空串，`scope=all` 时才有值。 */
  owner: string
  /** 是不是当前账号的会话（后端算好，前端不比较字符串）。 */
  mine: boolean
}

export interface SessionsOut {
  sessions: SessionSummary[]
  /** 这次问的是哪一档：`mine` / `all`。 */
  scope: string
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
  /**
   * 归在哪一级目录下(`folder_id`;空串 = 未分类)。
   *
   * **未登记的文件必然是未分类**:归属要写库,而"未登记"正是"没有行可写"。
   */
  folder: string
  /** **按库内实际块数算**:登记说 processed 不算数 —— 那正是"显示已入库却搜不到"的成因。 */
  searchable: boolean
  /** **向量库里**实际的块数(与 `chunks` 不是一个数:相等才是"对得上")。 */
  indexed_chunks: number
  /**
   * 这份文档有没有被收窄过。**谁看得见是另一回事** —— 清单已经按可见范围过滤过,
   * 员工看到的每一行他都能看;这个字段只用来标一句"这是限定的"。
   */
  restricted: boolean
}

/**
 * 一级目录(知识库的第一层)。**只有两层**,所以它没有 `parent_id`。
 *
 * `documents` / `chunks` / `size_bytes` 都是从**清单那一份行**聚合出来的
 * (`chunks` 是库内实际块数)—— 目录卡片与点进去看到的行必须是同一批数。
 */
export interface FolderRecord {
  folder_id: string
  name: string
  description: string
  created_at: string
  updated_at: string
  documents: number
  chunks: number
  size_bytes: number
  /** 需要处理的份数(三处不一致 + 在途/失败)。**不是"错误数"**:在途的也算。 */
  issues: number
  restricted: number
  /** 目录里最新一份文档的登记时间(空串 = 还没有文档)。 */
  last_activity: string
  /** 是不是"未分类"那个虚拟目录(它的 `folder_id` 是 `unfiled`)。 */
  unfiled: boolean
}

/** 目录页汇总。与 `DocumentSummary` 分开:那个数**状态**,这个数**组织**。 */
export interface FolderSummary {
  folders: number
  used: number
  unfiled: number
}

export interface FoldersOut {
  folders: FolderRecord[]
  /** 未分类那一格**恒定给出**(哪怕 0 份):前端少一支"有没有它"的分支。 */
  unfiled: FolderRecord
  summary: FolderSummary
}

/** 二级页面:目录 + 里面的文档 + 这份清单的汇总(一次往返)。 */
export interface FolderDetail {
  folder: FolderRecord
  documents: DocumentRecord[]
  summary: DocumentSummary
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

/**
 * 一个冷热分层里"用户拿到了什么"的计数。`rows` 是这一层的分母,要一起读。
 *
 * 冷启动与热启动各一份,**没有合计栏** —— 两层的比率不可加,要加总必须自己写下来。
 */
export interface BehaviorCounts {
  rows: number
  refused: number
  truncated: number
  aborted: number
  faq_hits: number
  rag_answers: number
}

/**
 * `GET /api/v1/reports/metrics` —— 一个时间窗的延迟汇总。
 *
 * `rows`/`cold_rows`/`warm_rows` 是"这一窗收了几次请求"(一次请求一行账);
 * 行为口径在 `warm`/`cold` 里 —— 冷启动不混进拒答率与通道计数。
 */
export interface MetricsOut {
  window: string
  rows: number
  cold_rows: number
  warm_rows: number
  warm: BehaviorCounts
  cold: BehaviorCounts
  segments: SegmentStats[]
  slowest: SlowRow[]
}

/**
 * 消耗报表里的一个模型 + **一个时段**。
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
 * `all_priced` 与六个"缺口"计数必须一起读,`total_cost` 才是可解释的:
 * `unmetered_calls`(没量到 token)、`period_unknown_calls`(读不到发生时间 → 按高峰计)、
 * `zone_unknown_calls`(时间戳没带时区 → 按高峰计)、
 * `calendar_unknown_calls`(那一年没填节假日 → 按高峰计)、
 * `cache_unknown_calls`(没回报缓存命中数 → 整段按未命中价计)、`unpriced_models`(没配单价)。
 */
export interface CostOut {
  window: string
  currency: string
  metered_calls: number
  unmetered_calls: number
  period_unknown_calls: number
  zone_unknown_calls: number
  calendar_unknown_calls: number
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
  /** 高峰是星期几(ISO:1=周一 … 7=周日);空数组 = 不按星期过滤。 */
  peak_weekdays: number[]
  /** 高峰钟点窗口(`["09:00","12:00"]`,左闭右开);空 = 这个部署不分时段。 */
  peak_windows: string[][]
  /** 法定节假日表覆盖了哪些年(界面据此说明"为什么这一行算空闲")。 */
  holiday_years: number[]
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
/**
 * 一份文档在**某一个组**眼里的样子(组侧授权页的三态):
 *
 * - `open`    —— 没有规则 = **人人可见**(本组本来就能看;这里禁不掉它 —— 要禁得先收窄文档);
 * - `allowed` —— 有规则,而且本组在名单里;
 * - `denied`  —— 有规则,但本组不在名单里。
 */
export type GroupDocumentVisibility = 'open' | 'allowed' | 'denied'

export interface GroupDocument {
  document_id: string
  filename: string
  visibility: GroupDocumentVisibility
  /** 除本组外还授权给了几个对象。取消本组时它是 0 ⇒ 这份文档会回到人人可见。 */
  other_subjects: number
}

/** `GET/PUT /api/v1/admin/groups/{id}/documents` 的出参(PUT 会带"改了什么")。 */
export interface GroupDocumentsOut {
  group_id: string
  name: string
  documents: GroupDocument[]
  granted: string[]
  revoked: string[]
  /** 取消授权后名单变空、**回到了人人可见**的那几份(最该被管理员看见的一栏)。 */
  opened: string[]
  notice: string
}

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

// ── FAQ 管理台 ────────────────────────────────────────────────────────────────
/** 一条问答对。`id` 是库里的自增主键(用它改/删,不用问题文本当标识)。 */
export interface FaqEntry {
  id: number
  question: string
  answer: string
}

/** `GET /api/v1/admin/faq` 的出参。`total` 是**过滤前**的总数。 */
export interface FaqListOut {
  entries: FaqEntry[]
  total: number
}

export interface DeletedFaqOut {
  entry_id: number
  /** 0 = 本来就没有这条(删除是幂等的)。 */
  deleted: number
}

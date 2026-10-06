"""
HTTP / WebSocket 的请求与响应模型(pydantic)。

**为什么模型放 api 而不是 domain**:domain 是纯 dataclass、不依赖校验框架(它的 docstring
就是这么写的);而这一层的职责恰好是「把外面的 JSON 变成合法的调用参数」—— 那是 pydantic 的活。
转换只发生在这一层,和 cli 只做 domain -> 文本是同一个分工。

``answer`` 不给默认值、也不允许 None:答不出来时返回 ``null`` 会让前端多写一支分支;
这里拒答也是**一句人话**(拒答话术),前端永远拿到字符串。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from project_rag.domain import RETRIEVAL_MODES

# 三个上限**只在这里写一次**:出参模型的 ``le=`` 与 ``GET /api/v1/capabilities``
# 报给前端的 limits 用的是同一批常量。分两处写的话,前端的滑条迟早能拖到一个
# 服务端会 422 的值 —— 用户看到的是"界面允许但后端拒绝"。
K_MAX = 50
MAX_CONTEXT_MAX = 20
RRF_K_MAX = 1000


class QueryRequest(BaseModel):
    """问答请求(HTTP body 与 WebSocket 的一条消息共用)。

    M7.1 起**检索旋钮也在这里**:前端右侧那个参数面板的每个控件都得有个 HTTP 落点,
    否则"界面上能调"就是假的。规则只有一条 —— **请求里给了就用请求的,没给就用配置文件里的**
    (默认值仍然只有一处真值来源:``config.yaml``)。
    """

    query: str = Field(min_length=1, description="用户问题")
    session_id: str | None = Field(default=None, description="会话 ID;给了就带对话历史")
    source_filter: str | None = Field(
        default=None, description="按来源过滤(如 bagu_ai / bagu_java)"
    )
    k: int | None = Field(
        default=None, ge=1, le=K_MAX, description="每一路各召回多少条;默认取配置"
    )

    # ── 检索旋钮(M7.1) ───────────────────────────────────────────────────────
    mode: Literal["vector", "keyword", "hybrid"] | None = Field(
        default=None, description="检索方式;hybrid = 两路都跑(None = 配置默认)"
    )
    fusion: Literal["rrf", "weighted"] | None = Field(
        default=None, description="融合方式;weighted = 各自 min-max 归一化后加权"
    )
    bm25_weight: float | None = Field(
        default=None, ge=0.0, le=1.0, description="关键字那一路的权重;0 = 关掉这一路"
    )
    vector_weight: float | None = Field(
        default=None, ge=0.0, le=1.0, description="向量那一路的权重;0 = 关掉这一路"
    )
    rerank: bool | None = Field(default=None, description="要不要重排;默认取配置")
    max_context: int | None = Field(
        default=None, ge=1, le=MAX_CONTEXT_MAX, description="最多几条父块进上下文"
    )
    rrf_k: int | None = Field(
        default=None, ge=1, le=RRF_K_MAX, description="RRF 的平滑常数;默认取配置"
    )

    @model_validator(mode="after")
    def _reject_conflicting_knobs(self) -> QueryRequest:
        """输入:无(校验自己)。行为:拦下"两个真值来源"和"两路全关"。输出:self。

        两条都是**静默错法的预防**,不是洁癖:

        * ``mode`` 与权重不能同时给 —— ``mode=keyword`` 配上 ``vector_weight=0.9``
          是两个互相打架的指令,"谁赢"得写进文档才猜得出来,不如当场 422;
        * 两路权重不能同时 ≤ 0 —— 那是一次"什么都不检索"的请求,结果看起来就是
          "知识库里没有",而真实原因是参数写错了。参数错必须当场可见(法律 32)。
        """
        if self.mode is not None and (
            self.bm25_weight is not None or self.vector_weight is not None
        ):
            raise ValueError(
                "mode 与 bm25_weight/vector_weight 不能同时给:模式已经决定跑哪几路"
            )
        if (
            self.bm25_weight is not None
            and self.vector_weight is not None
            and self.bm25_weight <= 0
            and self.vector_weight <= 0
        ):
            raise ValueError("bm25_weight 与 vector_weight 不能同时为 0:那等于两路都不检索")
        return self


class CitationOut(BaseModel):
    """引用清单里的一条(给人核对答案出处)。

    ``page`` 是 P0-5 加的:PDF 的引用要能直接告诉用户"翻到第几页"。
    **None = 不知道**(md/txt 没有页码概念,或者是 P0-5 之前入库的老块),
    前端按 ``null`` 显示"页码未知" —— 不许拿 0 或 1 顶上,那是在编。
    """

    index: int
    source: str
    doc_id: str
    preview: str
    score: float
    rerank_score: float | None = None
    page: int | None = None


class QueryResponse(BaseModel):
    """非流式回答。

    ``channel`` 比 ``domain.Channel`` 多一个 ``"greeting"``:寒暄是这一层的前置拦截,
    既不是 FAQ 通道也不是知识库通道 —— 混进那两种会让前端没法区分。
    """

    answer: str
    channel: Literal["faq", "rag", "greeting"]
    refused: bool = False
    truncated: bool = False
    citations: list[CitationOut] = []
    faq_score: float | None = None
    session_id: str | None = None


class SessionOut(BaseModel):
    session_id: str


class HistoryTurnOut(BaseModel):
    question: str
    answer: str


class HistoryOut(BaseModel):
    session_id: str
    rounds: list[HistoryTurnOut]


class DeletedOut(BaseModel):
    session_id: str
    deleted: int


class HealthOut(BaseModel):
    status: str


class SourcesOut(BaseModel):
    sources: list[str]


# ── 观测报表(P0-2) ──────────────────────────────────────────────────────────
# 形状与 `application/metrics.py` 的 dataclass 一一对应,**口径不在这一层**:
# 这一层只负责把已经算好的东西序列化出去(所以这里没有 min_n / 分位数算法)。
class SegmentOut(BaseModel):
    """一个分段的分布。``n`` 必须一起给:没有样本量的分位数读不出东西。"""

    name: str
    n: int
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    maximum: float | None = None


class SlowRowOut(BaseModel):
    """最慢榜的一条(**含冷启动** —— 那是用户真实等到的)。"""

    request_id: str
    channel: str
    cold: bool
    total_ms: float


class MetricsOut(BaseModel):
    """一个时间窗的延迟汇总。所有比率的分子都在字段里,分母是 ``rows``。"""

    window: str
    rows: int
    cold_rows: int
    warm_rows: int
    refused: int
    truncated: int
    aborted: int
    faq_hits: int
    rag_answers: int
    segments: list[SegmentOut]
    slowest: list[SlowRowOut]


# ── 认证(P0-3a) ──────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    """登录请求。**没有注册接口** —— 建号走 `project-rag user add`(见 cli/commands/user.py)。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    """登录成功。``token`` 只在这一次响应里出现(库里存的是它的哈希)。"""

    token: str
    username: str
    role: str
    expires_in: int


class MeOut(BaseModel):
    """当前账号。用来让前端判断"该不该显示上传/删除/报表"。"""

    username: str
    role: str
    is_admin: bool


# ── 文档治理(P0-3b) ──────────────────────────────────────────────────────────
class DocumentOut(BaseModel):
    """一份已登记文档(前端卡片要显示的东西)。"""

    document_id: str
    source: str
    filename: str
    status: str
    chunks: int
    size_bytes: int
    created_at: str
    error: str | None = None
    searchable: bool


class DocumentsOut(BaseModel):
    """文档清单(新→旧)。``uploads_dir`` 一并给出:排障时要知道文件落在哪。"""

    documents: list[DocumentOut]
    uploads_dir: str


class DeletedDocumentOut(BaseModel):
    """删除结果:**把做了什么如实说清**(删了几块 / 文件在不在 / 登记行在不在)。"""

    document_id: str
    deleted_chunks: int
    removed_file: bool
    removed_record: bool


class CapabilitiesOut(BaseModel):
    """后端能吃什么、吃多大、会什么 —— **前端不许写死这些**。

    上传框的 ``accept`` 来自 ``suffixes``;右侧参数面板的选项与上限来自 ``retrieval``;
    模型下拉来自 ``models``。写死在前端的话,前后端迟早各说一套:前端放行一个后端不认的后缀,
    或者滑条能拖到一个后端会 422 的值 —— 用户看到的是"界面允许但后端拒绝"。
    这个接口不需要登录(它不含任何数据)。
    """

    suffixes: list[str]
    max_upload_mb: int
    models: list[ModelOut]
    model_settings: ModelSettingsOut
    retrieval: RetrievalOut
    roles: list[str]


class ModelOut(BaseModel):
    """一个可选模型。**M7.1 只有一个**:下拉框是"预留接口",不是"能切"。"""

    id: str
    label: str
    default: bool = False


class ModelSettingsOut(BaseModel):
    """服务端当前生效的采样参数 —— **只读展示**。

    为什么不做成可调的:``temperature`` / ``thinking`` 现在是**启动期配置**,
    它们进延迟账的口径(``thinking`` 那一列)与"这次请求用什么"必须一致;
    开放 per-request 会让账本里那一列说不清是哪一档。前端如实显示,
    想改走 ``config.yaml``(触发条件见 ``docs/frontend.md`` §5)。
    """

    temperature: float
    thinking: str


class RetrievalDefaultsOut(BaseModel):
    """当前配置里生效的检索默认值(前端"恢复默认"按钮就恢复成这些)。"""

    mode: str
    fusion: str
    k: int
    max_context: int | None
    bm25_weight: float
    vector_weight: float
    rrf_k: int
    rerank: bool


class RetrievalLimitsOut(BaseModel):
    """数值旋钮的上下限 —— 与 ``QueryRequest`` 的 ``Field(ge=/le=)`` 是同一批常量。"""

    k_max: int
    max_context_max: int
    rrf_k_max: int


class RetrievalOut(BaseModel):
    """检索能力的自述(方式 / 融合 / 默认值 / 上限 / 重排能不能用)。"""

    modes: list[str]
    fusions: list[str]
    defaults: RetrievalDefaultsOut
    limits: RetrievalLimitsOut
    rerank_available: bool


class SessionSummaryOut(BaseModel):
    """会话列表里的一条。``title`` 由后端裁好(首问前 40 字),前端不再截第二次。"""

    session_id: str
    title: str
    rounds: int
    created_at: str
    updated_at: str


class SessionsOut(BaseModel):
    """我名下的会话(最近活动在前)。**只列有轮次的** —— 刚建没问过的不占地方。"""

    sessions: list[SessionSummaryOut]

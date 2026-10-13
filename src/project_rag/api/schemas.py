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
# 报表的「最慢榜取几条」上限。**必须与 capabilities 报给前端的 top_max 同源** ——
# 否则前端的滑条能拖到一个后端 422 的值(M7.1 那条 limits 契约的同一条规矩)。
TOP_MAX = 50


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

    ``channel`` 比 ``domain.Channel`` 多两个值,都是**这一层的前置拦截**:

    * ``"greeting"`` —— 寒暄,直接回话术,不查库;
    * ``"throttled"`` —— 触发限流,回一句"稍后再试"的兜底话术(M7.4)。

    它们既不是 FAQ 通道也不是知识库通道,混进那两种前端就没法区分
    ("这句话为什么没有引用"与"这句话为什么长得像错误")。
    """

    answer: str
    channel: Literal["faq", "rag", "greeting", "throttled"]
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


# ── 消耗报表(P0-7) ───────────────────────────────────────────────────────────
# 与 ``application/cost.py`` 的 dataclass 一一对应,口径不在这一层。
class CostLineOut(BaseModel):
    """一个模型 + **一个时段**的消耗。

    ``period`` 是 ``peak`` / ``off_peak``(provider 的空闲时段打折,单价差 2 倍),
    所以同一个模型最多两行;``total_cost`` 为 ``null`` = **这个模型没配单价**(不是花了 0 元)。

    ``cached_input_tokens`` 是输入里命中 **prompt cache** 的那部分 —— 它包含在
    ``input_tokens`` 里(不是额外的),而它的单价与未命中差 **50 倍**
    (0.02 vs 1 元/百万),所以必须单独给出来。
    """

    model: str
    period: str
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_cost: float | None = None


class PeriodTotalOut(BaseModel):
    """一个时段的合计(与 provider 的价目表一行行对得上)。"""

    period: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_cost: float


class CostOut(BaseModel):
    """一个时间窗的消耗汇总。

    ``all_priced`` 与六个"缺口"计数必须一起读,``total_cost`` 才是可解释的:

    * ``unmetered_calls`` —— 调了模型但没量到 token(provider 没回报 / 那一轮断了 / 老账),**没算进金额**;
    * ``period_unknown_calls`` —— 读不到账本时间戳,按**高峰**价计(偏保守,不会少报);
    * ``zone_unknown_calls`` —— 时间戳**没带时区**(老账或后端镜像还没重建),判不出高峰/空闲,同样按**高峰**价计;
    * ``calendar_unknown_calls`` —— 那一年的法定节假日还没填进配置,按**高峰**价计;
    * ``cache_unknown_calls`` —— 没回报缓存命中数,整段输入按**未命中**价计(这是上界);
    * ``unpriced_models`` —— 没配单价的模型,它们**不在**金额里。
    """

    window: str
    currency: str
    metered_calls: int
    unmetered_calls: int
    period_unknown_calls: int
    zone_unknown_calls: int
    calendar_unknown_calls: int
    cache_unknown_calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float
    all_priced: bool
    unpriced_models: list[str]
    periods: list[PeriodTotalOut]
    lines: list[CostLineOut]
    # **时段口径本身**(来自 config,不是界面自己写的一句话):哪几天、哪几个钟点是高峰,
    # 以及节假日表覆盖了哪些年。报表把"为什么这一行算空闲"讲清楚,否则读者只能猜;
    # 而口径放在这一份出参里,界面就不必再抄一遍(抄的那一份迟早与配置不一致)。
    peak_weekdays: list[int]
    peak_windows: list[list[str]]
    holiday_years: list[int]


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


class LogoutOut(BaseModel):
    """退出结果。``revoked`` = 服务端**真的**把那枚令牌撤掉了(不只是前端删了本地副本)。"""

    revoked: bool


class MeOut(BaseModel):
    """当前账号。用来让前端判断"该不该显示上传/删除/报表"。

    **三个能力布尔值都在这里**(不只是 ``is_admin``):前端要按它们决定显示哪些页签,
    而"哪些角色能看报表"这件事的真源是 ``domain/auth.py`` 的三张表 ——
    前端自己写 ``role === 'ops' || role === 'admin'`` 就是第二份真源,
    哪天加了第四个角色,前端会静默地少一个页签(不报错,只是没人看得到)。
    """

    username: str
    role: str
    is_admin: bool
    can_view_reports: bool
    can_use_knowledge: bool
    can_manage: bool


# ── 文档治理(P0-3b) ──────────────────────────────────────────────────────────
class DocumentOut(BaseModel):
    """清单上的一行:**登记表说的** + **磁盘上的** + **库里实际有的**。

    ``state`` 是这一行要显示的那一个字段(闭集见 ``domain/documents.DOC_STATES``):
    登记表是一份没人失效的缓存(CLI 灌完不写它、rm 一个文件不写它、drop 集合更不写它),
    所以"已入库"这三个字必须由**库里有没有块**说了算。两个块数各有各的名字:
    ``chunks`` 是登记表记的,``indexed_chunks`` 是库里实际的 —— 只给一个数,
    用户没法知道该信谁。
    """

    document_id: str
    source: str
    filename: str
    # 登记表说的状态;"unregistered" = 磁盘上有、登记表没有(那一行不存在,不是状态)
    status: str
    state: str
    chunks: int
    size_bytes: int  # 登记时记下的字节数(0 = 当年没记过,判不了"文件改没改")
    disk_size_bytes: int  # 磁盘上现在的字节数(0 = 不在)
    on_disk: bool
    created_at: str
    error: str | None = None
    # **按库内实际块数算**:登记表说 processed 不算数 —— 那正是"显示已入库却搜不到"的成因。
    searchable: bool
    indexed_chunks: int
    # 这份文档有没有被收窄过(P0-6)。**谁看得见是另一回事**:清单已经按可见范围过滤过,
    # 员工看到的每一行他都能看;这个字段只用来在界面上标一句"这是限定的"。
    restricted: bool = False


class DocumentSummaryOut(BaseModel):
    """清单的一句话汇总(与逐行 ``state`` 同一个口径:数一遍,不另算一套)。"""

    total: int
    ok: int
    changed: int
    not_indexed: int
    file_missing: int
    unregistered: int
    in_flight: int


class DocumentsOut(BaseModel):
    """文档清单(登记的行新→旧,未登记的文件按 doc_id 排在后面)。

    两个目录一并给出:排障时要知道"磁盘上那几处"分别在哪(前端只显示,不做路径拼接)。
    """

    documents: list[DocumentOut]
    summary: DocumentSummaryOut
    uploads_dir: str
    corpus_dir: str


class DeletedDocumentOut(BaseModel):
    """删除结果:**把做了什么如实说清**(删了几块 / 文件在不在 / 登记行在不在)。

    ``kept_source`` 与 ``removed_file`` 是两种不同的"文件还在":前者是**故意**没动
    (语料目录是源材料,删库不删源),后者是本来就不在。合成一句话会让用户以为删干净了。
    """

    document_id: str
    deleted_chunks: int
    removed_file: bool
    removed_record: bool
    kept_source: bool = False


class ChunkPreviewOut(BaseModel):
    """预览里的一段 —— **父块**(模型真正读到的那段),不是检索命中的子块。

    ``page`` 是这一段里第一个有页码的子块的页,``last_page`` 是最后一个:
    两者不同说明这一段**跨页**(真语料里约 22.9% 的块跨页,口径见 P0-5);
    都是 ``None`` 就是"不知道"(md/txt)。**不许拿 0 顶**。
    """

    index: int
    page: int | None = None
    last_page: int | None = None
    chars: int
    text: str


class DocumentPreviewOut(BaseModel):
    """一份文档在向量库里的样子(前端「预览」按钮要的东西)。

    ``parent_blocks`` 与 ``child_chunks`` 是两个数:入库写的是**子块**,
    预览显示的是折叠后的**父块**,两者差着切分比例。前端两个都显示,
    用户才不会把"入库 42 块、预览 15 段"读成丢数据。
    """

    document_id: str
    parent_blocks: int
    child_chunks: int
    returned: int
    truncated: bool
    blocks: list[ChunkPreviewOut]


class MetricsMinNOut(BaseModel):
    """分位数的最小样本量(**渲染成「-」的门槛**)。

    前端要拿它解释"为什么这一格是 `-`" —— 说实话比画一个好看的 0 重要:
    n=12 的时候 p95 根本没有含义(见 `application/metrics.py` 的口径 2)。
    """

    p50: int
    p95: int
    p99: int


class MetricsCapabilitiesOut(BaseModel):
    """报表能力:窗口白名单 / 默认窗口 / 最慢榜上限 / 分位数的样本量门槛。"""

    windows: list[str]
    default_window: str
    top_max: int
    min_n: MetricsMinNOut


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
    metrics: MetricsCapabilitiesOut
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


# ── 账号、分组与文档可见范围(P0-6) ────────────────────────────────────────────
# 三条口径先写在这里,免得读接口的人以为它们是"随便定的":
#
# 1. **密码只进不出**:任何出参里都没有密码或它的哈希,``user add`` 那一份也不回显;
# 2. **删号不能删自己、不能删最后一个管理员**(守卫在 ``application/accounts.py``,
#    接口层只把 ``ValueError`` 翻成 400);
# 3. **可见范围只用来收窄**:一份文档没有任何授权对象 = 人人可见(``restricted`` 为 false)。
#    所以"取消限制"就是把两个名单都清空,不是"设成只有管理员能看"。
class UserOut(BaseModel):
    """账号列表里的一行。``group_ids`` 是他所属的组(**用来在界面上显示"哪一类人"**)。"""

    user_id: str
    username: str
    role: str
    is_admin: bool
    group_ids: list[str] = []


class UsersOut(BaseModel):
    """全部账号(用户名升序)。只有名字与角色 —— 没有密码,也没有登录时间之类的推测字段。"""

    users: list[UserOut]


class CreateUserRequest(BaseModel):
    """建号请求(管理员专用)。角色必须是 ``USER_ROLES`` 里的一个(校验在用例层,消息给人看)。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default="employee", description="employee / ops / admin")


class CreateUserOut(BaseModel):
    """建号结果。**不回显密码**(它不是"确认",是"记下来给谁看"的问题)。"""

    user_id: str
    username: str
    role: str


class ResetPasswordRequest(BaseModel):
    """改密码请求(管理员替某个账号改)。"""

    password: str = Field(min_length=1, max_length=256)


class DeletedUserOut(BaseModel):
    """删号结果。``username`` 一起回:界面上的提示语要说清"删掉的是谁"。"""

    user_id: str
    username: str
    deleted: bool


class GroupOut(BaseModel):
    """一个组(一类员工):稳定 ID + 名字 + 成员 ID。"""

    group_id: str
    name: str
    member_ids: list[str] = []


class GroupsOut(BaseModel):
    """全部组(名字升序)。前端拿它渲染"哪一类员工"的下拉与勾选框。"""

    groups: list[GroupOut]


class CreateGroupRequest(BaseModel):
    """建组请求。名字是管理员唯一认得的标识,所以重名要报错(不覆盖)。"""

    name: str = Field(min_length=1, max_length=64)


class SetMembersRequest(BaseModel):
    """**整份**成员名单(声明式):前端勾完点保存,语义就是"这些人"。"""

    user_ids: list[str] = []


class DeletedGroupOut(BaseModel):
    """删组结果。``deleted=false`` = 本来就没有这个组(删除是幂等的)。"""

    group_id: str
    deleted: bool


class DocumentAclOut(BaseModel):
    """一份文档的可见范围。

    ``restricted=false`` 且两个名单都空 = **没有规则 = 人人可见**。
    这个布尔是**算出来的**(名单非空即为真),不是库里另存的一个字段 ——
    否则会出现"布尔说限制了、名单却是空的"这种自相矛盾的状态。
    """

    document_id: str
    restricted: bool
    user_ids: list[str]
    group_ids: list[str]


class SetDocumentAclRequest(BaseModel):
    """设置可见范围(**整份**覆盖)。两个名单都空 = 取消限制(回到人人可见)。"""

    user_ids: list[str] = []
    group_ids: list[str] = []

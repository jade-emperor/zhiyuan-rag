"""领域模型:纯数据,不依赖任何第三方 SDK。这里只做了自定义数据容器

domain 是依赖图的叶子 —— 这个包不认识 ports / adapters / cli 里的任何东西。
判断一个类型该不该放这里,问一句:如果明天把所有 LLM 端口都删掉,它还在不在?
ChatMessage 在(会话历史、上下文拼装、评估用例都要用它),所以它属于 domain。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]

Source = Literal["faq", "llm"]

# 合流通道的「这条答案走了哪条通道」。与 Source 刻意分开:Source 说的是
# 「这条答案是谁给的」(人工维护的标准答案 / 模型),Channel 说的是「走了哪条检索通道」——
# 合流之后这两个问题不再等价(知识库通道的答案也是模型生成的)。
Channel = Literal["faq", "rag"]


# 这个装饰器,frozen=Tru参数表示这个类构建的对象不可变,不可动态添加属性.slots=True表示优化缓存
@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息。

    只用 dataclass,不用 pydantic:domain 不该依赖校验/序列化框架,
    这样它可以在任何地方被实例化、比较、当字典键,不需要框架参与。
    HTTP 接口返回它时,domain -> schema 的转换在 api 层做(见 ``api/schemas.py``)。

    也不要在这里加 to_langchain() 之类的方法 —— 那会把 langchain 拉进 domain。
    转换是适配器的职责(见 adapters/llm_deepseek.py 的 to_sdk_messages)。
    """

    # 声明Role只能是system、user、assistant三种字面量
    role: Role
    content: str
    # 多模态消息附带的图片 URL(按顺序附在文字之后)。空元组 = 纯文本,
    # 适配器必须走原来的纯字符串 content,不能凭空把消息变成列表。
    # URL 由调用方现签(短时有效),**不在 domain 里签** —— 领域层不认识 OSS。
    images: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    """**当轮那张引用卡片**的快照:序号、来源、页码、两个分数、预览。

    **为什么不是 ``Citation``**:``Citation`` 带着整个 ``Chunk``(含父块正文),
    而历史要留的是"用户当时看到的那张卡片"。存快照而不是只存 ``chunk_id`` 有两条理由:
    ① 语料后来重灌或删了,旧会话仍然显示得出来;② 回看历史不该再去向量库查一次
    (那是一次昂贵往返,而且结果可能已经不同)。

    ``page`` 与实时那份同一条口径:**None = 不知道**(md/txt 没有页码、老块也没记),
    不许拿 0/1 顶上。

    ``image_keys`` 同理:存 key 不存签名 URL —— 历史回看时 URL 早就过期了,
    而 key 永远能用(只要对象还在,现签一次即可)。
    """

    index: int
    source: str
    doc_id: str
    preview: str
    score: float
    rerank_score: float | None = None
    page: int | None = None
    image_keys: tuple[str, ...] = ()


# 引用预览留多少字。**只在这里定义一次**:实时那份(接口出参)与历史那份(落库快照)
# 用的是同一个数 —— 两处各写一个 120,迟早一边改了另一边没改,而用户会看到
# "同一条引用,刚问完和历史里长得不一样"。
PREVIEW_CHARS = 120


def snapshot_citations(citations: Sequence[Citation]) -> tuple[CitationSnapshot, ...]:
    """输入:这一轮的引用序列。行为:按**给人看的样子**拍一份快照(截断预览)。输出:快照序列。

    实时出参与落库历史都走它 —— 一处口径,两个消费者。
    """
    return tuple(
        CitationSnapshot(
            index=citation.index,
            source=citation.chunk.source,
            doc_id=citation.chunk.doc_id,
            preview=citation.chunk.context_text[:PREVIEW_CHARS],
            score=citation.score,
            rerank_score=citation.rerank_score,
            page=citation.page,
            image_keys=citation.chunk.image_keys,
        )
        for citation in citations
    )


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """一轮问答(对话历史里的一条)。

    刻意不用 dict:写成 ``[{"question": ..., "answer": ...}]`` 的话,每个消费点都要自己
    校验 ``"question" in h``、自己转类型;换成领域类型之后,
    「一轮历史长什么样」由类型担保,用例与提示词层都不用再写一遍防御。

    ``citations`` 是**当轮引用卡片的快照**(见 ``CitationSnapshot``):切回旧会话时要连
    出处一起看到 —— 答案正文里的 ``[1]`` 没有卡片可点,等于没有出处。它**不进提示词**
    (``rag_answer_prompt`` 只渲染问答),也不参与窗口的字符预算。
    """

    question: str
    answer: str
    citations: tuple[CitationSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """一次模型调用**烧掉的 token 数**(provider 说的,不是我们数的)。

    **为什么必须由 provider 给、不许自己按字数估**:算钱要的是计费口径的 token
    (分词器切出来的、含思考内容的、含缓存命中的那一套),它和"答案有多少字"没关系;
    自己估出来的数字会**看起来很合理**,然后每个月和账单对不上 —— 那种错没人查得出来。

    ``model`` 是**provider 回报的模型名**(拿不到时适配器填配置里的名字):
    单价是按模型配的(见 ``config.CostConfig``),用一个猜的名字去查表等于编一个价格。

    ``input_tokens`` / ``output_tokens`` 给 ``None`` 与给 ``0`` 是两件事:
    ``None`` = 这个 provider/这条路径**没告诉我们**(报表要如实说"没量到"),
    ``0`` = 真的一个都没用(空回答)。折成 0 会让"没量到"和"没花钱"看不出区别。
    """

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 输入 token 里**命中缓存**的那部分(provider 的 prompt cache)。
    # **单价与未命中的差着 50 倍**(0.02 vs 1 元/百万),所以这个数不报就等于把账单算错一个量级。
    #
    # 三个值的含义必须分清:
    #   ``None`` = provider 没回报缓存明细 → 报表如实说"按未命中价计(偏保守)";
    #   ``0``    = 报了,这一轮一个都没命中;
    #   ``>0``   = 命中了这么多(它**包含在** input_tokens 里,不是额外的)。
    cached_input_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        """输入:无。行为:把两个数加起来。输出:总数;两个都没量到给 None。"""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def uncached_input_tokens(self) -> int | None:
        """输入:无。行为:算未命中的输入 token。输出:数;缺任一端给 None(不拿 0 顶)。"""
        if self.input_tokens is None or self.cached_input_tokens is None:
            return None
        # 夹到 >=0:provider 偶尔把缓存命中数报得比输入总数还大(见过的真实形状),
        # 负的"未命中"会让金额变成负数 —— 那种数没人看得懂,不如夹到 0。
        return max(self.input_tokens - self.cached_input_tokens, 0)


@dataclass(frozen=True, slots=True)
class LlmDelta:
    """模型流式输出的一小段:增量文本 + 这一帧带来的结束原因。

    provider 只在**收尾**那一帧给 ``finish_reason``("stop" = 正常说完;"length" = 撞到
    max_tokens 被砍;"content_filter" 等其它值同理)。适配器会把收尾信息补成
    ``last=True`` 的最后一帧 —— 消费方只看那一帧就够了,不用自己猜哪帧带原因。

    **为什么结束原因必须传出来**:实测到过「半截答案」(「…同时用向量」就没了),
    而当时适配器把它丢了 —— 用户拿到半句话却没有任何提示,比明说「答不出来」更误导。

    **思考内容走另一个字段**(``reasoning_text``):思考模型(DeepSeek-V4 系列)
    在正文之前会先吐一串 ``reasoning_content``。它**不许进 ``text``** ——
    理由不是洁癖:``"".join(d.text for d in deltas)`` 是「答案」在全项目里的定义,
    思考要是混在 ``text`` 里,每个消费方都得记得加过滤,漏一处就把模型的草稿印给用户。
    分成两个字段后,「答案 = 所有 text 帧」这条恒等式不需要任何附加条件。
    """

    text: str = ""
    reasoning_text: str = ""
    finish_reason: str | None = None
    last: bool = False
    # 用量**只挂在收尾帧上**(provider 就是在最后才给它的):消费方不需要为它写逐帧累加,
    # 只要在 ``delta.last`` 那一帧顺手看一眼(见 application/answer_rag.py)。
    # 中间帧上是 None —— 「Provider 到这儿还没说」与「说了 0」因此分得开。
    usage: LlmUsage | None = None

    @property
    def reasoning(self) -> bool:
        """输入:无。行为:判断这一帧是不是思考内容。输出:True = 思考(不是答案)。"""
        return bool(self.reasoning_text)

    @property
    def truncated(self) -> bool:
        """输入:无。行为:判断这一帧带来的结束原因是否异常。输出:True = 被强制结束。

        ``None``(provider 没说)按「没被截断」处理 —— 拿不到元数据不等于被砍断,
        不能因此每句答案都报警,那会把真的截断提示淹掉。
        """
        return self.finish_reason is not None and self.finish_reason != "stop"


@dataclass(frozen=True, slots=True)
class FaqEntry:
    """一条问答对,对应mysql中的行数据"""

    id: int
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class FaqMatch:
    """问答对和和对应的匹配分数。

    ``score`` = ``top1 原始 BM25 分 ÷ 这条问自己的原始分``，**夹到 0~1**：
    一字不差**恒为 1.000**（分子分母同源，与语料规模无关）；过不了命中口径那两道覆盖率闸门
    （提问覆盖率 / 条目覆盖率）时记 **0.0**（= 没命中，调用方走兜底）。

    也就是说：**0 = "不算命中"，1.000 = "近乎一字不差"**。判"该不该答"不靠分数的大小 ——
    夹取之后"提问里夹带了别的词"这类信息在分数上看不出来，是**覆盖率**在拦它
    （推导与实测见 ``adapters/matcher_bm25.py``）。
    """

    entry: FaqEntry
    score: float


@dataclass(frozen=True, slots=True)
class FaqAnswer:
    """最终答案以及来源"""

    answer: str
    source: Source
    score: float | None = None
    truncated: bool = False  # True = 这句兜底回答被模型砍断了(见 LlmDelta)


@dataclass(frozen=True, slots=True)
class DocumentImage:
    """文档里的一张图在正文中的锚点。

    ``key`` 是 OSS 对象的稳定身份(图片字节的 SHA-256),**不是 URL** ——
    签名 URL 会过期,存进 Document / Chunk / 历史就会变成死链。

    ``offset`` 是这张图在 ``Document.text`` 里的字符位置(图片被从正文里摘掉后,
    这个位置仍然保留)。切块时用它判断图属于哪个父块:位置只能由解析器给,
    不能事后拿图注去正文里 ``find`` —— 重复句子会给出看着合法的错位置。
    """

    key: str
    offset: int
    page: int | None = None
    alt: str = ""


@dataclass(frozen=True, slots=True)
class Document:
    """加载进来的一份原始文档(还没切分)。

    doc_id 用相对路径:日志和 chunk_id 里能一眼看出是哪份文件。
    source 是可过滤的来源标签(学科/目录),不是文件名。
    """

    doc_id: str
    text: str
    source: str
    # ``(起始, 结束, 页码)`` 区间表,**按字符偏移与 text 逐字符对齐**;
    # 空元组 = 这份格式没有页码概念(md/txt 就是空,不是"第 0 页")。
    # 默认值让现有构造不受影响(加字段不许改调用点)。
    pages: tuple[tuple[int, int, int], ...] = ()
    # 文档里的图片。空元组 = 这份文档没有图,或者图片功能没开。
    # 图注/alt 只作为给人看的说明保留,**不参与检索正文**;正文里另有一段
    # ``【图】`` 之类的短标记(由解析器决定),避免整块只剩图片时切不出父块。
    images: tuple[DocumentImage, ...] = ()


@dataclass(frozen=True, slots=True)
class Chunk:
    """文档切出来的一个子块,以及它所属的父块。

    doc_id 是它来自哪份文档 —— 重新 ingest 时靠它清掉这份文档的旧块。
    parent_id / parent_content 默认空是**给旧数据留的**:父子分层之前入库的扁平块
    没有父块,读回来也得能跑;新写入的子块一定带父块。
    检索时「有父块用父块,没有就用自己」。
    source 是文档来源(学科 / 目录名),用来做过滤 —— 跟 FaqAnswer.source 的
    "faq"/"llm" 不是一回事,那个说的是答案来源。
    """

    chunk_id: str
    text: str
    source: str
    doc_id: str  # 必填:漏了它就等于关掉「重灌时清旧块」
    parent_id: str = ""
    parent_content: str = ""
    # 这一块落在源文件的第几页(**从 1 开始**)。
    # None = 不知道 —— 两种来源都归到这里:这种格式没有页码概念(md/txt),或者块是
    # 早期入库的老数据。**不许拿 0 或 -1 当"没有页码"**:那会被读成一个真实页号。
    page: int | None = None
    # 这个子块所属**父块**包含的图片 key(内容哈希)。同一父块的所有子块共用一份 ——
    # 检索命中哪个子块不确定,但折叠父块后要能拿到同一组图。
    # 空元组 = 没有图,或者图片功能关着;老数据读回来也是空。
    image_keys: tuple[str, ...] = ()

    @property
    def context_text(self) -> str:
        """给模型看的正文:有父块就用父块(它留了上下文),没有就用自己。"""
        return self.parent_content or self.text


@dataclass(frozen=True, slots=True)
class Embedding:
    """一条文本的稠密向量(已 L2 归一化,所以内积就等于余弦)。

    **刻意只留稠密**:关键字检索由词面权重走独立一路,它带自己的稀疏向量
    (见下面的 ``SparseVector``),不塞进这里 —— 两条路各有各的索引,混在一个类型里
    会让"哪一路没数据"变得看不出来。
    """

    dense: tuple[float, ...]


# 稀疏向量:维度 → 权重(词面那一路用;查询侧只放出现过的词,权重 1,
# idf 与长度归一都烘在文档侧的权重里 —— 这样**查询侧不需要语料统计**,
# 见 adapters/bm25_sparse.py 的推导)。
#
# **空向量是合法的领域值**(一个词都没编码出来:整块标点、或者查询只有标点)。
# 但 Milvus 拒收「空稀疏行」(`empty sparse float vector row`),所以"保证非空"这件事
# 归写入侧(那边用保留维度 0 兜底),不归领域类型。
SparseVector = Mapping[int, float]


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """检索命中:子块 + 分数(越大越像,口径归适配器)。"""

    chunk: Chunk
    score: float


@dataclass(frozen=True, slots=True)
class Citation:
    """答案里 [n] 指向的那条资料。

    index 从 1 开始,和答案里的 [1][2] 对齐 —— 提示词和引用清单必须用同一个序号源,
    否则用户按 [2] 去核对时会对到别的东西上。

    **两个分数字段,刻意不合并**:

    - ``score`` 是**召回/融合分**:单路时是那一路的相似度,融合后是融合分。
    - ``rerank_score`` 是**重排分**;None 表示这条没重排过(没装配重排器,
      或者候选只有一条、排序不可能变),**不是「重排给了它 0 分」**。

    合并成一个字段就再也看不出「重排把融合的第 3 提到了第 1」—— 而那正是调重排时
    唯一要看的东西。两个分数**不可比**(RRF 量级 ≈0.016,重排是 sigmoid 后的 0~1),
    所以谁也不能拿去和阈值比。

    routes 记着命中它的路名("vector" / "bm25"),两路都中通常更可信。

    ``page`` 做成 **property 而不是字段**:引用本来就带着整个 ``Chunk``,再存一份页码就有了
    两个真值来源 —— 而折叠 / 重排 / 截断都会**重建 Citation**(见 answer_rag 的 ``_renumber``),
    谁少抄一次,页面上就少一个页码,而且看不出来。所以只留一条路:问那条子块。
    """

    index: int
    chunk: Chunk
    score: float
    routes: tuple[str, ...] = ()  # 命中它的路名("vector" / "bm25")
    rerank_score: float | None = None  # None = 没重排过(不是重排了得 0 分)

    @property
    def page(self) -> int | None:
        """输入:无。行为:取这条引用所在子块的页码。输出:页码(1 起);None = 不知道。"""
        return self.chunk.page


@dataclass(frozen=True, slots=True)
class RagAnswer:
    """知识库通道的回答:答案 + 证据链。"""

    answer: str
    citations: tuple[Citation, ...] = ()
    refused: bool = False  # 检索为空(或模型返回空)→ 没给答案,转人工
    truncated: bool = False  # True = 模型没说完(撞到长度上限);答案本身照原样保留


@dataclass(frozen=True, slots=True)
class QaAnswer:
    """合流通道的回答:哪条通道答的,以及那条通道的证据链。

    只返回一个字符串是不够的;这里把「谁答的、凭什么」一起带上 ——
    引用清单在 RagAnswer 里已经有了,合流没有理由把它丢掉(用户要能核对答案出处)。

    不变式(由 answer_question 保证):

    - ``channel == "faq"`` ⟺ ``faq_match is not None``(命中才有那条匹配);
    - ``channel == "faq"`` → ``citations == ()`` 且 ``refused is False``;
    - ``refused is True`` → ``channel == "rag"`` 且 answer 是知识库通道的拒答话术;
    - ``faq_match is None`` **不是**「FAQ 给了 0 分」,而是「这次没走 FAQ 通道」——
      与 Citation.rerank_score is None 是同一个约定。
    """

    answer: str
    channel: Channel
    citations: tuple[Citation, ...] = ()  # 知识库通道的证据;FAQ 通道为空
    faq_match: FaqMatch | None = None  # FAQ 通道命中的那一条;None = 没走 FAQ 通道
    refused: bool = False  # 两条通道都没给出可用答案 → 转人工
    truncated: bool = False  # True = 知识库通道那句答案被模型砍断了(FAQ 直答永远是 False)


@dataclass(frozen=True, slots=True)
class QaEvent:
    """流式回答的一帧(协议见 ``application/streaming.py``)。

    ``kind`` 只有三种:

    - ``sources``:知识库通道的引用清单(最多一帧,在 token 之前);
    - ``token``:要追加进答案的文本 —— **答案文本一律走这一种帧**,标准答案与
      拒答话术也一样。消费方因此只需要「打印 token」,不用为「这句话从哪来」写分支;
    - ``done``:收尾帧(**恰好一帧**),``answer`` 是拼接结果,另带
      ``channel`` / ``citations`` / ``faq_match`` / ``refused`` / ``truncated``。

    恒等式:``"".join(t.text for t in token 帧).strip() == done.answer``。
    """

    kind: Literal["sources", "token", "done"]
    text: str = ""
    answer: str = ""
    channel: Channel | None = None
    citations: tuple[Citation, ...] = ()
    faq_match: FaqMatch | None = None
    refused: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ChunkGroup:
    """一个父块 + 它切出来的子块(父子分层)。

    切分器只吐出这种分组,不生成 id —— id 怎么编是用例层的事
    (要带来源、文档、位置、内容四样东西,切分器不该知道这些)。

    ``child_starts`` 是**每个子块在传给 split() 的那段正文里的起始偏移**(字符,左闭),
    与 ``children`` **同序等长**;空元组 = 这个切分器不报偏移(旧实现、验收夹具)。
    页码就是靠它查出来的:``page_of(document.pages, child_starts[i])``。

    **为什么偏移由切分器给,而不是用例层拿子块文本去正文里 find**:重复的句子在语料里
    到处都是,find 会给出**看着合法**的错位置 —— 那比没有页码糟得多。切分器才知道每个块
    究竟是从哪里切下来的(它是从原文一步步切的,不是事后猜的)。
    """

    parent_text: str
    children: tuple[str, ...]
    child_starts: tuple[int, ...] = ()
    # 父块在**整篇正文**里的起始偏移。图片靠它挂到父块:offset 落在
    # [parent_start, 下一个 parent_start) 之间就是这一块的图。
    # None = 切分器不报位置(旧实现/验收夹具),此时**不挂图**,不许猜。
    parent_start: int | None = None


@dataclass(frozen=True, slots=True)
class FusedChunk:
    """两路融合后的一条命中。

    score 是**融合分**,不是相似度 —— RRF 分只反映「两路排名的倒数之和」,
    量纲由 rrf_k 与权重决定,拿去和任何阈值比都是错的。
    """

    chunk: Chunk
    score: float
    routes: tuple[str, ...] = ()


# 检索方式的闭集(接口层用语,翻译成权重见 api/app.py 的 ``_options``)。
# 与 ``USER_ROLES`` 同一个写法:元组给运行时(capabilities 要报给前端),
# Literal 给类型检查器(写错一个字母当场报,而不是运行时行为诡异)。
RetrievalMode = Literal["vector", "keyword", "hybrid"]
RETRIEVAL_MODES: tuple[RetrievalMode, ...] = ("vector", "keyword", "hybrid")

# 融合方式的闭集。之前只在 ``RetrievalOptions.fusion`` 的 Literal 里;现在 capabilities
# 要把它报给前端 —— 两处各写一份的话,将来加了第三种融合方式,前端下拉框会少一项(静默)。
FusionMode = Literal["rrf", "weighted"]
FUSION_MODES: tuple[FusionMode, ...] = ("rrf", "weighted")


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """一次检索的全部旋钮。

    攒成一个对象而不是排一长串关键字参数:旋钮只会越加越多(重排就是后来加的),
    每加一个就改一遍所有调用点的签名,不划算。默认值与 config.RagConfig 对齐。

    **跑哪几路不在这里**:它由两个权重表达(≤ 0 的那一路不跑,见 ``answer_rag.retrieve``),
    多一个 ``mode`` 字段就有两个真值来源 —— 而"权重说关、模式说开"时谁赢没人猜得出来。
    ``mode`` 只是**接口层**的说法(``RETRIEVAL_MODES``),到了这一层就翻译成权重。
    """

    k: int = 5  # 每一路各召回多少条
    max_context: int | None = None  # 折叠父块后给模型几条
    source: str | None = None  # 来源过滤
    fusion: FusionMode = "rrf"
    rrf_k: int = 60  # RRF 的平滑常数:越大越弱化「头部优势」
    bm25_weight: float = 0.5
    vector_weight: float = 0.5
    # 喂给重排器的是**父块正文**(``"parent"``,默认)还是**子块正文**(``"child"``):
    # 前者是「模型会读到的那段文字」,所以引用清单上的重排分核对得上;后者是「检索命中的那个单元」,
    # 更短更聚焦,但那个对应关系就断了。**默认保持 parent** —— 换默认要等 A/B 的读数;
    # 这个旋钮现在只给 A/B 用,还没进 YAML。
    rerank_input: Literal["parent", "child"] = "parent"

    # 「这些文档这一次**不许**被检索到」 —— 可见范围在检索这一侧的落地(见 application/access.py)。
    #
    # **为什么是"禁止名单"而不是"允许名单"**:可见范围的口径是"规则只用来收窄"
    # (没配规则的文档人人可见),所以对绝大多数人来说,能看的文档 = 全部 − 少数几份被限制的。
    # 用允许名单就得把全部 doc_id 都传下来(几百份文档 → 一个超长表达式 + 每次查询都白传),
    # 而禁止名单通常只有几条。
    #
    # **空元组 = 不限制**(不是"什么都看不到")。这一点必须记牢:把空表当成"全禁"
    # 会让一次"没查出规则"的数据库抖动变成整个知识库搜不到东西(而且不报错)。
    denied_docs: tuple[str, ...] = ()

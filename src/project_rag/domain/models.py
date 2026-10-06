"""领域模型:纯数据,不依赖任何第三方 SDK。这里只做了自定义数据容器

domain 是依赖图的叶子 —— 这个包不认识 ports / adapters / cli 里的任何东西。
判断一个类型该不该放这里,问一句:如果明天把所有 LLM 端口都删掉,它还在不在?
ChatMessage 在(会话历史、上下文拼装、评估用例都要用它),所以它属于 domain。
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """一轮问答(对话历史里的一条)。

    刻意不用 dict:写成 ``[{"question": ..., "answer": ...}]`` 的话,每个消费点都要自己
    校验 ``"question" in h``、自己转类型;换成领域类型之后,
    「一轮历史长什么样」由类型担保,用例与提示词层都不用再写一遍防御。
    """

    question: str
    answer: str


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
    """问答对和和对应的匹配分数"""

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
class Document:
    """加载进来的一份原始文档(还没切分)。

    doc_id 用相对路径:日志和 chunk_id 里能一眼看出是哪份文件。
    source 是可过滤的来源标签(学科/目录),不是文件名。
    """

    doc_id: str
    text: str
    source: str
    # P0-5:``(起始, 结束, 页码)`` 区间表,**按字符偏移与 text 逐字符对齐**;
    # 空元组 = 这份格式没有页码概念(md/txt 就是空,不是"第 0 页")。
    # 默认值让现有构造不受影响(加字段不许改调用点)。
    pages: tuple[tuple[int, int, int], ...] = ()


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
    # P0-5:这一块落在源文件的第几页(**从 1 开始**)。
    # None = 不知道 —— 两种来源都归到这里:这种格式没有页码概念(md/txt),或者块是
    # P0-5 之前入库的老数据。**不许拿 0 或 -1 当"没有页码"**:那会被读成一个真实页号。
    page: int | None = None

    @property
    def context_text(self) -> str:
        """给模型看的正文:有父块就用父块(它留了上下文),没有就用自己。"""
        return self.parent_content or self.text


@dataclass(frozen=True, slots=True)
class Embedding:
    """一条文本的稠密向量(已 L2 归一化,所以内积就等于余弦)。

    **刻意只留稠密**:关键字检索由 BM25 走独立一路(进程内),不用嵌入模型的稀疏向量 ——
    学习型稀疏与 BM25 都是词面匹配,两路重叠度高,留着只会让融合多一路、评估多一维,
    还要多维护一个稀疏索引。少一路,而且每一路都说得清。
    """

    dense: tuple[float, ...]


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

    不变式(由 answer_question 保证,check_m5_qa.py 逐条盯着):

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
    每加一个就改一遍所有调用点的签名,不划算。默认值与 config.RagConfig 对齐(有验收盯着)。

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
    # 更短更聚焦,但那个对应关系就断了。**默认保持 parent** —— 换默认要等 `ab_retrieval.py`
    # 的读数(见 STATUS §7 P0-4);这个旋钮现在只给 A/B 用,还没进 YAML。
    rerank_input: Literal["parent", "child"] = "parent"

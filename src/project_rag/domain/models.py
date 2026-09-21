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


# 这个装饰器,frozen=Tru参数表示这个类构建的对象不可变,不可动态添加属性.slots=True表示优化缓存
@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条对话消息。

    只用 dataclass,不用 pydantic:domain 不该依赖校验/序列化框架,
    这样它可以在任何地方被实例化、比较、当字典键,不需要框架参与。
    M2 要给 FastAPI 返回时,在 cli 层做 domain -> schema 的转换。

    也不要在这里加 to_langchain() 之类的方法 —— 那会把 langchain 拉进 domain。
    转换是适配器的职责(见 adapters/llm_deepseek.py 的 to_sdk_messages)。
    """

    # 声明Role只能是system、user、assistant三种字面量
    role: Role
    content: str


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


@dataclass(frozen=True, slots=True)
class Document:
    """加载进来的一份原始文档(还没切分)。

    doc_id 用相对路径:日志和 chunk_id 里能一眼看出是哪份文件。
    source 是可过滤的来源标签(学科/目录),不是文件名。
    """

    doc_id: str
    text: str
    source: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """文档切出来的一个子块,以及它所属的父块。

    doc_id 是它来自哪份文档 —— 重新 ingest 时靠它清掉这份文档的旧块。
    parent_id / parent_content 默认空:M3.4 的朴素切分还没有父子概念,M3.7 才填;
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

    score 是**排序分**:单路时是那一路的相似度,融合后是融合分 —— 只用于排序与展示,
    别拿去和阈值比。routes 记着命中它的路名("vector" / "bm25"),两路都中通常更可信。
    """

    index: int
    chunk: Chunk
    score: float
    routes: tuple[str, ...] = ()  # 命中它的路名("vector" / "bm25")


@dataclass(frozen=True, slots=True)
class RagAnswer:
    """知识库通道的回答:答案 + 证据链。"""

    answer: str
    citations: tuple[Citation, ...] = ()
    refused: bool = False  # 检索为空(或模型返回空)→ 没给答案,转人工


@dataclass(frozen=True, slots=True)
class ChunkGroup:
    """一个父块 + 它切出来的子块(讲义 4.3 的父子分层)。

    切分器只吐出这种分组,不生成 id —— id 怎么编是用例层的事
    (要带来源、文档、位置、内容四样东西,切分器不该知道这些)。
    """

    parent_text: str
    children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FusedChunk:
    """两路融合后的一条命中。

    score 是**融合分**,不是相似度 —— RRF 分只反映「两路排名的倒数之和」,
    量纲由 rrf_k 与权重决定,拿去和任何阈值比都是错的。
    """

    chunk: Chunk
    score: float
    routes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """一次检索的全部旋钮。

    攒成一个对象而不是排一长串关键字参数:旋钮只会越加越多(M3.9 还要加重排),
    每加一个就改一遍所有调用点的签名,不划算。默认值与 config.RagConfig 对齐(有验收盯着)。
    """

    k: int = 5  # 每一路各召回多少条
    max_context: int | None = None  # 折叠父块后给模型几条
    source: str | None = None  # 来源过滤
    fusion: Literal["rrf", "weighted"] = "rrf"
    rrf_k: int = 60  # RRF 的平滑常数:越大越弱化「头部优势」
    bm25_weight: float = 0.5
    vector_weight: float = 0.5

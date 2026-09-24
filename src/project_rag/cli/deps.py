"""
组合根:把「怎么造依赖」收在一个可注入的数据结构里。

装配完就不该再被改,而每个字段默认 None 即「不注入就不造」。

惰性:字段全是工厂,不是实例。所以 chat 不会因为 MySQL 挂了就跑不起来,
--help 更不会去连任何东西。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

from project_rag.adapters.bm25_index import BM25KeywordIndex
from project_rag.domain import ConversationTurn, QaEvent, RetrievalOptions
from project_rag.ports import (
    Cache,
    ConversationStore,
    Embedder,
    FaqStore,
    LLMClient,
    Reranker,
    TextMatcher,
    VectorStore,
)
from project_rag.ports.keyword_index import KeywordIndex

# 与 config.FaqConfig.threshold 保持一致;口径见 adapters/matcher_bm25.py
DEFAULT_FAQ_THRESHOLD = 0.85


# 这个类实例创建完成以后,就不再需要修改了,所以用 frozen=True,slots=True
@dataclass(frozen=True, slots=True)
class Deps:
    """一次进程运行需要的全部外部依赖,全部以工厂形式持有。"""

    # 类型注解,一个可调用的对象(如函数):Callable[[参数类型列表], 返回类型]
    llm_factory: Callable[[], LLMClient] | None = None
    faq_store_factory: Callable[[], FaqStore] | None = None
    matcher_factory: Callable[[], TextMatcher] | None = None
    cache_factory: Callable[[], Cache | None] | None = None  # 返回 None = 无缓存
    faq_threshold: float = DEFAULT_FAQ_THRESHOLD
    embedder_factory: Callable[[], Embedder] | None = None
    vector_store_factory: Callable[[], VectorStore] | None = None
    # 关键字那一路。它的语料也是从向量库取的(全量文本只有那一份),
    # 所以它跟 vector_store_factory 共享同一个实例 —— 见 build_real_deps 里的 lru_cache
    keyword_index_factory: Callable[[], KeywordIndex] | None = None
    # 重排。**注意它和上面几个不一样:工厂存在 ≠ 每次都用** ——
    # 用不用由 config 的 rag.rerank 决定,而那个开关只在命令层读。
    # 这样「关掉重排」不会顺带把模型加载也省掉,语义干净:
    # 组合根负责「造得出来」,命令负责「这一次要不要用」。
    reranker_factory: Callable[[], Reranker] | None = None
    # M5.2 起:会话历史。带了 --session 才用得到,不注入就不造(同其它工厂)。
    conversation_store_factory: Callable[[], ConversationStore] | None = None


def build_real_deps() -> Deps:
    """真实装配:读一次 Settings,把八个工厂装好。

    输入:无
    行为:读配置,工厂只捕获 settings 不建连接,真正的连接发生在命令用到它的时候;
         适配器延迟 import,否则 --help 会把 langchain / pymysql / redis / jieba 全拖进来
    输出:进程级 Deps

    核心:这些依赖会统一交给args.Deps并且传给所有子命令,子命令再根据自身需要创建策略类

    """
    from project_rag.adapters import (
        BM25TextMatcher,
        DeepSeekLLM,
        MySQLConversationStore,
        MySQLFaqStore,
        RedisCache,
    )
    from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.adapters.reranker_bge import BGEReranker
    from project_rag.config import get_settings

    settings = get_settings()
    # 匹配器只造一个:工厂每次调用都返回同一个实例,它按语料指纹缓存的索引才有意义
    matcher = BM25TextMatcher()

    # 两路共用同一个向量库实例:BM25 那一路要从库里取全量语料,
    # 各造一个就会开两个 Milvus 客户端(连接与内存都翻倍)
    @lru_cache(maxsize=1)
    def _vector_store() -> MilvusVectorStore:
        return MilvusVectorStore(settings.milvus)

    # lambda是为了构建闭包形式,让函数记住参数,后面调用不用传参,也可以在前面def定义,但是不够优雅
    # 这些变量都指向函数和类而不是直接实现,Deps.属性()才真正执行的,惰性加载策略类(懒汉模式)
    return Deps(
        llm_factory=lambda: DeepSeekLLM(settings.llm),
        faq_store_factory=lambda: MySQLFaqStore(settings.mysql, settings.faq),
        matcher_factory=lambda: matcher,
        cache_factory=lambda: RedisCache(settings.redis),
        # M3:知识库通道。BGEM3Embedder 会把 torch 拉起来(秒级 + 上 G 内存),
        # 所以它只在这两个工厂**被调用**时才构造 —— ingest / search 之外用不到就不付这份钱。
        embedder_factory=lambda: BGEM3Embedder(settings.embedding),
        vector_store_factory=_vector_store,
        keyword_index_factory=lambda: BM25KeywordIndex(_vector_store()),
        # 重排模型同样上 G 内存(实测加载 1.6s),所以也是惰性工厂;
        # 而且它跟嵌入共用 embedding 段配置(rerank_model_dir / device / use_f16)
        reranker_factory=lambda: BGEReranker(settings.embedding),
        # M5.2:会话历史。pymysql 很轻,但同样只在 --session 用到时才构造
        conversation_store_factory=lambda: MySQLConversationStore(
            settings.mysql, settings.conversation
        ),
        faq_threshold=settings.faq.threshold,
    )


_T = TypeVar("_T")


def _required(factory: Callable[[], _T] | None, name: str) -> Callable[[], _T]:
    """输入:一个可空工厂 + 它的名字。行为:为空则报错(组合根漏装),否则原样返回。输出:非空工厂。

    命令层已经查过「缺哪个」并给了人话;这里再兜一层是为了让类型检查器把
    ``Callable | None`` 收窄成 ``Callable`` —— 否则每个调用点都要手写一遍 assert。
    """
    if factory is None:
        raise RuntimeError(f"{name} 没装配:合流问答要 FAQ 与知识库两条通道的端口")
    return factory


def knowledge_callable(
    deps: Deps, query: str, options: RetrievalOptions, *, rerank: bool
) -> Callable[[Sequence[ConversationTurn]], Iterator[QaEvent]]:
    """把知识库通道的端口拼成**一次调用的零参函数**(不调用它就什么都不构造)。

    输入:依赖集合、问题、检索旋钮、要不要重排
    行为:返回一个闭包;闭包被调用时才去调那几个惰性工厂(向量库/嵌入/BM25/重排)
    输出:(历史轮次) -> 帧序列 —— 历史由用例层读出来再交进来,组合根不碰会话存储

    为什么要有它:合流入口(``qa`` 命令、M5.4 的 FastAPI)都从这里拿「知识库通道」——
    怎么拼只写一处,省得某条入口漏传了 source 或检索旋钮(讲义 ④-06 就是三条增强
    检索路径都漏传 source_filter)。放在组合根而不是用例里,是因为「怎么造、什么时候造」
    正是组合根的职责;用例只该知道「有个函数能给我答案」。**惰性也在这里落地**:
    ``qa`` 命中 FAQ 时这个闭包一次都不会被调用,嵌入/重排模型也就不会被加载。
    """

    def call(history: Sequence[ConversationTurn]) -> Iterator[QaEvent]:
        from project_rag.application import stream_rag

        return stream_rag(
            query,
            embedder=_required(deps.embedder_factory, "embedder_factory")(),
            store=_required(deps.vector_store_factory, "vector_store_factory")(),
            llm=_required(deps.llm_factory, "llm_factory")(),
            keyword_index=_required(
                deps.keyword_index_factory, "keyword_index_factory"
            )(),
            reranker=_required(deps.reranker_factory, "reranker_factory")()
            if rerank
            else None,
            options=options,
            history=history,
        )

    return call

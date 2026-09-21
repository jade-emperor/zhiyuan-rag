"""
组合根:把「怎么造依赖」收在一个可注入的数据结构里。

装配完就不该再被改,而每个字段默认 None 即「不注入就不造」。

惰性:字段全是工厂,不是实例。所以 chat 不会因为 MySQL 挂了就跑不起来,
--help 更不会去连任何东西。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from project_rag.adapters.bm25_index import BM25KeywordIndex
from project_rag.ports import (
    Cache,
    Embedder,
    FaqStore,
    LLMClient,
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


def build_real_deps() -> Deps:
    """真实装配:读一次 Settings,把六个工厂装好。

    输入:无
    行为:读配置,工厂只捕获 settings 不建连接,真正的连接发生在命令用到它的时候;
         适配器延迟 import,否则 --help 会把 langchain / pymysql / redis / jieba 全拖进来
    输出:进程级 Deps

    核心:这些依赖会统一交给args.Deps并且传给所有子命令,子命令再根据自身需要创建策略类

    """
    from project_rag.adapters import (
        BM25TextMatcher,
        DeepSeekLLM,
        MySQLFaqStore,
        RedisCache,
    )
    from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import get_settings

    settings = get_settings()
    # 提前创建对象,后续的BM25索引才可用
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
        faq_threshold=settings.faq.threshold,
    )

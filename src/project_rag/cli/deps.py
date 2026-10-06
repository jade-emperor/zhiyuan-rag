"""
组合根:把「怎么造依赖」收在一个可注入的数据结构里。

装配完就不该再被改,而每个字段默认 None 即「不注入就不造」。

惰性:字段全是工厂,不是实例。所以 chat 不会因为 MySQL 挂了就跑不起来,
--help 更不会去连任何东西。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, TypeVar

from project_rag.adapters.bm25_index import BM25KeywordIndex
from project_rag.domain import ConversationTurn, QaEvent, RetrievalOptions
from project_rag.observability import get_logger, note_build
from project_rag.observability.ledger_sink import LedgerHandler, attach_ledger_sink
from project_rag.ports import (
    AuthStore,
    Cache,
    DocumentStore,
    ConversationStore,
    Embedder,
    FaqStore,
    LedgerStore,
    LLMClient,
    Reranker,
    SessionCatalog,
    TextMatcher,
    VectorStore,
)
from project_rag.ports.keyword_index import KeywordIndex

_logger = get_logger("cli.deps")

def _default_faq_threshold() -> float:
    """输入:无。行为:读配置里的 FAQ 阈值。输出:阈值。

    **默认值为什么不写成字面量**:写 `0.85` 就等于在 config 之外又立了一个真源,
    还得靠一条验收盯着两边一致(法则 #25)。这里改成 dataclass 的 ``default_factory`` ——
    它只在**构造 Deps 时**才被调用,所以本模块 import 期仍然不碰 config
    (`--help` 那条路根本不构造 Deps),而 `build_real_deps()` 自动拿到配置值。
    """
    from project_rag.config import get_settings

    return get_settings().faq.threshold


def _default_cache_ttl() -> int:
    """输入:无。行为:读配置里的缓存 TTL。输出:秒数。理由同 ``_default_faq_threshold``。"""
    from project_rag.config import get_settings

    return get_settings().cache.ttl_s


# 这个类实例创建完成以后,就不再需要修改了,所以用 frozen=True,slots=True
@dataclass(frozen=True, slots=True)
class Deps:
    """一次进程运行需要的全部外部依赖,全部以工厂形式持有。"""

    # 类型注解,一个可调用的对象(如函数):Callable[[参数类型列表], 返回类型]
    llm_factory: Callable[[], LLMClient] | None = None
    faq_store_factory: Callable[[], FaqStore] | None = None
    matcher_factory: Callable[[], TextMatcher] | None = None
    cache_factory: Callable[[], Cache | None] | None = None  # 返回 None = 无缓存
    # 延迟账本的持久化(落表)。默认 None = 不落表,只记日志 —— 建表/连库失败时
    # `attach_ledger` 会退到这一档,所以「没有 MySQL」不等于「服务起不来」。
    ledger_store_factory: Callable[[], LedgerStore] | None = None
    # 认证与授权(用户/令牌/会话归属)。**默认 None = 无认证模式** —— 那是
    # 「显式注入的结果」,不是"忘了配":验收脚本注入无认证的 Deps 才能只测业务逻辑,
    # 而 `build_real_deps` 永远把它接上(生产路径没有无认证这一档)。
    auth_store_factory: Callable[[], AuthStore] | None = None
    # 文档登记表(有哪些文档 / 各自状态)。默认 None = 不接(老契约与纯检索路径用不上)。
    document_store_factory: Callable[[], DocumentStore] | None = None
    # 上传入库要用的三样,全部走工厂:**api/ 只许认识 Deps,不许认识适配器**
    # (那条分层法律也有 AST 检查)。切分器、装载函数、支持的后缀表都在适配器里。
    chunker_factory: Callable[[], Any] | None = None
    loader_factory: Callable[[], Any] | None = None
    suffixes_factory: Callable[[], tuple[str, ...]] | None = None
    # 阈值:默认取配置(见 _default_faq_threshold),注入就按注入的来 ——
    # 验收脚本靠这个注入一个边界值直接测「命中判定」。
    faq_threshold: float = field(default_factory=_default_faq_threshold)
    # 缓存过期秒数。和 faq_threshold 一样是**静态标量**:默认取配置,也可以注入。
    # 放 Deps 而不是让 faq.py 自己 get_settings(),
    # 是为了让验收脚本能注入一个短 TTL 直接测过期。
    cache_ttl: int = field(default_factory=_default_cache_ttl)
    # 思考档位。**不是工厂**:它是个静态标签,唯一用途是让延迟账里能看出
    # 「这一轮按哪一档跑的」。命令层把它转交给 stream_rag / stream_question,用例与账本
    # 都不认识 config。默认 None → 账里是 '-'（注入式调用不该被编一个假档位)。
    thinking: str | None = None
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
    # 会话历史。带了 --session 才用得到,不注入就不造(同其它工厂)。
    conversation_store_factory: Callable[[], ConversationStore] | None = None
    # 多集合视角(列出集合、删掉一个集合)。**Milvus 特有** —— 端口 VectorStore 讲的是
    # "一个集合怎么读写",而"库里有哪几个、哪个该删"是运维动作(P0-5 加 page 列之后,
    # 旧集合不能写、新集合要建要删,这类事必须有接口,见 cli/commands/drop.py)。
    # 适配器在**组合根这一层**被包成两个函数,命令层不 import pymilvus ——
    # 与「api/ 只许认识 Deps」是同一条分层法律。
    collections_factory: Callable[[], Sequence[Any]] | None = None
    drop_collection_factory: Callable[[str], int] | None = None
    # 会话目录(前端左侧「我有哪些会话」)。与 conversation_store_factory 是**两张不同的表**
    # (归属表 + 对话表),所以是两个工厂:只问答的入口不必认识它。
    session_catalog_factory: Callable[[], SessionCatalog] | None = None


def build_real_deps() -> Deps:
    """真实装配:读一次 Settings,把全部工厂装好(数量别写进这句 —— 写死了会漂,见法律 4)。

    输入:无
    行为:读配置,工厂只捕获 settings 不建连接,真正的连接发生在命令用到它的时候;
         适配器延迟 import,否则 --help 会把 langchain / pymysql / redis / jieba 全拖进来
    输出:进程级 Deps

    核心:这些依赖会统一交给args.Deps并且传给所有子命令,子命令再根据自身需要创建策略类

    """
    from project_rag.adapters import (
        BM25TextMatcher,
        MySQLConversationStore,
        MySQLFaqStore,
        RedisCache,
    )
    # 重适配器一律按完整路径 import:它们不在 adapters 包入口里
    # (llm_deepseek 拉 langchain,实测 3510ms;另外两个拉 torch,秒级 + 上 G 内存)
    from project_rag.adapters.auth_mysql import MySQLAuthStore
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import load_documents, supported_suffixes
    from project_rag.adapters.documents_mysql import MySQLDocumentStore
    from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder
    from project_rag.adapters.ledger_mysql import MySQLLedgerStore
    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.adapters.milvus_store import (
        MilvusVectorStore,
        drop_collection,
        list_collections,
    )
    from project_rag.adapters.reranker_bge import BGEReranker
    from project_rag.adapters.sessions_mysql import MySQLSessionCatalog
    from project_rag.config import get_settings

    settings = get_settings()
    # 匹配器只造一个:工厂每次调用都返回同一个实例,它按语料指纹缓存的索引才有意义
    matcher = BM25TextMatcher()

    # 下面四个工厂都是**进程级单例**(lru_cache),因为它们各自抱着一份重资源:
    # Milvus 连接 / 嵌入模型(2G) / 重排模型(2.2G) / BM25 的全量分词索引。
    # 为什么必须缓存:CLI 一进程一命令时无所谓(那正是惰性工厂的设计初衷),但
    # `project-rag-api` 是**常驻进程** —— 不缓存就是「每个走 RAG 的请求重载一遍模型」。
    # 实测(延迟账):一次请求 10.4s 里有 **4.4s** 花在构造端口上,
    # 单是 BGEM3Embedder 636ms + BGEReranker 394ms(CPU,构造两次耗时一样 → 完全没复用)。
    #
    # 两路共用同一个向量库实例:BM25 那一路要从库里取全量语料,
    # 各造一个就会开两个 Milvus 客户端(连接与内存都翻倍)
    @lru_cache(maxsize=1)
    def _vector_store() -> MilvusVectorStore:
        note_build("vector_store")
        return MilvusVectorStore(settings.milvus)

    @lru_cache(maxsize=1)
    def _embedder() -> BGEM3Embedder:
        note_build("embedder")  # 2G 模型:账本里"冷"最主要的那一笔
        return BGEM3Embedder(settings.embedding)

    @lru_cache(maxsize=1)
    def _reranker() -> BGEReranker:
        note_build("reranker")  # 2.2G 模型
        return BGEReranker(settings.embedding)

    @lru_cache(maxsize=1)
    def _keyword_index() -> BM25KeywordIndex:
        # 缓存只用来存「语料指纹的摘要」,让每查询那次 397.6ms 的 Milvus 往返
        # 变成一次 Redis GET(~0.1ms);缓存坏了自动退回"每查询读一次指纹",不影响正确性。
        return BM25KeywordIndex(
            _vector_store(),
            RedisCache(settings.redis),
            settings.cache.corpus_fp_ttl_s,
        )

    # lambda是为了构建闭包形式,让函数记住参数,后面调用不用传参,也可以在前面def定义,但是不够优雅
    # 这些变量都指向函数和类而不是直接实现,Deps.属性()才真正执行的,惰性加载策略类(懒汉模式)
    return Deps(
        llm_factory=lambda: DeepSeekLLM(settings.llm),
        faq_store_factory=lambda: MySQLFaqStore(settings.mysql, settings.faq),
        matcher_factory=lambda: matcher,
        cache_factory=lambda: RedisCache(settings.redis),
        ledger_store_factory=lambda: MySQLLedgerStore(settings.mysql),
        auth_store_factory=lambda: MySQLAuthStore(settings.mysql),
        document_store_factory=lambda: MySQLDocumentStore(settings.mysql),
        chunker_factory=lambda: ChineseRecursiveChunker(
            settings.rag.parent_chunk_size,
            settings.rag.child_chunk_size,
            settings.rag.chunk_overlap,
        ),
        loader_factory=lambda: load_documents,
        suffixes_factory=supported_suffixes,
        # 集合级运维(列现状 / 删集合本身)。这两个不碰模型、也不建连接 —— 客户端在**被调用时**才建。
        collections_factory=lambda: list_collections(settings.milvus),
        drop_collection_factory=lambda name: drop_collection(settings.milvus, name),
        # 知识库通道。BGEM3Embedder 会把 torch 拉起来(秒级 + 上 G 内存),
        # 所以它只在工厂**被调用**时才构造 —— ingest / search 之外用不到就不付这份钱。
        embedder_factory=_embedder,
        vector_store_factory=_vector_store,
        keyword_index_factory=_keyword_index,
        # 重排模型同样上 G 内存(实测加载 1.6s),所以也是惰性工厂;
        # 而且它跟嵌入共用 embedding 段配置(rerank_model_dir / device / use_f16)
        reranker_factory=_reranker,
        # 会话历史。pymysql 很轻,但同样只在 --session 用到时才构造
        conversation_store_factory=lambda: MySQLConversationStore(
            settings.mysql, settings.conversation
        ),
        # 会话目录:只读、只查,建表由上面那个(ensure_table)与 auth 的 init_schema 负责 ——
        # 这条路径不该有副作用(前端每次打开都会调它)。
        session_catalog_factory=lambda: MySQLSessionCatalog(
            settings.mysql, settings.conversation
        ),
        faq_threshold=settings.faq.threshold,
        cache_ttl=settings.cache.ttl_s,
        thinking=settings.llm.thinking,
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

    为什么要有它:合流入口(``qa`` 命令、FastAPI)都从这里拿「知识库通道」——
    怎么拼只写一处,省得某条入口漏传了 source 或检索旋钮(三条增强检索路径都漏传过
    source_filter)。放在组合根而不是用例里,是因为「怎么造、什么时候造」
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
            thinking=deps.thinking,
        )

    return call


def attach_ledger(deps: Deps) -> LedgerHandler | None:
    """输入:Deps。行为:确保账本表存在,并把落表 handler 挂到 ``project_rag`` 上(**幂等**)。输出:handler;接不上给 None。

    **为什么是组合根的事**:账本的出口只有一处(``StreamTiming.emit``),但入口有三条 ——
    CLI、HTTP、WebSocket。在这里挂一次,三条路径就都落表;反过来在每个调用点各接一次,
    迟早漏一条,而且**漏了不报错**(只是那张表里少一类请求)。

    **失败只警告,不拦启动**:观测是旁路。MySQL 没起时服务必须照常起得来 ——
    否则"加了个报表"就变成"服务起不来",本末倒置。降级后的行为是:账继续记在日志里,
    只是不进表(``metrics`` 命令会读到空表并如实报 0 行)。
    """
    if deps.ledger_store_factory is None:
        return None
    try:
        store = deps.ledger_store_factory()
        store.init_schema()
    except Exception as e:  # noqa: BLE001 - 见上:观测接不上不该拦启动
        _logger.warning("账本落表未启用(建表失败,服务继续跑,账只在日志里): %s", e)
        return None
    return attach_ledger_sink(store.record)

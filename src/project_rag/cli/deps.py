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
from typing import TYPE_CHECKING, Any, TypeVar

from project_rag.adapters.bm25_index import BM25KeywordIndex
from project_rag.adapters.bm25_sparse import BM25SparseEncoder, MilvusSparseKeywordIndex
from project_rag.application.cost import PeriodRule
from project_rag.domain import ConversationTurn, QaEvent, RetrievalOptions
from project_rag.observability import StreamTiming, get_logger, note_build
from project_rag.observability.ledger_sink import LedgerHandler, attach_ledger_sink
from project_rag.ports import (
    AccessStore,
    AuthStore,
    Cache,
    DocumentStore,
    ConversationStore,
    Embedder,
    FaqStore,
    LedgerStore,
    LLMClient,
    QueryRewriter,
    RateLimiter,
    Reranker,
    SessionCatalog,
    SparseEncoder,
    TextMatcher,
    VectorStore,
)
from project_rag.ports.keyword_index import KeywordIndex

if TYPE_CHECKING:  # 只为注解;知识库通道的形状由用例层定义
    from project_rag.application.answer_question import KnowledgeChannel

_logger = get_logger("cli.deps")

def _default_faq_threshold() -> float:
    """输入:无。行为:读配置里的 FAQ 阈值。输出:阈值。

    **默认值为什么不写成字面量**:写 `0.85` 就等于在 config 之外又立了一个真源,
    还得靠一条验收盯着两边一致(法则 #25)。这里改成 dataclass 的 ``default_factory`` ——
    它只在**构造 Deps 时**才被调用,所以本模块 import 期仍然不碰 config
    (`--help` 那条路根本不构造 Deps),而 `build_real_deps()` 自动拿到配置值。
    """
    from project_rag.config import get_settings, project_path

    return get_settings().faq.threshold


def _default_cache_ttl() -> int:
    """输入:无。行为:读配置里的缓存 TTL。输出:秒数。理由同 ``_default_faq_threshold``。"""
    from project_rag.config import get_settings

    return get_settings().cache.ttl_s


def _default_cost_prices() -> dict[str, Any]:
    """输入:无。行为:读配置里的单价表(``模型 -> 三档 × 两时段的价``)。输出:字典。

    理由同 ``_default_faq_threshold``:写一份字面量就等于在 config 之外又立一个真源。
    验收脚本可以注入自己的单价表(一个同形状的对象就行),直接测"算钱"那段口径。
    """
    from project_rag.config import get_settings

    return get_settings().cost.prices()


def _default_cost_rule() -> PeriodRule:
    """输入:无。行为:把配置里的高峰口径(星期 / 窗口 / 节假日)解析成判定规则。输出:``PeriodRule``。

    **解析只在这一处**(与 ``_default_cost_prices`` 同一个理由):用例层收到的是能直接判的
    规则,不是"三个原始列表";配置写错时在这里当场抛,而不是每个请求各抛一次。
    """
    from project_rag.config import get_settings

    cost = get_settings().cost
    return PeriodRule.of(
        weekdays=cost.peak.weekdays,
        windows=cost.peak.windows,
        holidays=cost.holidays,
    )


def _default_cost_currency() -> str:
    """输入:无。行为:读配置里的币种。输出:文本(只用于显示)。"""
    from project_rag.config import get_settings

    return get_settings().cost.currency


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
    # 可见范围(组 / 组员 / 每份文档授权给谁)。默认 None = 不接 = **不做可见范围**
    # (没有任何规则时本来就人人可见,所以"不接"与"接了但一条规则都没有"行为一致)。
    # `build_real_deps` 永远接上 —— 于是"生产路径上没有'忘了配权限'"这一档。
    access_store_factory: Callable[[], AccessStore] | None = None
    # 上传入库要用的三样,全部走工厂:**api/ 只许认识 Deps,不许认识适配器**
    # (那条分层法律也有 AST 检查)。切分器、装载函数、支持的后缀表都在适配器里。
    chunker_factory: Callable[[], Any] | None = None
    loader_factory: Callable[[], Any] | None = None
    suffixes_factory: Callable[[], tuple[str, ...]] | None = None
    # **磁盘索引**(上传目录 + 语料目录里的文件、大小、候选 doc_id)。清单拿它和登记表、
    # 向量库对账 —— "语料目录里新放的文件"只有这一处看得见。默认 None = 不看磁盘那一处
    # (多份老契约只关心登记表与库,不注入就不扫);`build_real_deps` 永远接上。
    disk_files_factory: Callable[[], Sequence[Any]] | None = None
    # 阈值:默认取配置(见 _default_faq_threshold),注入就按注入的来 ——
    # 验收脚本靠这个注入一个边界值直接测「命中判定」。
    faq_threshold: float = field(default_factory=_default_faq_threshold)
    # 缓存过期秒数。和 faq_threshold 一样是**静态标量**:默认取配置,也可以注入。
    # 放 Deps 而不是让 faq.py 自己 get_settings(),
    # 是为了让验收脚本能注入一个短 TTL 直接测过期。
    cache_ttl: int = field(default_factory=_default_cache_ttl)
    # 消耗报表的单价表与币种(见 config.CostConfig)。静态标量,理由同 cache_ttl:
    # 验收注入一份自己的单价就能直接测"算钱",不必改配置文件。
    cost_prices: dict[str, Any] = field(default_factory=_default_cost_prices)
    cost_currency: str = field(default_factory=_default_cost_currency)
    # 高峰/空闲的判定规则(见 config.CostConfig.peak + holidays)。默认规则 = 不分时段
    # (`` PeriodRule() `` 的窗口是空的):所有调用按 peak 价,报表也不报"时段不明"。
    cost_rule: PeriodRule = field(default_factory=_default_cost_rule)
    # 思考档位。**不是工厂**:它是个静态标签,唯一用途是让延迟账里能看出
    # 「这一轮按哪一档跑的」。命令层把它转交给 stream_rag / stream_question,用例与账本
    # 都不认识 config。默认 None → 账里是 '-'（注入式调用不该被编一个假档位)。
    thinking: str | None = None
    embedder_factory: Callable[[], Embedder] | None = None
    vector_store_factory: Callable[[], VectorStore] | None = None
    # 关键字那一路。它的语料也是从向量库取的(全量文本只有那一份),
    # 所以它跟 vector_store_factory 共享同一个实例 —— 见 build_real_deps 里的 lru_cache。
    # 走哪一档由 `keyword.backend` 决定:memory = 进程内 BM25(默认),
    # milvus = 稀疏向量检索(那档才需要 sparse_encoder_factory)。
    keyword_index_factory: Callable[[], KeywordIndex] | None = None
    # 稀疏向量编码器(keyword.backend=milvus 那一档)。**入库与检索共用同一个实例**:
    # 查询侧虽然不带 idf,但"分词口径 + 哈希维度"必须与写文档时逐字一致,
    # 共用实例让这件事不靠约定(见 adapters/bm25_sparse.py)。
    # 默认 None = 不接这一档(写入不带稀疏向量,检索仍走进程内 BM25)。
    sparse_encoder_factory: Callable[[], SparseEncoder] | None = None
    # 重排。**注意它和上面几个不一样:工厂存在 ≠ 每次都用** ——
    # 用不用由 config 的 rag.rerank 决定,而那个开关只在命令层读。
    # 这样「关掉重排」不会顺带把模型加载也省掉,语义干净:
    # 组合根负责「造得出来」,命令负责「这一次要不要用」。
    reranker_factory: Callable[[], Reranker] | None = None
    # 会话历史。带了 --session 才用得到,不注入就不造(同其它工厂)。
    conversation_store_factory: Callable[[], ConversationStore] | None = None
    # 多集合视角(列出集合、删掉一个集合)。**Milvus 特有** —— 端口 VectorStore 讲的是
    # "一个集合怎么读写",而"库里有哪几个、哪个该删"是运维动作(旧集合可能缺列不能写、
    # 新集合要建要删,这类事必须有接口,见 cli/commands/drop.py)。
    # 适配器在**组合根这一层**被包成两个函数,命令层不 import pymilvus ——
    # 与「api/ 只许认识 Deps」是同一条分层法律。
    collections_factory: Callable[[], Sequence[Any]] | None = None
    drop_collection_factory: Callable[[str], int] | None = None
    # 会话目录(前端左侧「我有哪些会话」)。与 conversation_store_factory 是**两张不同的表**
    # (归属表 + 对话表),所以是两个工厂:只问答的入口不必认识它。
    session_catalog_factory: Callable[[], SessionCatalog] | None = None
    # 限流计数器。默认 None = **不限流** —— 与其它工厂同一条口径:
    # "不注入就不造"。`build_real_deps` 永远接上(Redis 那一份),
    # 只有显式注入的验收/本机才可能是 None;契约里有一条盯着这件事。
    rate_limiter_factory: Callable[[], RateLimiter] | None = None
    # 查询改写(FAQ 未命中 → 向量检索之间那一步)。默认 None = **不改写** ——
    # 与 rate_limiter_factory 同一条口径,但这里的默认是**真的默认**:
    # `build_real_deps` 只在 `rewrite.enabled=true` 时才接上,因为这一步要多花
    # 一次 LLM 调用(见 config.RewriteConfig)。启用了但装配不起来时**不返回 None**,
    # 而是返回一个"原样检索"的实现 —— 那种情况下服务的可用性不变,只是改写没生效,
    # 所以必须在日志里喊一声(见 `_query_rewriter`)。
    query_rewriter_factory: Callable[[], QueryRewriter] | None = None


def build_real_deps() -> Deps:
    """真实装配:读一次 Settings,把全部工厂装好(数量别写进这句 —— 写死了会漂)。

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
    from project_rag.adapters.access_mysql import MySQLAccessStore
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import (
        index_files,
        ingest_roots,
        load_documents,
        supported_suffixes,
    )
    from project_rag.adapters.documents_mysql import MySQLDocumentStore
    from project_rag.adapters.ledger_mysql import MySQLLedgerStore
    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.adapters.milvus_store import (
        MilvusVectorStore,
        drop_collection,
        list_collections,
    )
    from project_rag.adapters.reranker_bge import BGEReranker
    from project_rag.adapters.sessions_mysql import MySQLSessionCatalog
    from project_rag.config import get_settings, project_path

    settings = get_settings()
    # 匹配器只造一个:工厂每次调用都返回同一个实例,它按语料指纹缓存的索引才有意义。
    # 两道覆盖率闸门从配置进来:它们是**命中口径**的一部分,写死在适配器里就等于
    # "调它要改代码"(口径与推导见 adapters/matcher_bm25.py)。
    matcher = BM25TextMatcher(
        min_query_coverage=settings.faq.min_query_coverage,
        min_entry_coverage=settings.faq.min_entry_coverage,
    )

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
        # sparse 那一列要不要建,由关键字那一路的档位决定(一个开关决定 schema,
        # 免得"配置说用稀疏检索、集合却没有那一列"这种两处各说一套的坏法)
        return MilvusVectorStore(
            settings.milvus, sparse=settings.keyword.backend == "milvus"
        )

    @lru_cache(maxsize=1)
    def _sparse_encoder() -> SparseEncoder:
        """关键字那一路的稀疏编码器。**只在这一档被调用**(memory 那档根本不会用到它)。"""
        return BM25SparseEncoder(k1=settings.keyword.bm25_k1, b=settings.keyword.bm25_b)

    @lru_cache(maxsize=1)
    def _embedder() -> Embedder:
        return _embedder_factory(settings)

    @lru_cache(maxsize=1)
    def _reranker() -> BGEReranker:
        note_build("reranker")  # 2.2G 模型
        return BGEReranker(settings.embedding)

    @lru_cache(maxsize=1)
    def _keyword_index() -> KeywordIndex:
        if settings.keyword.backend == "milvus":
            # 稀疏那一档:进程内**不再**需要全量语料与 jieba 索引(111MB/进程 + 冷启动 2.1s
            # 实测见 bm25_index.py),查询编码后交给 Milvus 的稀疏倒排索引。
            return MilvusSparseKeywordIndex(store=_vector_store(), encoder=_sparse_encoder())
        # 缓存只用来存「语料指纹的摘要」,让每查询那次 397.6ms 的 Milvus 往返
        # 变成一次 Redis GET(~0.1ms);缓存坏了自动退回"每查询读一次指纹",不影响正确性。
        return BM25KeywordIndex(
            _vector_store(),
            RedisCache(settings.redis),
            settings.cache.corpus_fp_ttl_s,
        )

    @lru_cache(maxsize=1)
    def _strategy_classifier():
        """查询改写的判定器。**进程级单例**:它抱着一份 BERT 权重(几百兆)。

        ``note_build`` 记在这里而不是适配器里:冷启动账本的调用方全是组合根
        (与嵌入 / 重排 / 向量库那三个同一套做法),适配器不该认识可观测层。
        """
        from project_rag.adapters.strategy_lora import LoraStrategyClassifier
        from project_rag.adapters.strategy_rule import RuleStrategyClassifier

        if settings.rewrite.classifier == "rule":
            return RuleStrategyClassifier()
        note_build("strategy_classifier")
        return LoraStrategyClassifier(
            adapter_dir=settings.rewrite.adapter_dir,
            model_dir=settings.rewrite.model_dir or None,
            device=settings.rewrite.device,
            max_length=settings.rewrite.max_length,
        )

    @lru_cache(maxsize=1)
    def _query_rewriter() -> QueryRewriter:
        """查询改写端口(进程级单例:改写用的模型客户端也常驻这一个)。"""
        from project_rag.adapters.llm_deepseek import DeepSeekLLM

        return build_query_rewriter(
            _strategy_classifier,
            lambda: DeepSeekLLM(settings.llm),
            settings.rewrite,
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
        access_store_factory=lambda: MySQLAccessStore(settings.mysql),
        chunker_factory=lambda: ChineseRecursiveChunker(
            settings.rag.parent_chunk_size,
            settings.rag.child_chunk_size,
            settings.rag.chunk_overlap,
        ),
        loader_factory=lambda: load_documents,
        suffixes_factory=supported_suffixes,
        # 磁盘索引(清单对账用的"磁盘那一处")。**每次调用扫一遍**,不做缓存:
        # 有人刚往语料目录里放了个文件,清单就该立刻看见它 —— 缓存一秒都是在制造
        # "我明明放进去了前端看不到"这类问题。目录不大,递归列一遍是毫秒级。
        disk_files_factory=lambda: index_files(
            ingest_roots(
                project_path(settings.api.uploads_dir), project_path(settings.api.corpus_dir)
            ),
            suffixes=supported_suffixes(),
        ),
        # 集合级运维(列现状 / 删集合本身)。这两个不碰模型、也不建连接 —— 客户端在**被调用时**才建。
        collections_factory=lambda: list_collections(settings.milvus),
        drop_collection_factory=lambda name: drop_collection(settings.milvus, name),
        # 知识库通道。BGEM3Embedder 会把 torch 拉起来(秒级 + 上 G 内存),
        # 所以它只在工厂**被调用**时才构造 —— ingest / search 之外用不到就不付这份钱。
        embedder_factory=_embedder,
        vector_store_factory=_vector_store,
        keyword_index_factory=_keyword_index,
        # **只在 milvus 那一档装配**:memory 那档的集合 schema 里根本没有 sparse_vector 列,
        # 装了它只会让写入路径带着稀疏向量去撞那一列(而且先白算一遍全库统计)。
        # 一个旋钮决定三件事:schema 要不要稀疏列 / 写入带不带稀疏向量 / 检索走哪条路。
        sparse_encoder_factory=(
            _sparse_encoder if settings.keyword.backend == "milvus" else None
        ),
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
        # 限流:默认 Redis(跨进程共享 / 重启不清零 / 时钟以 Redis 为准);
        # `enabled: false` 或 `backend: memory` 时退到单进程内存那一份,或干脆不接(不限流)。
        rate_limiter_factory=_rate_limiter(settings),
        # 查询改写:配置说开才接上。不接 = 检索直接用原话,与"没有这个功能"行为一致 ——
        # 所以关掉它不会改变任何已有读数,这也正是它能当消融开关的前提。
        query_rewriter_factory=_query_rewriter if settings.rewrite.enabled else None,
        faq_threshold=settings.faq.threshold,
        cache_ttl=settings.cache.ttl_s,
        cost_prices=settings.cost.prices(),
        cost_currency=settings.cost.currency,
        cost_rule=PeriodRule.of(
            weekdays=settings.cost.peak.weekdays,
            windows=settings.cost.peak.windows,
            holidays=settings.cost.holidays,
        ),
        thinking=settings.llm.thinking,
    )


def build_query_rewriter(
    classifier_factory: Callable[[], Any],
    llm_factory: Callable[[], Any],
    config: Any,
) -> QueryRewriter:
    """输入:分类器工厂 + LLM 工厂 + ``config.RewriteConfig``。

    行为:拼出「判策略 → 生成检索语句」这条链。
    输出:检索侧要的 ``QueryRewriter``(**永不抛**)。

    **装配失败退化成"原样检索",不往上抛**:分类器是可选件 —— 权重没同步、换了机器
    没有 GPU、适配器目录被清掉,这些都不该让整个问答答不出来。代价是"改写静默失效",
    所以这里必须 WARNING,而且每一条改写日志里都带着 ``decided_by=fallback`` 与原因,
    不会表现成"效果一般"。

    **为什么单独一个模块级函数**:失败路径也得能被验收直接跑一遍 ——
    埋在 ``build_real_deps`` 的闭包里就只能靠"改 config 再重载模块"去够它,
    而那种测法既慢又不稳。
    """
    from project_rag.application import PassthroughRewriter, PlannedRewriter

    try:
        classifier = classifier_factory()
        llm = llm_factory()
    except Exception as exc:  # noqa: BLE001 - 见 docstring:可选件坏掉不该拖垮问答
        _logger.warning(
            "查询改写的分类器装配失败(%s: %s),本进程内改写退化为**原样检索** —— "
            "检查 rewrite.adapter_dir / model_dir / device,"
            "并确认 `python tools/lora/infer.py --test` 能跑通",
            type(exc).__name__,
            exc,
        )
        return PassthroughRewriter(reason=f"分类器装配失败:{type(exc).__name__}")

    return PlannedRewriter(
        classifier=classifier,
        llm=llm,
        max_subqueries=config.max_subqueries,
        max_query_chars=config.max_query_chars,
        confidence_threshold=config.confidence_threshold,
    )


def _embedder_factory(settings) -> Embedder:
    """输入:配置。行为:按 `embedding.backend` 造嵌入器。输出:实现 `Embedder` 的实例。

    两档，**默认必须是真的语义模型**：

    * ``bge_m3``（默认）—— BGE-M3，1024 维，2G 模型；`note_build("embedder")` 记在**这里**，
      因为"冷"这个词只在真要加载模型时才成立（哈希实现零成本，不该记一笔冷启动）；
    * ``hashing`` —— 字符 bigram 哈希，256 维、零模型、确定性。**它不是语义模型**：
      只保证"文本越像、向量越近"（见 `adapters/hashing_embedder.py` 的 docstring）。
      它存在的唯一理由是让"没有 4.4G 模型"的机器也能跑通 ingest→search→界面
      （演示与验收），所以这里**必须打一条 WARNING** —— 否则使用者会以为检索"正常"，
      而拿到的全是字面匹配。
    """
    if settings.embedding.backend == "hashing":
        from project_rag.adapters.hashing_embedder import HashingEmbedder

        _logger.warning(
            "嵌入用的是**演示实现**（embedding.backend=hashing）：字符 bigram 哈希，"
            "只按字面相似度检索，不是语义模型。要真语义请设回 bge_m3 并准备好 models/bge-m3。"
        )
        return HashingEmbedder()
    from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder

    note_build("embedder")  # 2G 模型:账本里"冷"最主要的那一笔
    return BGEM3Embedder(settings.embedding)


def _rate_limiter(settings) -> Callable[[], RateLimiter] | None:
    """输入:配置。行为:按 `api.rate_limit` 选一个限流实现(或 None = 不限流)。输出:工厂或 None。

    三档配置对应三种部署:`redis`(默认,共享计数)/ `memory`(单进程)/ `enabled: false`(不要这道闸)。
    **`enabled` 在这里落地** —— 配置里写了它就得真的生效,否则那是一行骗人的注释。
    """
    limit_config = settings.api.rate_limit
    if not limit_config.enabled:
        _logger.warning("限流已关闭(api.rate_limit.enabled=false):问答/上传/登录都不设闸")
        return None
    if limit_config.backend == "memory":
        from project_rag.adapters.ratelimit_memory import MemoryRateLimiter

        _logger.warning(
            "限流用进程内存计数(api.rate_limit.backend=memory):"
            "重启即清零、多 worker 各算各的 —— 只适合单进程部署"
        )
        return lambda: MemoryRateLimiter()
    if limit_config.backend != "redis":
        raise ValueError(
            f"不认识的限流后端 {limit_config.backend!r};只支持 redis / memory"
        )

    from project_rag.adapters.ratelimit_redis import RedisRateLimiter

    return lambda: RedisRateLimiter(settings.redis, key_prefix=limit_config.key_prefix)


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
) -> KnowledgeChannel:
    """把知识库通道的端口拼成**一次调用的函数**(不调用它就什么都不构造)。

    输入:依赖集合、问题、检索旋钮、要不要重排
    行为:返回一个闭包;闭包被调用时才去调那几个惰性工厂(向量库/嵌入/BM25/重排)
    输出:(历史轮次, 这一轮的账本) -> 帧序列 —— 历史由用例层读出来再交进来,
          账本同理(见 ``application.answer_question.KnowledgeChannel``),组合根不碰会话存储

    为什么要有它:合流入口(``qa`` 命令、FastAPI)都从这里拿「知识库通道」——
    怎么拼只写一处,省得某条入口漏传了 source 或检索旋钮(三条增强检索路径都漏传过
    source_filter)。放在组合根而不是用例里,是因为「怎么造、什么时候造」
    正是组合根的职责;用例只该知道「有个函数能给我答案」。**惰性也在这里落地**:
    ``qa`` 命中 FAQ 时这个闭包一次都不会被调用,嵌入/重排模型也就不会被加载。
    """

    def call(history: Sequence[ConversationTurn], timing: StreamTiming) -> Iterator[QaEvent]:
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
            # 账本**交下去**:合流入口已经开了这一轮的账,检索/生成的打点记在同一行上,
            # 否则一次请求会落两行账(报表的请求量因此翻倍)。
            timing=timing,
            # 查询改写正好落在这一步之前:这个闭包**只在 FAQ 未命中时**被调用,
            # 所以「FAQ 未命中 → 改写 → 向量检索」是结构上的顺序,
            # 不靠调用方记得先改写(而 FAQ 命中那条路一次都不会碰到它)。
            # 没装配(= 配置没开)就是 None,检索直接用原话。
            rewriter=deps.query_rewriter_factory()
            if deps.query_rewriter_factory is not None
            else None,
        )

    return call


def ensure_access_schema(deps: Deps) -> str | None:
    """输入:Deps。行为:确保可见范围那三张表存在(**幂等**)。输出:失败原因;成功给 None。

    **为什么失败只记一条告警、不拦启动**:没有 MySQL 时连登录都过不去
    (令牌要 ``JOIN users`` 才换得出人),所以"建不了这几张表"不会变成一次静默放行 ——
    它只会让管理员页在 MySQL 恢复前用不了。反过来把服务拦在启动阶段,
    等于"权限表没建好,整套问答都用不了",那是拿一个次要功能绑架主功能。

    **查询期不这样做**:真正读规则失败时接口回 503(见 ``api/app.py`` 的 ``_access``),
    因为那时已经有人在用这套权限了 —— 读不到就放行才是真的危险。
    """
    if deps.access_store_factory is None:
        return None
    try:
        deps.access_store_factory().init_schema()
    except Exception as e:  # noqa: BLE001 - 见上:建表失败不该拦启动
        return f"{type(e).__name__}: {e}"
    return None


def ensure_documents_schema(deps: Deps) -> str | None:
    """输入:Deps。行为:确保登记表存在(**幂等**)。输出:失败原因;成功给 None。

    **为什么在启动时建**:这张表此前**没有任何生产入口去建它**(只有验收脚本与工具碰过
    ``init_schema``)—— 空库起来以后"上传/清单"直接 500,而 500 的那句话是 PyMySQL 的
    ``Table 'rag.documents' doesn't exist``,除了开发者没人看得懂。顺带它也把老表的排序规则
    扳回来(见 ``adapters/mysql_schema.py``:那张表与可见范围表按 ``document_id`` 相关)。

    与另外两个 ``ensure_*`` 同一条政策:失败只记警告,不拦启动。
    """
    if deps.document_store_factory is None:  # 注入式部署(验收/假件):不碰真库
        return None
    try:
        deps.document_store_factory().init_schema()
    except Exception as e:  # noqa: BLE001 - 见上:建表失败不该拦启动
        return f"{type(e).__name__}: {e}"
    return None


def ensure_auth_schema(deps: Deps) -> str | None:
    """输入:Deps。行为:确保认证那三张表存在(**幂等**)。输出:失败原因;成功给 None。

    **为什么在启动时建**(原来只由 ``project-rag user add`` 建):

    ① ``session_owner`` 是**会话列表要 JOIN 的那张表**,它的排序规则也得跟着扳到当前口径 ——
       老库里它只写了 ``CHARSET``,与 ``conversations`` JOIN 直接报 1267,表现是"整页会话读不出来"
       (见 ``adapters/mysql_schema.py``)。这条自愈写在 ``init_schema`` 里,而那个入口以前只有
       ``user add`` 会走 —— 也就是说,**恰恰是"已经有账号、只想看会话"的那种部署修不到**;
    ② 空库启动时登录页不再是"500 直到你想起跑一条 CLI 命令"。

    与 ``ensure_access_schema`` 同一条政策:失败只记警告、不拦启动(真正读不到时接口回 503)。
    """
    if deps.auth_store_factory is None:  # 注入式部署(验收/假件):不碰真库
        return None
    try:
        deps.auth_store_factory().init_schema()
    except Exception as e:  # noqa: BLE001 - 见上:建表失败不该拦启动
        return f"{type(e).__name__}: {e}"
    return None


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

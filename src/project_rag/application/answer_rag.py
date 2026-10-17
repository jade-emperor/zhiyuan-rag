"""
用例:知识库问答(两路检索 → 融合 → 还原父块 → 重排 → 拼上下文 → 生成带引用的回答)。

只认识端口 —— 换嵌入模型 / 换向量库 / 换关键字索引 / 换重排模型 / 换 LLM,
这个文件一行都不改。

五条刻意的设计:

1. **检索为空时不调 LLM**:没有资料还让模型"自由发挥",正是幻觉的源头。
   直接返回拒答(``refused=True``),既省一次调用,又不会编。
2. **两路融合在这里做**:两路分数不可比,融合是决策不是管道(见 fusion.py)。
   只给向量库不给关键字索引时**退化成单路,且不做融合** —— 单路时直接沿用那一路的分数,
   这样「检索分」的口径不会因为有没有第二路而变。
3. **命中子块之后按父块折叠**:父子分层的意义就是「多召回子块、少给父块」。
   同一个父块的三个子块都被命中时,不折叠就等于把几乎一样的上下文喂三遍。
4. **重排放在折叠之后、截断之前**(顺序就是这一步的全部):

   - 折叠之后 → 先去掉重复的子块,不做重复功;
   - 截断之前 → 重排要在**全部候选**里挑,先截断就只剩一小撮可选,重排白做;
   - 喂进去的是 ``context_text``(父块正文)→ 分数对应的正好是模型会读到的那段文字,
     引用清单上的分数才核对得上。

5. **引用跟着答案一起返回**:``[1][2]`` 是给人核对用的;``routes`` 还告诉你
   这一条是单路捞到的还是两路都捞到了 —— 两路都命中通常更可信。
6. **改写只发生在检索这一侧**(``rewriter``,默认 None = 不改写):它把用户原话规划成
   一组检索语句(直接检索 / 假设答案 / 子查询分解 / 去噪改写),但**回答的仍然是原话**。
   合流通道里这段天然落在「FAQ 未命中 → 向量检索」之间 —— FAQ 命中时压根走不到这里。
   改写失败一律退回原话(见 ``application/query_rewrite.py``):它是锦上添花,
   不该成为「答不出来」的新原因。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from project_rag.domain import (
    ChatMessage,
    Citation,
    ConversationTurn,
    QaEvent,
    FusedChunk,
    RagAnswer,
    RetrievalOptions,
    RetrievedChunk,
)
from project_rag.observability import StreamTiming, get_logger
from project_rag.ports import (
    Embedder,
    KeywordIndex,
    LLMClient,
    QueryRewriter,
    Reranker,
    VectorStore,
)

# 同包用相对导入:项目的分层扫描规则把「绝对导入 project_rag.application.*」
# 当成跨层依赖拦下来(它只看模块前缀),相对导入才表达得出「同一个包内」
from .fusion import KEYWORD_ROUTE, VECTOR_ROUTE, fuse_rankings
from .streaming import done_of, timed
from .prompts import rag_answer_prompt

_logger = get_logger("application.answer_rag")

# 检索为空(或模型返回空)时的兜底话术。
REFUSAL_TEXT = "信息不足,无法回答,请联系人工客服。"

# 有文档被禁止时每路多要几倍的候选(理由见 ``retrieve`` 里那段注释)。
_DENIED_OVERFETCH = 4
# 多要的上界:调用方把 k 开到几百时,再乘 4 会把整个库拉回来做一次无谓的过滤。
_DENIED_MAX_K = 200


def _visible(
    hits: Sequence[RetrievedChunk], denied: set[str], k: int
) -> tuple[RetrievedChunk, ...]:
    """输入:一路的命中、禁止名单、要几条。行为:滤掉被禁文档,再截到 k 条。输出:可见的命中。

    **截断放在过滤之后**:先截再滤的话,k 条里被禁几条就少几条 —— 而那正是"多要几倍"
    要解决的问题。两者顺序反过来,多要的候选全白要了。
    """
    return tuple(hit for hit in hits if hit.chunk.doc_id not in denied)[:k]


def _renumber(citations: Iterable[Citation]) -> tuple[Citation, ...]:
    """输入:任意引用序列。行为:按当前顺序重新从 1 编号。输出:新引用元组。

    编号只由这里产生:融合、折叠、重排、截断之后都必须重新编号,
    否则答案里的 [3] 会指向一个已经被折叠掉的子块。
    """
    return tuple(
        Citation(
            index=index,
            chunk=citation.chunk,
            score=citation.score,
            routes=citation.routes,
            rerank_score=citation.rerank_score,
        )
        for index, citation in enumerate(citations, start=1)
    )


def collapse_parents(citations: Sequence[Citation]) -> tuple[Citation, ...]:
    """输入:子块命中(同一个父块可能命中好几条)。行为:按父块去重,每组留分数最高的那条。输出:重新编号的引用。

    没有父块的块(``parent_id`` 为空)按 ``chunk_id`` 自成一组,
    所以这个函数对「还没上父子分层」的旧数据也是安全的。
    """
    best: dict[str, Citation] = {}
    for citation in citations:
        chunk = citation.chunk
        key = chunk.parent_id or chunk.chunk_id
        current = best.get(key)
        if current is None or citation.score > current.score:
            best[key] = citation
    # 组内已经取了最高分,组间按这个最高分降序 —— 和单路检索的排序口径保持一致
    ordered = sorted(best.values(), key=lambda item: item.score, reverse=True)
    return _renumber(ordered)


def rerank_citations(
    query: str,
    citations: Sequence[Citation],
    *,
    reranker: Reranker,
    rerank_input: Literal["parent", "child"] = "parent",
) -> tuple[Citation, ...]:
    """输入:问题 + 当前引用序列 + 重排端口 + 喂哪段正文。行为:按重排分重新排序。输出:重新编号的引用。

    默认喂 ``chunk.context_text``(父块正文)—— **模型会读到的那段正文**,不是子块。
    这样引用清单上的重排分才对应得到模型的实际输入。

    ``rerank_input="child"`` 改成喂检索命中的那个**子块**:更短、更聚焦,长文档上通常更准,
    代价是「重排分」对应的文字与模型读到的不是同一段(那个分数就只能当排序依据看)。
    这条旋钮是为 P0-4 那组 A/B 加的,**默认不动** —— 换默认要等读数说话。

    三条边界,每条都有理由:

    - **空序列直接返回**,不调重排器:没有候选还跑一次模型前向是白花钱。
    - **只有 1 条候选也直接返回**,同样不调:一条候选的排序不可能变。
      代价是这时 ``rerank_score`` 留空 —— 没算过就不写数,别让人以为「重排给了它 0 分」。
    - **回来的分数条数不符当场抛错**:zip 会把多出来的候选静默丢掉,
      表现成「有几条候选凭空消失」,比直接报错难查得多。
    """
    if len(citations) < 2:
        return _renumber(citations)

    texts = [
        item.chunk.text if rerank_input == "child" else item.chunk.context_text
        for item in citations
    ]
    scores = reranker.rerank(query, texts)
    if len(scores) != len(citations):
        raise ValueError(
            f"重排器回了 {len(scores)} 个分数,候选却有 {len(citations)} 条 —— "
            "这个端口的约定是同序等长"
        )
    # 同分按 chunk_id 兜底:顺序稳定,验收才写得死断言
    ranked = sorted(
        zip(citations, scores),
        key=lambda pair: (-float(pair[1]), pair[0].chunk.chunk_id),
    )
    return _renumber(
        Citation(
            index=0,
            chunk=item.chunk,
            score=item.score,
            routes=item.routes,
            rerank_score=float(score),
        )
        for item, score in ranked
    )


def _noop(name: str) -> None:
    """输入:打点名。行为:什么都不做。输出:无。

    ``retrieve()`` 也可能被不带账本的调用方用(验收脚本、一次性工具),
    所以打点必须能空转 —— 不然每个调用点都要写一遍 ``if timing is not None``。
    """
    return None


def _noop_span(name: str, ms: float) -> None:
    """输入:段名 + 毫秒。行为:什么都不做。输出:无(理由同 ``_noop``)。"""
    return None


def _elapsed_ms(started: float) -> float:
    """输入:起跑时刻(``time.perf_counter()``)。行为:算到现在的毫秒。输出:毫秒。"""
    return (time.perf_counter() - started) * 1000.0


@dataclass(slots=True)
class _Leg:
    """子线程里那一路召回的收成:命中 + 它自己的耗时(毫秒) + 抛出的异常(没抛就是 None)。

    **异常必须存在这里带回来**:线程里抛的异常没人接(``Thread.join`` 只等人,不传异常),
    默认行为是往 stderr 打一条栈然后主线程当没事发生 —— 那会变成"关键字那一路静默消失",
    检索结果少一半而账上一切正常,是最难查的那种坏法。
    """

    hits: Sequence[RetrievedChunk] = field(default_factory=tuple)
    ms: float = 0.0
    error: BaseException | None = None


def _recall_keyword(
    leg: _Leg,
    keyword_index: KeywordIndex,
    query: str,
    *,
    k: int,
    source: str | None,
) -> None:
    """输入:待填的收成 + 关键字索引 + 检索语句 + 条数 + 来源过滤。行为:在**子线程**里跑关键字那一路。输出:无(结果写进 ``leg``)。"""
    started = time.perf_counter()
    try:
        leg.hits = keyword_index.search(query, k=k, source=source)
    except BaseException as exc:  # noqa: BLE001 —— 原样带回去由主线程重抛,不在这里吞、也不在这里包装
        leg.error = exc
    finally:
        leg.ms = _elapsed_ms(started)


def _gather(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    keyword_index: KeywordIndex | None,
    options: RetrievalOptions,
    mark: Callable[[str], None],
    span: Callable[[str, float], None],
) -> tuple[Citation, ...]:
    """输入:一句检索语句 + 端口 + 旋钮 + 打点/分段函数。

    行为:跑有索引的那几路(**互不依赖的那两路并行**) → 融合(单路则跳过) → 折叠父块 → 重新编号。
    输出:引用序列(已折叠,**未重排、未截断**);没检索到给空序列。

    **两路为什么并行**:它们是同一个问题的两种捞法,谁也不等谁的结果(稠密那路要嵌入,
    关键字那路自己分词),融合本来就要等两路都到手 —— 串着跑纯属白等。真栈上
    「稠密 425ms + 关键字 200ms」叠起来 ≈ 425ms。关键字的活主要在等 Milvus,
    重建索引那段又是纯 CPU,两者不抢同一件资源。

    **权重 ≤ 0 的那一路不跑**(连检索都不做,不只是融合时跳过)。两个权重同时 ≤ 0 时
    结果是空序列 —— 而"什么都没检索"与"检索了但没命中"在这一层看不出区别,
    所以这种组合要由**边界**拦住(HTTP 出参层直接 422)。

    **为什么从 ``retrieve`` 里拆出来**:「子查询分解」把一句话拆成几句各检索一遍,
    那几句必须**各自召回、合并之后再统一重排**。重排塞在循环里的话每句各截断一次,
    合并时已经没有候选可挑了 —— 而重排的全部意义就是"在全部候选里挑"。
    """

    # 可见范围(见 application/access.py):被禁止的文档**不许出现在任何一条路上**。
    # 过滤放在这里(两路结果汇齐之后、融合之前)而不是各家适配器里,是因为"谁能看什么"
    # 是用例层的规矩,不是存储引擎的:三份适配器各写一遍过滤,迟早有一份忘了 ——
    # 而漏掉的那一路会把受限文档照常召回来。
    denied = set(options.denied_docs)
    # 禁止名单非空时**多要几条再筛**:库里取的是"最像的 k 条",其中若干条被禁之后
    # 剩下的不足 k,而调用方是按 k 理解结果的(少了几条它不知道)。倍数按"被禁比例"给:
    # 4 倍能容忍约 3/4 的命中落在被禁文档里;上界防止 k 很大时白拉一整库。
    ask = options.k if not denied else min(options.k * _DENIED_OVERFETCH, _DENIED_MAX_K)

    # 查询嵌入与向量检索拆成两句:写成一句的话,「嵌入多久」与「Milvus 多久」就永远
    # 分不开了 —— 而这两件事的优化手段完全不同(换 device / 换索引 / 加缓存)
    #
    # **权重 ≤ 0 = 明确关掉这一路**(融合层本来就会跳过它,见 ``fusion.fuse_rankings``),
    # 所以这里连检索都不做:白跑一趟再丢掉,账本上的 ``embed`` / ``vector`` / ``bm25``
    # 三段就都在骗人。前端「只用向量 / 只用关键字」这两个档就是靠它表达的。
    # 「有没有查询向量」就是「稠密那一路跑不跑」:一次嵌入只为这一路服务,
    # 关掉那一路时连嵌入都不做(理由见上)。
    query_vector = embedder.embed([query])[0] if options.vector_weight > 0 else None
    mark("embed")

    rankings: dict[str, Sequence[RetrievedChunk]] = {}
    vector_ms: float | None = None
    keyword_ms: float | None = None

    # 两路召回**互不依赖**(稠密那路要嵌入,关键字那路自己分词),所以能并行。
    # 真栈 A/B(真语料、同一个 store 实例、各 5 次取中位):串行 稠密 398ms + 关键字 399ms
    # = 797ms → 并行墙钟 **400ms**。省下的正好是"较快那一路"的时间,所以它随当次的一致性等待
    # 落在 200~400ms(= 一次真问答墙钟的 10~20%),而且一个口径都不用动:
    # 两路照样先汇齐、再融合(融合要的正是"两路都在手上")。
    #
    # 谁进子线程:关键字那一路。因为它可能触发一次 BM25 重建(``load_all`` + 分词 ≈ 1.5s,
    # 纯 CPU 且按片持有 GIL),而稠密那一路绝大部分时间在等 Milvus 的网络往返(放 GIL)——
    # 这样"CPU 那段"与"等待那段"是重叠的,不是抢的。
    #
    # **两路共用同一个 MilvusVectorStore**(``deps`` 里 `_vector_store()` 是 `lru_cache`),
    # 也就是同一个 pymilvus 客户端被两个线程同时用 —— 上面那次 A/B 逐条比对过结果,
    # 5 轮并发没有异常(并行化真正的隐含前提是这条,不是"代码看起来能跑")。
    #
    # 代价说清楚:每个在跑的检索多占**一条原生线程**(不是 anyio 池里的,池子的上限不受影响),
    # 上界 = 同时在跑的检索数。只有一路在跑时**不建线程**(见下面的 else),那条路上的
    # 行为与并行之前逐字一样。
    if query_vector is not None and keyword_index is not None and options.bm25_weight > 0:
        leg = _Leg()
        worker = threading.Thread(
            target=_recall_keyword,
            args=(leg, keyword_index, query),
            kwargs={"k": ask, "source": options.source},
            name="rag-keyword-recall",
            # 守护线程:它只是这一次检索的一部分,绝不该拖住进程退出
            daemon=True,
        )
        worker.start()
        started = time.perf_counter()
        failure: BaseException | None = None
        try:
            rankings[VECTOR_ROUTE] = store.search(
                query_vector, k=ask, source=options.source
            )
        except BaseException as exc:  # noqa: BLE001 —— 先收线再重抛,见下面的 join
            failure = exc
        finally:
            vector_ms = _elapsed_ms(started)
            # 本线程这一路炸了也要先把子线程收回来:留一条野线程继续跑,它的结果会晚一步
            # 落在**下一次**请求的账旁边(而且下一次请求可能已经在用同一份索引了)
            worker.join()
        if failure is not None and leg.error is not None:
            # 两路都炸:只能抛一个。另一个必须留痕 —— 静默丢掉的那个迟早变成"偶发少召回"
            _logger.warning(
                "两路召回都抛了异常;关键字那一路的会被丢掉:"
                f"{type(leg.error).__name__}: {leg.error}"
            )
        if failure is not None:
            raise failure
        if leg.error is not None:
            raise leg.error
        rankings[KEYWORD_ROUTE] = leg.hits
        keyword_ms = leg.ms
    else:
        if query_vector is not None:
            started = time.perf_counter()
            rankings[VECTOR_ROUTE] = store.search(
                query_vector, k=ask, source=options.source
            )
            vector_ms = _elapsed_ms(started)
        if keyword_index is not None and options.bm25_weight > 0:
            started = time.perf_counter()
            rankings[KEYWORD_ROUTE] = keyword_index.search(query, k=ask, source=options.source)
            keyword_ms = _elapsed_ms(started)

    # 分段值由**各路自己报**(账户口径见 ``StreamTiming.span``):并行时相邻打点之差
    # 量不出谁花了多久。下面两条打点仍在召回段结束时落下,只负责让融合那几段的差值接得上 ——
    # 融合的起点因此是"两路都到手"那一刻,而不是先结束的那一路。
    if vector_ms is not None:
        span("vector", vector_ms)
        mark("vector")
    if keyword_ms is not None:
        span("bm25", keyword_ms)
    mark("bm25")
    if denied:
        rankings = {
            route: _visible(hits, denied, options.k) for route, hits in rankings.items()
        }

    if len(rankings) == 1:
        # 单路:不做融合(也没有第二路可比)。**路名从剩下的那一路取** ——
        # 写死 VECTOR_ROUTE 的话,「只用关键字」会把引用标成 vector 那一路(静默错标)。
        only_route, only_hits = next(iter(rankings.items()))
        fused: tuple[FusedChunk, ...] = tuple(
            FusedChunk(chunk=hit.chunk, score=hit.score, routes=(only_route,))
            for hit in only_hits
        )
    else:
        fused = fuse_rankings(
            rankings,
            mode=options.fusion,
            weights={
                VECTOR_ROUTE: options.vector_weight,
                KEYWORD_ROUTE: options.bm25_weight,
            },
            rrf_k=options.rrf_k,
        )
    mark("fuse")

    citations = _renumber(
        Citation(index=0, chunk=item.chunk, score=item.score, routes=item.routes)
        for item in fused
    )
    collapsed = collapse_parents(citations)
    mark("collapse")
    return collapsed


def _finish(
    rerank_query: str,
    citations: tuple[Citation, ...],
    *,
    reranker: Reranker | None,
    options: RetrievalOptions,
    mark: Callable[[str], None],
) -> tuple[Citation, ...]:
    """输入:重排用的那句原话 + 候选 + 重排器 + 旋钮 + 打点函数。

    行为:重排(给了重排器才做) → 截断到 ``max_context``。
    输出:最终引用序列。

    **重排开不开由「有没有传重排器」表达**,不另设开关 —— 和 ``keyword_index=None``
    表示「没有 BM25 那一路」是同一个道理。两处都能表达就有两个真值来源:
    options 说不要重排、却又传了重排器时,谁赢?
    """
    if reranker is not None:
        citations = rerank_citations(
            rerank_query, citations, reranker=reranker, rerank_input=options.rerank_input
        )
        # 只在真装了重排器时打点:没装的时候这一格留 '-' 而不是 0.0 ——
        # 「没做」和「很快」在排查时是两件事
        mark("rerank")
    if options.max_context is not None and len(citations) > options.max_context:
        citations = _renumber(citations[: options.max_context])
    return citations


def retrieve(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    keyword_index: KeywordIndex | None = None,
    reranker: Reranker | None = None,
    options: RetrievalOptions | None = None,
    timing: StreamTiming | None = None,
) -> tuple[Citation, ...]:
    """输入:问题 + 向量库 + 可选的关键字索引 + 可选的重排器 + 检索旋钮 + 可选计时账本。

    行为:跑有索引的那几路 → 融合(单路则跳过) → 折叠父块 → 重排(给了重排器才做)
          → 截断到 max_context。
    输出:引用序列;没检索到给空序列。

    ``timing`` 给了就在六处打点(嵌入 / 向量检索 / 关键字 / 融合 / 折叠 / 重排):
    「检索一共 1.8 秒」是一回事,「这 1.8 秒花在哪」是另一回事 —— 没有分段就只能猜。
    不给就空转(验收与脚本常常不需要账本)。

    这是**单句检索**的入口。开了查询改写之后,真正被调的是 ``retrieve_many``
    (它可能带回来好几句);``retrieve_many([q])`` 与 ``retrieve(q)`` 逐字等价。

    ``vector`` / ``bm25`` 那两格走 ``span``(各路自己报自己的时长)而不是打点之差 ——
    这两路是**并行**跑的,见 ``_gather``。
    """
    options = options or RetrievalOptions()
    mark = timing.mark if timing is not None else _noop
    span = timing.span if timing is not None else _noop_span
    citations = _gather(
        query,
        embedder=embedder,
        store=store,
        keyword_index=keyword_index,
        options=options,
        mark=mark,
        span=span,
    )
    return _finish(query, citations, reranker=reranker, options=options, mark=mark)


def retrieve_many(
    queries: Sequence[str],
    *,
    embedder: Embedder,
    store: VectorStore,
    keyword_index: KeywordIndex | None = None,
    reranker: Reranker | None = None,
    options: RetrievalOptions | None = None,
    timing: StreamTiming | None = None,
    rerank_query: str | None = None,
) -> tuple[Citation, ...]:
    """输入:一句或多句检索语句 + 端口 + 旋钮 + 可选账本 + 可选的重排用原话。

    行为:每句各跑一遍召回→融合→折叠 → 按 ``chunk_id`` 合并(同一条留最高分)→
          用 ``rerank_query``(默认第一句)重排一次 → 截断。
    输出:引用序列;全都召不到给空序列。

    ``retrieve_many([q])`` 与 ``retrieve(q)`` **逐字等价**(两句走的是同一对
    ``_gather``/``_finish``,不是"看起来差不多"的两段实现)—— 这条恒等式是刻意的:
    开不开改写,不该改变"这一句检索本来会召回什么"。

    **合并为什么按 chunk_id 取最大分,而不是把这 N 句当成 N 路做 RRF**:
    「子查询分解」拆出来的 N 句是**不同的问题**,各自的融合分不在一个量纲上
    (第二句问的是另一个子话题,它的第 1 名未必比第一句的第 3 名更相关)。
    真正的可比性由**重排**给:``rerank_query`` 传的是**用户原话**,所有候选在同一把
    尺子下重排一次 —— 于是排序既照顾了"各子问题都召回到",又回到"用户到底问了什么"。
    合并只负责去重(N 句常常都召回到同一条,留两条会让同一个父块在上下文里出现两遍)。

    **代价说清楚**:没装重排器时最终顺序就是"各句分数直接比大小",那是近似的。
    日志里记了这次是几句召回,排查时对得上。

    **六段子打点仍只记第一句**(``mark`` 只认第一次到达,``span`` 同理):所以 N>1 时它们
    不再与 ``retrieve_ms`` 相加相等 —— 两路召回并行之后,那两格本来就是"各自花了多久",
    N>1 时也只是各自取第一句的那一份。这是刻意的 —— "第一句花在哪"比"N 句的平均"更能
    解释问题,而"这次是 N 句"在日志里有。
    """
    cleaned = [one for one in (queries or ()) if one and one.strip()]
    if not cleaned:
        # 空 = 没有可检索的句子。这里**不猜**(不拿原话顶上):调用方给的是一组检索语句,
        # 它给了空表就是它那边出了问题,猜一句去检索只会把问题埋起来。
        return ()
    options = options or RetrievalOptions()
    mark = timing.mark if timing is not None else _noop
    span = timing.span if timing is not None else _noop_span

    best: dict[str, Citation] = {}
    for one in cleaned:
        for citation in _gather(
            one,
            embedder=embedder,
            store=store,
            keyword_index=keyword_index,
            options=options,
            mark=mark,
            span=span,
        ):
            current = best.get(citation.chunk.chunk_id)
            if current is None:
                best[citation.chunk.chunk_id] = citation
                continue
            best[citation.chunk.chunk_id] = Citation(
                index=0,
                chunk=citation.chunk,
                score=max(current.score, citation.score),
                # routes 取并集:哪几句捞到了它是条线索(和"两路都命中"同一个用法)
                routes=tuple(dict.fromkeys(current.routes + citation.routes)),
                rerank_score=current.rerank_score or citation.rerank_score,
            )
    # 同分按 chunk_id 兜底:顺序稳定,验收才写得死断言(与 rerank_citations 同一套)
    merged = _renumber(
        sorted(best.values(), key=lambda item: (-item.score, item.chunk.chunk_id))
    )
    if len(cleaned) > 1:
        _logger.info(
            "多句检索:%d 句 → 去重后 %d 条候选(%s)",
            len(cleaned),
            len(merged),
            " | ".join(cleaned),
        )
    return _finish(
        rerank_query or cleaned[0],
        merged,
        reranker=reranker,
        options=options,
        mark=mark,
    )


def stream_rag(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    llm: LLMClient,
    keyword_index: KeywordIndex | None = None,
    reranker: Reranker | None = None,
    options: RetrievalOptions | None = None,
    history: Sequence[ConversationTurn] = (),
    thinking: str | None = None,
    rewriter: QueryRewriter | None = None,
    timing: StreamTiming | None = None,
) -> Iterator[QaEvent]:
    """输入/输出同 ``_rag_frames``,额外做一件事:**计时并记一条 latency 日志**。

    TTFT / 吐字速率只有在「边界」上才看得到 —— 管道自己不知道「什么时候交出去」。
    这一层只观察:帧的内容、顺序、条数一个字节都不改(验收拿未包装的流逐帧比对过)。
    账本字段与口径见 ``observability/latency.py``。

    ``timing`` 给了就**用别人的账本**:合流入口(``stream_question``)是"一次请求"的那一层,
    它把自己的账本交下来,检索/生成的打点记在同一行上 —— 一次请求只落一行账。
    这时这里**不再包 ``timed()``**(再包一层会把每个 token 数两遍,而且 ``emit`` 也不是
    这一层该做的事)。不给就自己开一条(直调知识库通道的调用方,例如 ``rag`` 命令)。

    ``thinking`` 是**给日志用的静态标签**(配置里的档位,组合根从 config 读出来交进来):
    用例层不认识 config,账本更不认识 —— 账本只负责渲染。默认 None → 账里是 ``-``,
    于是注入式调用(验收、直调)不会被编一个假档位出来。

    ``rewriter`` = 查询改写端口,**默认 None = 不改写**。合流通道里它只会在这条路上
    被用到(FAQ 命中时压根不会走到 ``stream_rag``),所以「FAQ 未命中 → 改写 →
    向量检索」这个顺序是**结构上**成立的,不靠调用方记得先调改写。
    """
    if timing is not None:
        yield from _rag_frames(
            query,
            embedder=embedder,
            store=store,
            llm=llm,
            keyword_index=keyword_index,
            reranker=reranker,
            options=options,
            history=history,
            timing=timing,
            rewriter=rewriter,
        )
        return
    own = StreamTiming(label="rag", channel="rag", thinking=thinking)
    yield from timed(
        _rag_frames(
            query,
            embedder=embedder,
            store=store,
            llm=llm,
            keyword_index=keyword_index,
            reranker=reranker,
            options=options,
            history=history,
            timing=own,
            rewriter=rewriter,
        ),
        own,
    )


def _rag_frames(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    llm: LLMClient,
    keyword_index: KeywordIndex | None = None,
    reranker: Reranker | None = None,
    options: RetrievalOptions | None = None,
    history: Sequence[ConversationTurn] = (),
    timing: StreamTiming,
    rewriter: QueryRewriter | None = None,
) -> Iterator[QaEvent]:
    """输入:问题 + 四个端口 + 可选的关键字索引/重排器 + 检索旋钮 + 可选的历史轮次 + 计时账本
    + 可选的查询改写端口。

    行为:改写(配了才做)→ 检索并还原父块 → 有资料就先发一帧 sources(前端可以先渲染来源),
          再逐帧发 token;没资料就直接把拒答话术当一帧 token 发出去(答案一律走 token 帧,见下)。
    输出:帧序列,**恰好一帧 done 收尾**,它带答案、证据链与结束状态。

    帧协议在 ``application/streaming.py``;两条要记住的:

    - **答案文本一律通过 token 帧交付**(标准答案、拒答话术都在内),消费方不用写分支;
    - ``"".join(token.text).strip() == done.answer`` 恒成立 —— 非流式入口就是靠这条收拢的。

    历史只进提示词、不参与检索:它不该改变召回什么,只该改变「怎么读这个问题」。

    **改写只改"拿什么去检索",不改"回答的是哪个问题"**:生成答案用的仍然是
    ``query``(用户原话)。拿假设答案的正文去当 prompt 里的「问题」,模型会开始回答那个
    假设答案 —— 用户看到的就成了"自问自答",而且看不出哪里不对。

    **这是内部的帧生产者**:公开入口是 ``stream_rag``(它多包了一层计时与日志)。
    这里只多做两件事 —— 在检索之前把 query 规划成一组检索语句,并把账本往
    ``retrieve_many()`` 里带(那里面有六段子打点),再在检索返回、准备调 provider
    两处收口(TTFT 的两个分解项)。
    """
    queries: tuple[str, ...] = (query,)
    if rewriter is not None:
        plan = rewriter.rewrite(query)
        # 端口约定 queries 永不为空;真拿到空表是适配器违约,这里兜最后一道 ——
        # 空表一路传下去就是"召回到空 → 拒答",而那看着像"知识库里没有"
        queries = plan.queries or (query,)
        # 打点在**改写结束之后**:``rewrite_ms`` 因此是"改写了多久"。
        # 检索那几段(embed/retrieve)以这个点为起点,免得一次 LLM 改写的耗时
        # 混进"检索花了多久"里(见 observability/latency.py 的 _AFTER_REWRITE)。
        timing.mark("rewrite")
        _logger.info(
            "查询改写[%s/%s]%s:%s → %s",
            plan.strategy,
            plan.decided_by,
            plan.note or "ok",
            query,
            " | ".join(queries),
        )
    citations = retrieve_many(
        queries,
        embedder=embedder,
        store=store,
        keyword_index=keyword_index,
        reranker=reranker,
        options=options,
        timing=timing,
        # 重排用**用户原话**:所有候选在同一把尺子下比一次,答案里 [1][2] 的顺序
        # 才对应得到"哪条更答得上用户问的那件事"
        rerank_query=query,
    )
    timing.mark("retrieve")
    if not citations:
        _logger.info("检索为空,直接拒答(不调 LLM):%s", query)
        yield QaEvent(kind="token", text=REFUSAL_TEXT)
        yield QaEvent(kind="done", answer=REFUSAL_TEXT, refused=True)
        return

    yield QaEvent(kind="sources", citations=citations)

    prompt = rag_answer_prompt(citations, query, history)
    timing.mark("llm_start")
    parts: list[str] = []
    reason: str | None = None
    for delta in llm.stream([ChatMessage(role="user", content=prompt)]):
        if delta.last:
            reason = delta.finish_reason
        if delta.usage is not None:
            # 用量只记在**这一层**(真调了模型的那一层):外层的 qa 账本也会转发这些帧,
            # 但它没有自己的 provider 调用,记上去就是双倍计入(见 application/cost.py)。
            timing.usage(
                model=delta.usage.model,
                input_tokens=delta.usage.input_tokens,
                output_tokens=delta.usage.output_tokens,
                cached_input_tokens=delta.usage.cached_input_tokens,
            )
        if delta.reasoning:
            # 思考只记账、**不外发**:这一轮想没想、想了多久要看得见,但草稿不该进答案
            # (也不进 token 帧 —— 前端此刻只该等正文)。阈值/口径见 observability/latency.py
            timing.think(delta.reasoning_text)
        if delta.text:
            parts.append(delta.text)
            yield QaEvent(kind="token", text=delta.text)

    text = "".join(parts).strip()
    # 结束原因在最后一帧(适配器的约定);provider 没说就是 None,按「没截断」处理
    truncated = reason not in (None, "stop")
    if truncated:
        _logger.warning(
            "模型回答被截断(finish_reason=%s),只拿到 %d 字:%s", reason, len(text), query
        )
    if not text:
        # 空回答按拒答处理:把空串返回给用户等于页面一片空白,比明说信息不足更糟
        _logger.warning("模型返回空回答,按拒答处理:%s", query)
        yield QaEvent(kind="token", text=REFUSAL_TEXT)
        yield QaEvent(
            kind="done",
            answer=REFUSAL_TEXT,
            citations=citations,
            refused=True,
            truncated=truncated,
        )
        return

    yield QaEvent(kind="done", answer=text, citations=citations, truncated=truncated)


def answer_rag(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    llm: LLMClient,
    keyword_index: KeywordIndex | None = None,
    reranker: Reranker | None = None,
    options: RetrievalOptions | None = None,
    history: Sequence[ConversationTurn] = (),
    thinking: str | None = None,
    rewriter: QueryRewriter | None = None,
) -> RagAnswer:
    """输入/输出同 ``stream_rag``,只是把流**收拢**成一句答案。

    刻意实现成 ``stream_rag`` 的收拢器,而不是另写一遍生成逻辑:
    两条路各写一份,「流式答案和非流式答案不一样」是迟早的事,而且那种不一致极难查。
    """
    done = done_of(
        stream_rag(
            query,
            embedder=embedder,
            store=store,
            llm=llm,
            keyword_index=keyword_index,
            reranker=reranker,
            options=options,
            history=history,
            thinking=thinking,
            rewriter=rewriter,
        )
    )
    return RagAnswer(
        answer=done.answer,
        citations=done.citations,
        refused=done.refused,
        truncated=done.truncated,
    )

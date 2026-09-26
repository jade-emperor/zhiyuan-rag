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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence

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
from project_rag.ports import Embedder, KeywordIndex, LLMClient, Reranker, VectorStore

# 同包用相对导入:项目的分层扫描规则把「绝对导入 project_rag.application.*」
# 当成跨层依赖拦下来(它只看模块前缀),相对导入才表达得出「同一个包内」
from .fusion import KEYWORD_ROUTE, VECTOR_ROUTE, fuse_rankings
from .streaming import done_of, timed
from .prompts import rag_answer_prompt

_logger = get_logger("application.answer_rag")

# 检索为空(或模型返回空)时的兜底话术。
REFUSAL_TEXT = "信息不足,无法回答,请联系人工客服。"


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
) -> tuple[Citation, ...]:
    """输入:问题 + 当前引用序列 + 重排端口。行为:按重排分重新排序。输出:重新编号的引用。

    喂给重排器的是 ``chunk.context_text`` —— **模型会读到的那段正文**,不是子块。
    这样引用清单上的重排分才对应得到模型的实际输入。

    三条边界,每条都有理由:

    - **空序列直接返回**,不调重排器:没有候选还跑一次模型前向是白花钱。
    - **只有 1 条候选也直接返回**,同样不调:一条候选的排序不可能变。
      代价是这时 ``rerank_score`` 留空 —— 没算过就不写数,别让人以为「重排给了它 0 分」。
    - **回来的分数条数不符当场抛错**:zip 会把多出来的候选静默丢掉,
      表现成「有几条候选凭空消失」,比直接报错难查得多。
    """
    if len(citations) < 2:
        return _renumber(citations)

    scores = reranker.rerank(query, [item.chunk.context_text for item in citations])
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

    **重排开不开由「有没有传重排器」表达**,不另设开关 —— 和 ``keyword_index=None``
    表示「没有 BM25 那一路」是同一个道理。两处都能表达就有两个真值来源:
    options 说不要重排、却又传了重排器时,谁赢?

    ``timing`` 给了就在六处打点(嵌入 / 向量检索 / 关键字 / 融合 / 折叠 / 重排):
    「检索一共 1.8 秒」是一回事,「这 1.8 秒花在哪」是另一回事 —— 没有分段就只能猜。
    不给就空转(验收与脚本常常不需要账本)。
    """
    options = options or RetrievalOptions()
    mark = timing.mark if timing is not None else _noop

    # 查询嵌入与向量检索拆成两句:写成一句的话,「嵌入多久」与「Milvus 多久」就永远
    # 分不开了 —— 而这两件事的优化手段完全不同(换 device / 换索引 / 加缓存)
    query_vector = embedder.embed([query])[0]
    mark("embed")
    vector_hits = store.search(query_vector, k=options.k, source=options.source)
    mark("vector")
    rankings: dict[str, Sequence[RetrievedChunk]] = {VECTOR_ROUTE: vector_hits}
    if keyword_index is not None:
        rankings[KEYWORD_ROUTE] = keyword_index.search(
            query, k=options.k, source=options.source
        )
    mark("bm25")

    if len(rankings) == 1:
        fused: tuple[FusedChunk, ...] = tuple(
            FusedChunk(chunk=hit.chunk, score=hit.score, routes=(VECTOR_ROUTE,))
            for hit in vector_hits
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
    if reranker is not None:
        collapsed = rerank_citations(query, collapsed, reranker=reranker)
        # 只在真装了重排器时打点:没装的时候这一格留 '-' 而不是 0.0 ——
        # 「没做」和「很快」在排查时是两件事
        mark("rerank")
    if options.max_context is not None and len(collapsed) > options.max_context:
        collapsed = _renumber(collapsed[: options.max_context])
    return collapsed


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
) -> Iterator[QaEvent]:
    """输入/输出同 ``_rag_frames``,额外做一件事:**计时并记一条 latency 日志**。

    TTFT / 吐字速率只有在「边界」上才看得到 —— 管道自己不知道「什么时候交出去」。
    这一层只观察:帧的内容、顺序、条数一个字节都不改(验收拿未包装的流逐帧比对过)。
    账本字段与口径见 ``observability/latency.py``。

    ``thinking`` 是**给日志用的静态标签**(配置里的档位,组合根从 config 读出来交进来):
    用例层不认识 config,账本更不认识 —— 账本只负责渲染。默认 None → 账里是 ``-``,
    于是注入式调用(验收、直调)不会被编一个假档位出来。
    """
    timing = StreamTiming(label="rag", channel="rag", thinking=thinking)
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
            timing=timing,
        ),
        timing,
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
) -> Iterator[QaEvent]:
    """输入:问题 + 四个端口 + 可选的关键字索引/重排器 + 检索旋钮 + 可选的历史轮次 + 计时账本。

    行为:检索并还原父块 → 有资料就先发一帧 sources(前端可以先渲染来源),再逐帧发 token;
          没资料就直接把拒答话术当一帧 token 发出去(答案一律走 token 帧,见下)。
    输出:帧序列,**恰好一帧 done 收尾**,它带答案、证据链与结束状态。

    帧协议在 ``application/streaming.py``;两条要记住的:

    - **答案文本一律通过 token 帧交付**(标准答案、拒答话术都在内),消费方不用写分支;
    - ``"".join(token.text).strip() == done.answer`` 恒成立 —— 非流式入口就是靠这条收拢的。

    历史只进提示词、不参与检索:它不该改变召回什么,只该改变「怎么读这个问题」。

    **这是内部的帧生产者**:公开入口是 ``stream_rag``(它多包了一层计时与日志)。
    这里只多做一件事 —— 把账本往 ``retrieve()`` 里带(那里面有六段子打点),
    并在检索返回、准备调 provider 两处收口(TTFT 的两个分解项)。
    """
    citations = retrieve(
        query,
        embedder=embedder,
        store=store,
        keyword_index=keyword_index,
        reranker=reranker,
        options=options,
        timing=timing,
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
        )
    )
    return RagAnswer(
        answer=done.answer,
        citations=done.citations,
        refused=done.refused,
        truncated=done.truncated,
    )

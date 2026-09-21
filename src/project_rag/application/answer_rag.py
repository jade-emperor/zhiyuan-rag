"""
用例:知识库问答(两路检索 → 融合 → 还原父块 → 拼上下文 → 生成带引用的回答)。

只认识端口 —— 换嵌入模型 / 换向量库 / 换关键字索引 / 换 LLM,这个文件一行都不改。

四条刻意的设计:

1. **检索为空时不调 LLM**:没有资料还让模型"自由发挥",正是幻觉的源头。
   直接返回拒答(``refused=True``),既省一次调用,又不会编。
2. **两路融合在这里做**:两路分数不可比,融合是决策不是管道(见 fusion.py)。
   只给向量库不给关键字索引时**退化成单路,且不做融合** —— 单路时直接沿用那一路的分数,
   这样「检索分」的口径不会因为有没有第二路而变。
3. **命中子块之后按父块折叠**:父子分层的意义就是「多召回子块、少给父块」。
   同一个父块的三个子块都被命中时,不折叠就等于把几乎一样的上下文喂三遍。
4. **引用跟着答案一起返回**:``[1][2]`` 是给人核对用的;``routes`` 还告诉你
   这一条是单路捞到的还是两路都捞到了 —— 两路都命中通常更可信。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from project_rag.domain import (
    ChatMessage,
    Citation,
    FusedChunk,
    RagAnswer,
    RetrievalOptions,
    RetrievedChunk,
)
from project_rag.observability import get_logger
from project_rag.ports import Embedder, KeywordIndex, LLMClient, VectorStore

# 同包用相对导入:项目的分层扫描规则把「绝对导入 project_rag.application.*」
# 当成跨层依赖拦下来(它只看模块前缀),相对导入才表达得出「同一个包内」
from .fusion import KEYWORD_ROUTE, VECTOR_ROUTE, fuse_rankings
from .prompts import rag_answer_prompt

_logger = get_logger("application.answer_rag")

# 检索为空(或模型返回空)时的兜底话术。跟讲义 4.5 的转人工话术一致。
REFUSAL_TEXT = "信息不足,无法回答,请联系人工客服。"


def _renumber(citations: Iterable[Citation]) -> tuple[Citation, ...]:
    """输入:任意引用序列。行为:按当前顺序重新从 1 编号。输出:新引用元组。

    编号只由这里产生:融合、折叠、截断之后都必须重新编号,
    否则答案里的 [3] 会指向一个已经被折叠掉的子块。
    """
    return tuple(
        Citation(
            index=index,
            chunk=citation.chunk,
            score=citation.score,
            routes=citation.routes,
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


def retrieve(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    keyword_index: KeywordIndex | None = None,
    options: RetrievalOptions | None = None,
) -> tuple[Citation, ...]:
    """输入:问题 + 向量库 + 可选的关键字索引 + 检索旋钮。

    行为:跑有索引的那几路 → 融合(单路则跳过) → 折叠父块 → 截断到 max_context。
    输出:引用序列;没检索到给空序列。
    """
    options = options or RetrievalOptions()

    vector_hits = store.search(
        embedder.embed([query])[0], k=options.k, source=options.source
    )
    rankings: dict[str, Sequence[RetrievedChunk]] = {VECTOR_ROUTE: vector_hits}
    if keyword_index is not None:
        rankings[KEYWORD_ROUTE] = keyword_index.search(
            query, k=options.k, source=options.source
        )

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

    citations = _renumber(
        Citation(index=0, chunk=item.chunk, score=item.score, routes=item.routes)
        for item in fused
    )
    collapsed = collapse_parents(citations)
    if options.max_context is not None and len(collapsed) > options.max_context:
        collapsed = _renumber(collapsed[: options.max_context])
    return collapsed


def answer_rag(
    query: str,
    *,
    embedder: Embedder,
    store: VectorStore,
    llm: LLMClient,
    keyword_index: KeywordIndex | None = None,
    options: RetrievalOptions | None = None,
) -> RagAnswer:
    """输入:问题 + 三个端口 + 可选的关键字索引 + 检索旋钮。

    行为:检索并还原父块 → 有资料就拼提示词问模型,没资料直接拒答。
    输出:答案 + 引用清单;拒答时 ``refused=True`` 且引用为空。
    """
    citations = retrieve(
        query,
        embedder=embedder,
        store=store,
        keyword_index=keyword_index,
        options=options,
    )
    if not citations:
        _logger.info("检索为空,直接拒答(不调 LLM):%s", query)
        return RagAnswer(answer=REFUSAL_TEXT, citations=(), refused=True)

    prompt = rag_answer_prompt(citations, query)
    text = "".join(llm.stream([ChatMessage(role="user", content=prompt)])).strip()
    if not text:
        # 空回答按拒答处理:把空串返回给用户等于页面一片空白,比明说信息不足更糟
        _logger.warning("模型返回空回答,按拒答处理:%s", query)
        return RagAnswer(answer=REFUSAL_TEXT, citations=citations, refused=True)

    return RagAnswer(answer=text, citations=citations, refused=False)

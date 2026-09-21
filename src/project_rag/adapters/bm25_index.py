"""
进程内 BM25 关键字索引(``rank_bm25`` 的 ``BM25Plus``)。

**为什么关键字那一路不进 Milvus**:Milvus 2.4.4 没有内置 BM25(那是 2.6 的能力),
而 ``rank_bm25`` 已经在依赖里、FAQ 通道也一直在用它。只为这一路再引一个 ES,不划算。


**语料从哪来**:从 ``VectorStore`` 取 —— 全量文本在向量库里已经有唯一的一份,
不必再维护第二个存储。代价是每次查询都要 ``load_all()`` 读一遍:
几千块无所谓,上万块时要改成 ingest 时喂索引 + 持久化(记在 deferred)。

**索引按指纹缓存**:语料一变(或子块内容一变)就重建。这和 FAQ 匹配器是同一套做法 ——
两处都不该每次查询重算 IDF。
"""

from __future__ import annotations

from collections.abc import Sequence

from rank_bm25 import BM25Plus

from project_rag.adapters.chinese_tokens import tokens
from project_rag.domain import Chunk, RetrievedChunk
from project_rag.ports import KeywordIndex, VectorStore


class BM25KeywordIndex(KeywordIndex):
    """子块级 BM25。语料从向量库取,索引按指纹缓存。"""

    def __init__(self, store: VectorStore) -> None:
        """输入:向量库(全量语料从它那里取)。行为:记下依赖,索引留给第一次查询再建。输出:无。"""
        self._store = store
        self._fingerprint: tuple[tuple[str, str], ...] | None = None
        self._index: BM25Plus | None = None
        self._chunks: list[Chunk] = []

    def search(
        self, query: str, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、返回条数、来源过滤。行为:BM25 打分 → 过滤来源 → 降序取前 k。输出:命中序列。"""
        self._refresh()
        index = self._index
        if index is None:
            return []

        scores = index.get_scores(tokens(query))
        # 同分时按 chunk_id 兜底排序:顺序稳定,验收才写得死断言
        ranked = sorted(
            range(len(self._chunks)),
            key=lambda i: (-float(scores[i]), self._chunks[i].chunk_id),
        )

        hits: list[RetrievedChunk] = []
        for i in ranked:
            score = float(scores[i])
            if score <= 0.0:
                # 一个查询词都没沾上(词表里压根没有,分数恒为 0)—— **这不是命中**。
                # 实测:域外问法(「学费 多少钱」)全 0,域内问法 9.7~14.7,中间没有灰区。
                # 必须在这里停:融合那一步会给「第 1 名」排名分,
                # 把 0 分的垃圾当命中返回,等于让它挤掉真命中。降序排列,所以能直接 break。
                break
            chunk = self._chunks[i]
            if source is not None and chunk.source != source:
                continue
            hits.append(RetrievedChunk(chunk=chunk, score=score))
            if len(hits) == k:
                break
        return hits

    def _refresh(self) -> None:
        """输入:无。行为:语料指纹变了就重建索引。输出:无。"""
        chunks = list(self._store.load_all())
        fingerprint = tuple((chunk.chunk_id, chunk.text) for chunk in chunks)
        if fingerprint == self._fingerprint and self._index is not None:
            return
        self._chunks = chunks
        self._index = (
            BM25Plus([tokens(chunk.text) for chunk in chunks]) if chunks else None
        )
        self._fingerprint = fingerprint

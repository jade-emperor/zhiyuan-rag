"""
进程内 BM25 关键字索引(``rank_bm25`` 的 ``BM25Plus``)。

**为什么关键字那一路不进 Milvus**:Milvus 2.4.4 没有内置 BM25(那是 2.6 的能力),
而 ``rank_bm25`` 已经在依赖里、FAQ 通道也一直在用它。只为这一路再引一个 ES,不划算。

**语料从哪来**:从 ``VectorStore`` 取 —— 全量文本在向量库里已经有唯一的一份,
不必再维护第二个存储。取全文只在**语料变了**的时候发生:平时每次查询只取一份
「主键 + 父块号」的廉价指纹(``VectorStore.fingerprint()``)。以前的做法是
每次查询都 ``load_all()`` —— 1283 块要把 ~6MB 正文(含父块正文)拉回来,是
「检索 1.8s」里固定的一笔。

**索引按指纹缓存**:语料一变(或父子结构一变)就重建。这和 FAQ 匹配器是同一套做法 ——
两处都不该每次查询重算 IDF。

**这个类现在是进程级单例**(``cli/deps.py`` 里 `lru_cache`),所以它**会被多个线程并发调用**
(FastAPI 的线程池)。因此「换一份索引」必须是一次**原子发布**:见 ``_state`` 的注释。
"""

from __future__ import annotations

from collections.abc import Sequence

from rank_bm25 import BM25Plus

from project_rag.adapters.chinese_tokens import tokens
from project_rag.domain import Chunk, RetrievedChunk
from project_rag.ports import KeywordIndex, VectorStore

# 一份配套的快照:(语料指纹, 语料, BM25 索引)
_State = tuple[tuple[str, ...], list[Chunk], "BM25Plus | None"]


class BM25KeywordIndex(KeywordIndex):
    """子块级 BM25。语料从向量库取,索引按指纹缓存。"""

    def __init__(self, store: VectorStore) -> None:
        """输入:向量库(全量语料从它那里取)。行为:记下依赖,索引留给第一次查询再建。输出:无。"""
        self._store = store
        # **整套快照只用一个属性装**:指纹/语料/索引必须同时换。
        # 拆成三个属性写的话,并发读的人可能拿到「新语料 + 旧索引」——
        # 长度不匹配直接 IndexError,或者分数与块错位(那更糟:它不报错)。
        # 单次属性赋值在 CPython 里是原子的,所以这里不需要锁。
        self._state: _State | None = None

    def search(
        self, query: str, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、返回条数、来源过滤。行为:BM25 打分 → 过滤来源 → 降序取前 k。输出:命中序列。"""
        self._refresh()
        state = self._state  # 单次读取:拿到的三元组一定是配套的
        if state is None:
            return []
        _, chunks, index = state
        if index is None:
            return []

        scores = index.get_scores(tokens(query))
        # 同分时按 chunk_id 兜底排序:顺序稳定,验收才写得死断言
        ranked = sorted(
            range(len(chunks)),
            key=lambda i: (-float(scores[i]), chunks[i].chunk_id),
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
            chunk = chunks[i]
            if source is not None and chunk.source != source:
                continue
            hits.append(RetrievedChunk(chunk=chunk, score=score))
            if len(hits) == k:
                break
        return hits

    def _refresh(self) -> None:
        """输入:无。行为:指纹变了才重建索引,并把新快照原子地发布出去。输出:无。

        指纹取自主键 + 父块号:主键里含内容哈希,所以「指纹相同」蕴含「正文没变」。
        两个线程同时发现语料变了会各建一份 —— 白做一次功,但结果一致(可接受;
        加锁串行化反而会把 1283 块的分词(实测 ~0.4s)变成所有请求一起等)。
        """
        fingerprint = tuple(self._store.fingerprint())
        state = self._state
        if state is not None and state[0] == fingerprint:
            return
        chunks = list(self._store.load_all())
        index = BM25Plus([tokens(chunk.text) for chunk in chunks]) if chunks else None
        self._state = (fingerprint, chunks, index)

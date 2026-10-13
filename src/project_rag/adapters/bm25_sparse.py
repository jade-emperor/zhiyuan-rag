"""
BM25 的稀疏向量编码(``BM25SparseEncoder``)+ Milvus 上的关键字检索(``MilvusSparseKeywordIndex``)。

这是 ``keyword.backend=milvus`` 那一档:入库时把每一块编码成稀疏向量写进集合的
``sparse_vector`` 列,检索交给 Milvus 的 ``SPARSE_INVERTED_INDEX`` —— 进程内不再需要
全量语料、jieba 索引与那 111MB 常驻内存(代价与账见下)。

**BM25 是怎么塞进"文档向量 · 查询向量"的**:

    score(q, d) = Σ_{t ∈ q ∩ d} idf(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1-b+b·|d|/avgdl))

右边每一项都是"只跟 t 与 d 有关"乘上"只跟 t 与 q 有关"。取

* **文档侧** ``w_d(t) = idf(t) · tf·(k1+1) / (tf + k1·(1-b+b·|d|/avgdl))``
* **查询侧** ``w_q(t) = 1``(这个词在查询里出现过)

那么稀疏内积 ``Σ_t w_q(t)·w_d(t)`` **就等于**上面的 BM25 分数。于是:

* 查询路径**不需要任何语料统计** —— 只要"分词 + 定维度"(这是关键:否则每次检索都得
  把全库的 df/avgdl 捞回来,等于把进程内那份索引换个地方重建一遍);
* ``idf`` / ``avgdl`` 只在**写文档**时需要,而写文档的人(ingest / 上传)手上正好有全量正文
  (``SparseEncoder.fit`` 的入参,token 数一下就有)。

**维度怎么来的**:Milvus 的稀疏向量是"整数维度 → 权重",所以词必须映射到整数。
这里用 ``blake2b`` 的 64 位摘要取模 —— **不许用内置 ``hash()``**:它按进程加盐
(``PYTHONHASHSEED``),同一个词在 ingest 进程与 API 进程会落到不同维度,
表现是"检索结果静默变空"(没有报错)。取模空间 2^31-2,十万级词表的碰撞期望 ≪ 1,
撞了也只是两个词共享一个维度(分数略有串扰,不影响可用性)。

**维度 0 是保留位**:Milvus 拒收空稀疏行(``empty sparse float vector row``,2.4.4 实测),
而"整块都是标点"的块确实会出现 —— 那种块写 ``{0: 1e-6}``:非空、且与任何真实查询的内积
都近似 0(真实词永远落在 ≥ 1 的维度上)。**不给它编一个假词**:那会凭空生出一次命中。

**为什么是标准 BM25、而进程内那档是 BM25Plus**:``rank_bm25.BM25Plus`` 多了个 delta 项
(每个词最低给一点分),所以两档的**分数绝对值不同** —— 融合用的是排名(RRF)或 min-max 归一
(weighted),两档都成立。同一份语料换档,读数会变一点,但排序口径一致。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from project_rag.adapters.chinese_tokens import tokens
from project_rag.domain import RetrievedChunk, SparseVector
from project_rag.observability import get_logger
from project_rag.ports import KeywordIndex, SparseEncoder, SparseStats, VectorStore

_logger = get_logger("adapters.bm25_sparse")

# 稀疏向量的维度空间:1 .. _HASH_SPACE(含)。2^31-1 是质数,离 uint32 上限还有一倍余量。
_HASH_SPACE = 2**31 - 1
# 保留维度:空文档占位(见模块 docstring)。权重取一个极小数 —— 非零(满足 Milvus 的非空要求),
# 又小到与任何真实查询的内积都约等于 0。
_EMPTY_DIM = 0
_EMPTY_WEIGHT = 1e-6


def _dimension(term: str) -> int:
    """输入:一个词。行为:稳定地映射到 [1, _HASH_SPACE] 里的整数维度。输出:维度。

    **稳定性是这个函数的全部意义**:ingest 与 API 是两个进程、可能两台机器,
    同一个词必须落到同一个维度,否则稀疏检索永远检索不到东西(而且不报错)。
    """
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    return 1 + int.from_bytes(digest, "big") % (_HASH_SPACE - 1)


class BM25SparseEncoder(SparseEncoder):
    """按 BM25 口径把文本编码成稀疏向量(文档侧带 idf 与长度归一,查询侧只带出现)。"""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        """输入:BM25 的两个常数(来自 ``keyword.bm25_k1`` / ``keyword.bm25_b``)。

        行为:记下常数,统计留到 ``fit``。输出:无。
        """
        if k1 <= 0:
            raise ValueError(f"bm25_k1 必须 > 0,收到 {k1}")
        if not 0.0 <= b <= 1.0:
            raise ValueError(f"bm25_b 必须在 [0, 1] 内,收到 {b}")
        self._k1 = k1
        self._b = b
        # 维度 → idf(不是"词 → idf":撞维度的两个词本来就分不开,存一份即可)
        self._idf: dict[int, float] = {}
        self._n_docs = 0
        self._avg_len = 0.0

    def fit(self, texts: Sequence[str]) -> SparseStats:
        """输入:全库文本。行为:统计每个维度的 df 与平均长度。输出:统计(SparseStats)。

        **口径必须是全库**:喂一部分文本,算出来的 idf 只对那一部分成立 ——
        查询侧不带 idf,所以错的口径不会被"另一侧也错了"抵消,而是直接错进排序里。
        """
        n_docs = len(texts)
        df: Counter[int] = Counter()
        total_len = 0
        for text in texts:
            terms = tokens(text)
            total_len += len(terms)
            # 同一块里重复出现的词只算一次 df(这才是 document frequency)
            df.update({_dimension(term) for term in terms})

        # 空语料:给一份"没统计"的初值,让 encode_documents 能给出可用的权重
        self._n_docs = max(n_docs, 1)
        self._avg_len = (total_len / n_docs) if n_docs else 0.0
        total = float(self._n_docs)
        self._idf = {
            dim: math.log(1.0 + (total - count + 0.5) / (count + 0.5))
            for dim, count in df.items()
        }
        stats = SparseStats(n_docs=n_docs, avg_len=self._avg_len, vocabulary=len(self._idf))
        _logger.info(
            "BM25 统计:%d 块 / 平均 %.1f 词 / %d 个维度(≈词表大小)",
            stats.n_docs,
            stats.avg_len,
            stats.vocabulary,
        )
        return stats

    def encode_documents(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        """输入:待写的块正文。行为:按 BM25 权重编码。输出:稀疏向量(保证非空)。"""
        if self._n_docs == 0:
            raise RuntimeError(
                "encode_documents 之前必须先 fit(权重依赖全库统计:df / N / avgdl)"
                " —— 见 application/ingest_documents.py 里的调用顺序"
            )
        vectors: list[SparseVector] = []
        for text in texts:
            terms = tokens(text)
            counts: Counter[int] = Counter(_dimension(term) for term in terms)
            length = len(terms) or 1
            norm = 1.0 - self._b + self._b * (length / (self._avg_len or 1.0))
            vector = {
                dim: self._idf.get(dim, self._unseen_idf())
                * tf
                * (self._k1 + 1.0)
                / (tf + self._k1 * norm)
                for dim, tf in counts.items()
            }
            # 一个词都没分出来(整块标点/空白):用保留维度占位,别返回空(Milvus 会拒收)
            vectors.append(vector or {_EMPTY_DIM: _EMPTY_WEIGHT})
        return vectors

    def encode_query(self, text: str) -> SparseVector:
        """输入:查询文本。行为:出现过的词各给权重 1(不去重会重复计分,所以用集合)。输出:稀疏向量。"""
        return {_dimension(term): 1.0 for term in set(tokens(text))}

    def _unseen_idf(self) -> float:
        """输入:无。行为:全库没出现过的词按"只出现一次"给 idf。输出:idf。

        为什么是这一档:新文档(上传)里的生僻词在全库统计里没有 df —— 按 df=1 算,
        即"它很稀罕",与同量级的词保持一致;给 0 会让新词的权重整段消失,
        给平均 idf 又会让生僻词被稀释。**两种都错得很安静**,所以取这个有明确含义的极值。
        """
        total = float(self._n_docs)
        return math.log(1.0 + (total - 1.0 + 0.5) / (1.0 + 0.5))


class _SparseSearcher(Protocol):
    """这条路径**只需要**稀疏检索这一个能力。

    依赖写窄一点有两个好处:假件不必造出整个 ``VectorStore``(验收里那几条就是这么写的);
    同时也说明白了"关键字这一路不会去动向量库的别的方法"。
    """

    def search_sparse(
        self, query_sparse: SparseVector, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]: ...


class MilvusSparseKeywordIndex(KeywordIndex):
    """关键字那一路:查询编码成稀疏向量,检索交给 Milvus(``search_sparse``)。"""

    def __init__(self, store: _SparseSearcher, encoder: SparseEncoder) -> None:
        """输入:向量库(稀疏检索从它走)、编码器(与入库用的是**同一个**,见 deps)。输出:无。

        **与入库共用一份编码器实例**是刻意的:查询侧虽然不带 idf,
        但"分词口径 + 哈希维度"必须与写文档时逐字一致 —— 共用实例让这件事不需要靠约定。
        """
        self._store = store
        self._encoder = encoder

    def search(
        self, query: str, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、返回条数、来源过滤。行为:编码 → Milvus 稀疏检索。输出:命中序列。"""
        sparse = self._encoder.encode_query(query)
        if not sparse:
            # 一个词都没分出来(整句标点):没有命中可言。拿空向量去检索,
            # Milvus 要么报错要么回一堆 0 分 —— 两条路都不如在这里停。
            return []
        return self._store.search_sparse(sparse, k=k, source=source)

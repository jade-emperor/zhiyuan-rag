"""
端口:词面稀疏编码(把一块文本编码成「维度 → 权重」,供稀疏倒排索引检索)。

**为什么查询侧不需要语料统计**:BM25 的分数是

    score(q, d) = Σ_{t ∈ q ∩ d} idf(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1-b+b·|d|/avgdl))

把等号右边拆成"文档侧权重 × 查询侧权重"两半,让**文档侧**带上 ``idf`` 与长度归一、
**查询侧**只放"这个查询出现了哪些词"(权重 1),那么稀疏内积(IP)恰好等于上面那个和。
于是查询路径只需要"分词 + 定维度",一句语料统计都不用读 —— 这是把 BM25 放进
向量库之后**最重要的一条性质**(否则每次检索都得先把全库统计捞回来,等于把
进程内那份索引换个地方重建一遍)。

代价与边界(都写在 ``adapters/bm25_sparse.py`` 里,这里只列契约):

* ``fit`` 必须在 ``encode_documents`` 之前调用 —— **权重依赖全库统计**(df / N / avgdl),
  这是 BM25 的固有属性,不是实现选择;
* ``encode_query`` **不需要** fit(权重恒为 1,不依赖统计);
* 维度由词哈希而来(同一份实现里,同一个词在任何进程、任何机器上都必须落到同一个维度)——
  ``encoding`` 的身份(哈希算法与词表口径)由实现负责稳定,调用方不许自己算维度。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from project_rag.domain import SparseVector


@dataclass(frozen=True, slots=True)
class SparseStats:
    """一次 ``fit`` 的结果(语料级统计),给日志与账本看。"""

    n_docs: int  # 参与统计的文档(本项目里 = 子块)数
    avg_len: float  # 平均长度(词数)
    vocabulary: int  # 不同词数


class SparseEncoder(Protocol):
    """把文本编码成稀疏向量(词面权重)。"""

    def fit(self, texts: Sequence[str]) -> SparseStats:
        """输入:全库文本。行为:统计 df / N / avgdl,之后 ``encode_documents`` 才有效。输出:统计。

        **口径是"全库"**:只喂一部分文本,算出来的 idf 只对那一部分成立 ——
        调用方要给整个集合的正文(见 ``application/ingest_documents.py`` 的 ``corpus_texts``)。
        """
        ...

    def encode_documents(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        """输入:待编码的文档文本。行为:按 BM25 权重编码(含 idf 与长度归一)。输出:稀疏向量。

        约定:**返回的向量必须非空**(至少要有一个维度)—— 存储层(Milvus)拒收空稀疏行。
        真的一个词都没有时,实现要用保留维度兜底(不能悄悄返回空)。
        """
        ...

    def encode_query(self, text: str) -> SparseVector:
        """输入:查询文本。行为:编码成"出现过的词,权重 1"。输出:稀疏向量(可能为空)。

        空 = 这个查询一个词都没分出来 —— 调用方应当直接判定"没有命中",
        而不是拿空向量去检索(内积恒为 0,没有意义)。
        """
        ...

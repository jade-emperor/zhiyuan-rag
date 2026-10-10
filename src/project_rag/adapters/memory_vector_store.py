"""
内存向量库:余弦相似 + 来源过滤 + 按 chunk_id 幂等覆盖。不持久化。

**为什么它在 adapters/ 而不是测试目录**(两条理由,都与"端口要有显式实现"无关 ——
那条 AST 检查 已删,显式继承也不再是要求):

① **端口的语义契约需要一份参考实现**:`upsert` 回的是库内总数而不是写入数、`search` 空库给空序列、
   `fingerprint` 顺序稳定、按 `source` / `doc_id` 删 —— 这些"反直觉但必须"的口径由它钉住
   (`check_m3_rag` 里 30+ 处引用它);换向量库时新适配器照它对齐语义。
② **验收必须能在没有容器的机器上跑**:Docker 一停,Milvus 相关的检查只能 SKIP,
   而 ingest→search 全链路正是靠它继续可跑、可验收(这轮环境断掉时实测发生过)。

**它是测试替身,不是产品路径的一部分**:`build_real_deps` 永远不造它 ——
真实装配必须接 Milvus,悄悄退回内存实现 = 把假结果当真答案返回。
(真要减代码:唯一正当的做法是把它搬进 `scripts/acceptance/`,而不是删 —— 那要改 30+ 处引用,
并放弃"端口在 src/ 里有一份参考实现"这个说法。性价比很低。)
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from project_rag.domain import Chunk, Embedding, RetrievedChunk
from project_rag.ports import VectorStore


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """输入:两个向量。行为:算余弦相似度,维度不一致直接报错。输出:[-1,1] 分数;零向量给 0.0。"""
    if len(left) != len(right):
        raise ValueError(f"向量维度不一致：{len(left)} vs {len(right)}")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class InMemoryVectorStore(VectorStore):
    """进程内向量库。数据随进程消失,不做持久化。"""

    def __init__(self) -> None:
        """输入:无。行为:初始化空表。输出:无。"""
        # chunk_id -> (子块, 向量)。用 dict 就是为了让 upsert 天然幂等
        self._rows: dict[str, tuple[Chunk, Embedding]] = {}

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Embedding]) -> int:
        """输入:子块 + 同序等长向量。行为:按 chunk_id 覆盖写入。输出:表内总条数。"""
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks({len(chunks)}) 与 embeddings({len(embeddings)}) 必须等长同序"
            )
        for chunk, embedding in zip(chunks, embeddings):
            self._rows[chunk.chunk_id] = (chunk, embedding)
        return len(self._rows)

    def search(
        self,
        query_embedding: Embedding,
        *,
        k: int,
        source: str | None = None,
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、问题向量、返回条数、来源过滤。行为:算余弦并按分数降序。输出:前 k 条。"""
        hits = [
            RetrievedChunk(
                chunk=chunk, score=_cosine(query_embedding.dense, embedding.dense)
            )
            for chunk, embedding in self._rows.values()
            if source is None or chunk.source == source
        ]
        # 同分时按 chunk_id 兜底排序:顺序稳定,验收才写得死断言
        hits.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return hits[:k]

    def load_all(self) -> Sequence[Chunk]:
        """输入:无。行为:取出全部子块(按 chunk_id 排序,顺序稳定)。输出:子块序列。"""
        return [
            chunk
            for chunk, _ in sorted(self._rows.values(), key=lambda row: row[0].chunk_id)
        ]

    def fingerprint(self) -> Sequence[str]:
        """输入:无。行为:主键 + 父块号的指纹(与 Milvus 实现同构,排序后返回)。输出:指纹序列。

        ``\x1f`` 是项目里惯用的字段分隔符(缓存 value 契约也用它):
        主键与父块号都是十六进制串,不含这个字符,拼起来不会歧义。
        """
        return sorted(
            f"{chunk.chunk_id}\x1f{chunk.parent_id}" for chunk, _ in self._rows.values()
        )

    def delete_by_doc_id(self, doc_id: str) -> int:
        """输入:文档标识。行为:删掉这份文档的全部块(ingest 写之前调)。输出:删除条数。"""
        doomed = [
            chunk_id
            for chunk_id, (chunk, _) in self._rows.items()
            if chunk.doc_id == doc_id
        ]
        for chunk_id in doomed:
            del self._rows[chunk_id]
        return len(doomed)

    def delete_by_source(self, source: str) -> int:
        """输入:来源标签。行为:删掉该来源的全部块(整份语料下架时用)。输出:删除条数。"""
        doomed = [
            chunk_id
            for chunk_id, (chunk, _) in self._rows.items()
            if chunk.source == source
        ]
        for chunk_id in doomed:
            del self._rows[chunk_id]
        return len(doomed)

    def drop(self) -> None:
        """输入:无。行为:清空表。输出:无。"""
        self._rows.clear()

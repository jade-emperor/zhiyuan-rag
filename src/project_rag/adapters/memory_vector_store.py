"""
内存向量库:余弦相似 + 来源过滤 + 按 chunk_id 幂等覆盖。不持久化。

**为什么它在 adapters/ 而不是测试目录**:
项目法律要求「每个端口都必须有类显式实现」(check_m1_3_cli.py 静态扫描)——
端口不能是一份没人执行的合同。内存实现同时承担两件事:
① Milvus 还没接上(M3.5 之前)时,让 ingest→search 全链路可跑、可验收;
② 给后续用例提供不依赖容器的测试替身。

**绝不能进 build_real_deps**:真实装配必须接 Milvus,
悄悄退回内存实现 = 把假结果当真答案返回。
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

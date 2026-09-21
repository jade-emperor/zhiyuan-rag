"""
端口:文本 → 向量。

只依赖 ``domain`` + 标准库 —— 谁来实现（本地 BGE-M3 / 远程 embedding API）跟用例无关。

约定:**一次调用编码一批文本,返回顺序与输入一一对应**。
逐条调用会把推理吞吐打回原点(批量前向才是 CPU/GPU 都吃得下的形态)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import Embedding


class Embedder(Protocol):
    """把文本编码成向量。"""

    @property
    def dim(self) -> int:
        """输入:无。行为:返回稠密向量维度。输出:维度(Milvus 建集合要用它)。"""
        ...

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """输入:一批文本。行为:批量编码。输出:与输入同序等长的向量序列。"""
        ...

"""
端口:关键字(词面)检索。

和 ``VectorStore`` 是**平级的两路**:一个按词面匹配、一个按语义匹配。
两路的分数根本不可比(BM25 原始分无上界、余弦在 [0,1]),所以谁都不该在端口里做融合 ——
融合归用例层,那里才有「用排名还是用分数」的选择权。

约定:返回的 ``score`` 是**这一路自己的原始分**,既不要拿去和另一路比大小,
也不要拿去和阈值比。要比较就先融合(见 application/fusion.py)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import RetrievedChunk


class KeywordIndex(Protocol):
    """按词面相似度检索子块。"""

    def search(
        self, query: str, *, k: int, source: str | None = None
    ) -> Sequence[RetrievedChunk]:
        """输入:原问题、返回条数、可选的来源过滤。

        行为:按词面相似度打分,降序取前 k(先按来源过滤再截断)。
        输出:命中序列;空库给空序列。
        """
        ...

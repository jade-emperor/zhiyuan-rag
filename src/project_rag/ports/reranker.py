"""
端口:问题 + 一批候选正文 → 相关性分数。

**为什么是一次一批,不是一次一对**:和 ``Embedder.embed`` 同理 —— 批量前向才是
CPU/GPU 都吃得下的形态。实测 5 对合批 1.13s(0.23s/对),而单独一对 1.18s,
合批省掉的正是每条那一份固定开销。

**为什么它比嵌入准**:嵌入是双塔 —— 问题和文档各自编码,编码时谁也看不见谁;
CrossEncoder 把 (问题, 文档) 拼成**一条序列**过注意力,能直接建模两者的交互。
代价是它不能预计算:每个候选都得现算一次前向,所以只能用在召回之后的小候选集上。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Reranker(Protocol):
    """给「问题 + 候选正文」打分,分数越大越相关。"""

    def rerank(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        """输入:一个问题 + 一批候选正文。行为:逐对打分。输出:与输入同序等长的分数。

        两条约定:

        1. **同序等长**。用例层按位置把分数配回候选,少一条就会错位 ——
           所以用例层拿到长度不符会当场抛错,不许 zip 悄悄截断。
        2. 分数只在**同一次调用的结果内部**可比。跨问题的分数不可比 ——
           和相似度一样,排序 ≠ 置信度,别拿它去和阈值比。
        """
        ...

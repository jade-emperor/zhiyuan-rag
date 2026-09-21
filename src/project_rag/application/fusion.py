"""
两路融合:把「向量」和「BM25」的候选合并成一个排序。

**为什么融合必须在用例层**:两路的分数不可比 —— 实测同一句 query,余弦 0.59 而 BM25 14.7,
直接相加等于把 BM25 的权重放大 25 倍。怎么融合是个决策,决策留在自己手里才测得动、
才能和别的算法摆到同一份评估集上比;交给 Milvus 的 ranker 就只剩调参了。

两种方式都实现,用评估集选(配置里的 ``rag.fusion``):

- ``rrf``:只用**排名**。``score = Σ wᵢ / (rrf_k + rankᵢ)``,rank 从 1 开始。
  免标定、对量纲完全不敏感;代价是丢掉「第 1 名领先多少」这个信息。
- ``weighted``:各路先 **min-max 归一化**再按权重相加。
  保留分数信息、能表达「这一路更可信」;代价是要标定,而且归一化方式本身也是个选择。

**两个模式共同的坑**:同一份文档被两路都命中时必须**合并成一条**(分数相加、路名累积),
否则它会在结果里出现两次 —— 折叠父块那一步才去重的话,重复的分数已经算进去了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from project_rag.domain import Chunk, FusedChunk, RetrievedChunk

# 路名是契约的一部分:CLI 要按它打印、调试时要按它认人
VECTOR_ROUTE = "vector"
KEYWORD_ROUTE = "bm25"

_MODES = ("rrf", "weighted")


def fuse_rankings(
    rankings: Mapping[str, Sequence[RetrievedChunk]],
    *,
    mode: str = "rrf",
    weights: Mapping[str, float] | None = None,
    rrf_k: int = 60,
) -> tuple[FusedChunk, ...]:
    """输入:每一路的命中(键是路名)、融合方式、各路权重、RRF 平滑常数。

    行为:按 mode 融合;同一 chunk 只留一条(分数累加、路名累积);权重 ≤ 0 的路直接跳过。
    输出:融合后的命中,按融合分降序;同分按 chunk_id 兜底排序(顺序稳定,验收才写得死断言)。

    不认识的 mode 直接抛 ValueError —— 配置写错了要当场炸,不能悄悄退化成某一种。
    """
    if mode not in _MODES:
        raise ValueError(f"不认识的融合方式 {mode!r};只支持 {'/'.join(_MODES)}")
    weights = weights or {}

    totals: dict[str, float] = {}
    first_seen: dict[str, Chunk] = {}
    routes: dict[str, list[str]] = {}

    for name, hits in rankings.items():
        weight = float(weights.get(name, 1.0))
        if weight <= 0 or not hits:
            # 权重为 0 = 明确要关掉这一路(而不是「随便给点分」)
            continue
        for hit, contribution in zip(hits, _contributions(mode, hits, weight, rrf_k)):
            chunk_id = hit.chunk.chunk_id
            totals[chunk_id] = totals.get(chunk_id, 0.0) + contribution
            first_seen.setdefault(chunk_id, hit.chunk)
            routes.setdefault(chunk_id, []).append(name)

    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        FusedChunk(
            chunk=first_seen[chunk_id], score=score, routes=tuple(routes[chunk_id])
        )
        for chunk_id, score in ordered
    )


def _contributions(
    mode: str, hits: Sequence[RetrievedChunk], weight: float, rrf_k: int
) -> list[float]:
    """输入:一路的命中、融合方式、该路权重、RRF 常数。行为:算这一路给每条命中多少分。输出:与 hits 等长的贡献分。"""
    if mode == "rrf":
        # 排名从 1 开始。rrf_k 越大越弱化「头部优势」(60 是 RRF 原论文之后的常见默认值)
        return [weight / (rrf_k + rank) for rank in range(1, len(hits) + 1)]
    return [weight * value for value in _min_max(hits)]


def _min_max(hits: Sequence[RetrievedChunk]) -> list[float]:
    """输入:一路的命中。行为:把这一路的分数线性拉到 [0,1]。输出:与 hits 等长的归一化分数。

    **不归一化就不能加权相加**:同一句 query 实测余弦 0.59、BM25 14.7,
    直接相加等于把 BM25 的权重放大 25 倍 —— 那不是「两路融合」,是「BM25 加了个零头」。

    全部同分(含只有一条命中)时统一给 1.0:「这一路能给出的最高分」就是它自己。
    """
    values = [hit.score for hit in hits]
    low, high = min(values), max(values)
    if high - low <= 0:
        return [1.0] * len(values)
    return [(value - low) / (high - low) for value in values]

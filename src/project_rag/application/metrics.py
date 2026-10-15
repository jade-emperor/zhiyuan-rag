"""
延迟聚合:把一行行账本收成「分位数 + 比率 + 最慢几条」。

**口径写死在这里(改口径 = 改契约,`check_m5_9_metrics.py` 钉住)**:

1. **分位数用最近秩**(nearest-rank):排序后取第 ``ceil(q × n)`` 个,**不做插值**。
   样本少时插值会给出"比任何一次真实观测都好看"的数字,而这个项目已经吃过一次
   「指标名字会骗人」的亏(§5 第 24 条);
2. **样本不够就不给数**:``p95`` 要 ``n ≥ 20``、``p99`` 要 ``n ≥ 100``,不够一律 ``None``
   (渲染成 ``-``)。n=28 就报 p95 正是上一轮踩的坑 —— 数字有,含义没有;
3. **分段只在「热」的行上算**:冷启动那几行被模型加载主导(rerank 中位 668ms、p95 5383ms
   就是这个形状),混进来 p95 就变成"冷启动有多慢"。冷热条数各自单独报,最慢榜**含冷**
   (那是用户真实感受到的);
4. **``-``(没量到)一律不参与统计,不折算成 0**:折成 0 会把 p50 拉低,而且从数字上看不出来;
5. **比率的分母是窗口内全部行**(含冷、含中断):拒答率/截断率该按用户实际拿到的东西算;
6. **老数据的 ``cold`` 字段缺失时按「热」算**:那些行是加这个字段之前记的,
   当作热至少不会把它们从统计里剔掉(剔掉会让窗口前半段的样本凭空消失)。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# 默认统计哪些分段(顺序 = 报表里的顺序)。名字必须与账本字段一一对应。
DEFAULT_SEGMENTS = (
    "faq_ms",
    # 查询改写那段(有改写时才有值)。它和 retrieve_ms 是**并列**的两段:
    # 开了改写之后 retrieve_ms 从改写结束算起,所以这两格相加才是"检索前 + 检索"。
    "rewrite_ms",
    "sources_ms",
    "retrieve_ms",
    "embed_ms",
    "vector_ms",
    "bm25_ms",
    "fuse_ms",
    "collapse_ms",
    "rerank_ms",
    "llm_ttft_ms",
    "ttft_ms",
    "total_ms",
)
# 分位数的最小样本量。不够就给 None —— 见模块 docstring 第 2 条。
MIN_N_P50 = 1
MIN_N_P95 = 20
MIN_N_P99 = 100
# 最慢榜默认取几条
DEFAULT_TOP_N = 5


@dataclass(frozen=True, slots=True)
class SegmentStats:
    """一个分段的分布。``n`` 必须一起报:没有样本量的分位数读不出东西。"""

    name: str
    n: int
    p50: float | None
    p95: float | None
    p99: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class SlowRow:
    """最慢榜的一行(用户真实等待的那几条,含冷启动)。"""

    request_id: str
    channel: str
    cold: bool
    total_ms: float


@dataclass(frozen=True, slots=True)
class MetricsSummary:
    """一个时间窗的汇总。所有比率的分子都能在字段里找到,分母是 ``rows``。"""

    window: str
    rows: int
    cold_rows: int
    warm_rows: int
    refused: int
    truncated: int
    aborted: int
    faq_hits: int
    rag_answers: int
    segments: tuple[SegmentStats, ...]
    slowest: tuple[SlowRow, ...]


def _num(row: Mapping[str, str], key: str) -> float | None:
    """输入:一行账本 + 字段名。行为:取数,``-``/空/缺失/解析不了都算「没量到」。输出:浮点或 None。"""
    value = row.get(key)
    if value is None or value in ("", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _flag(row: Mapping[str, str], key: str) -> bool:
    """输入:一行账本 + 字段名。行为:判 ``true``。输出:布尔(缺失算 false)。"""
    return row.get(key) == "true"


def _percentile(ordered: Sequence[float], q: float, min_n: int) -> float | None:
    """输入:**升序**序列、分位、最小样本量。行为:最近秩取值(不插值)。输出:值或 None。"""
    n = len(ordered)
    if n < min_n or n == 0:
        return None
    rank = max(1, math.ceil(q * n))
    return ordered[min(rank, n) - 1]


def _segment(rows: Sequence[Mapping[str, str]], name: str) -> SegmentStats:
    """输入:行 + 分段名。行为:收齐非 ``-`` 的值再算分位数。输出:该分段的统计。"""
    values = sorted(v for v in (_num(row, name) for row in rows) if v is not None)
    return SegmentStats(
        name=name,
        n=len(values),
        p50=_percentile(values, 0.50, MIN_N_P50),
        p95=_percentile(values, 0.95, MIN_N_P95),
        p99=_percentile(values, 0.99, MIN_N_P99),
        maximum=values[-1] if values else None,
    )


def summarize(
    rows: Sequence[Mapping[str, str]],
    *,
    window: str,
    segments: Sequence[str] = DEFAULT_SEGMENTS,
    top_n: int = DEFAULT_TOP_N,
) -> MetricsSummary:
    """输入:一个时间窗内的账本行 + 窗口标签 + 要统计的分段 + 最慢榜条数。

    行为:算分段分布(**只用品热行**)、算比率(分母=全部行)、取最慢若干条(含冷)。
    输出:``MetricsSummary``。空窗返回全 0/None 的汇总(而不是抛错 —— 报表要能打开)。
    """
    warm = [row for row in rows if not _flag(row, "cold")]
    cold_rows = sum(1 for row in rows if _flag(row, "cold"))

    slow_candidates = [
        SlowRow(
            request_id=str(row.get("request_id") or "-"),
            channel=str(row.get("channel") or "-"),
            cold=_flag(row, "cold"),
            total_ms=total,
        )
        for row in rows
        if (total := _num(row, "total_ms")) is not None
    ]
    slow_candidates.sort(key=lambda item: item.total_ms, reverse=True)

    return MetricsSummary(
        window=window,
        rows=len(rows),
        cold_rows=cold_rows,
        warm_rows=len(warm),
        refused=sum(1 for row in rows if _flag(row, "refused")),
        truncated=sum(1 for row in rows if _flag(row, "truncated")),
        aborted=sum(1 for row in rows if _flag(row, "aborted")),
        faq_hits=sum(1 for row in rows if row.get("channel") == "faq"),
        rag_answers=sum(1 for row in rows if row.get("channel") == "rag"),
        segments=tuple(_segment(warm, name) for name in segments),
        slowest=tuple(slow_candidates[:top_n]),
    )

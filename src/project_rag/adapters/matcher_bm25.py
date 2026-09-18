"""
BM25 匹配适配器:jieba 分词 + BM25 打分 + 自身分归一化成 0~1 + 索引复用。

分数口径 = top1 的 BM25 原始分 ÷「同一条问题问自己」的 BM25 原始分。

为什么用这个参照系,而不是「top1 领先第二名的幅度」:
  - margin 度量的是区分度,不是相似度。它的病是**分数随语料冗余度衰减**:库里只要
    有一条近似重复的问法,完全一致的问题也会掉到 0.14,永远越不过阈值;而且条目越多
    分数越低,阈值被迫随语料规模漂移,根本标不出来(实测 N=3 时 0.411 → N=50 时 0.335,
    同时无关问法最高能到 0.481 —— 阈值区间是空集)。
  - 「问自己」的参照系稳定:分子分母同源,语料变大时两者一起变。实测一字不差 = 1.000
    (与语料规模无关)、改写问法 0.2~0.7、完全无关 ≤ 0.274(N=50)。
    但注意它只是让阈值有意义,不是让 FAQ 能理解改写 —— 改写问法归 M3 的向量检索通道。

真实语料复核(N=463 条,scripts/acceptance/calibrate_threshold.py):
  一字不差 1.000 / 无关问法 ≤ 0.303 / 库内近邻(次高分占比)中位 0.631、最高 0.912。
  阈值 0.85 落在区间 (0.303, 1.000] 内。

已知边界(不是 bug,是分工):
  - 分数被夹在 1.0:句尾附加无关内容(「…，另外帮我写一首诗」)同样得 1.000,
    FAQ 会把前半句答掉、忽略后半句。多意图拆分归 M5 的路由层,不在匹配器里解决。
  - 它度量的是「像不像原句」,不是「意思等不等价」。同义改写分数会掉到 0.2~0.7 被拦掉,
    这是**故意的**:FAQ 只答近乎一字不差的问题,改写交给 M3 向量通道。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import jieba
from rank_bm25 import BM25Plus

from project_rag.domain import FaqEntry, FaqMatch
from project_rag.ports import TextMatcher

# 只保留含「词字符」的 token,过滤掉标点和空白(中文标点不在 \w 里)
_WORDISH = re.compile(r"\w")


def _tokens(text: str) -> list[str]:
    """输入:一段文本。行为:jieba 分词,丢掉标点和空白。输出:词列表。"""
    return [
        t for t in (tok.strip() for tok in jieba.lcut(text)) if t and _WORDISH.search(t)
    ]


class BM25TextMatcher(TextMatcher):
    """把 FAQ 问题当文档、用户提问当 query,用 BM25 排序 + 自身分归一化成置信度。"""

    def __init__(self) -> None:
        """输入:无。行为:初始化索引缓存和语料指纹,首次匹配时才建索引。输出:无。"""
        # 指纹:id+question元组序列,用于比对语料是否变了,index存BM25索引,也就是IDF等等参数
        self._fingerprint: tuple[tuple[int, str], ...] | None = None
        self._index: BM25Plus | None = None

    def best_match(self, query: str, candidates: Sequence[FaqEntry]) -> FaqMatch | None:
        """输入:用户问题 + 候选问答对。行为:BM25 打分,再除以该条问自己的分。输出:top1 匹配;候选为空给 None。"""
        entries = list(candidates)
        if not entries:
            return None

        top_index, _, normalized = self._rank(query, entries)
        return FaqMatch(entry=entries[top_index], score=normalized[top_index])

    def score_all(self, query: str, candidates: Sequence[FaqEntry]) -> list[float]:
        """输入:问题 + 候选。行为:按 best_match 的同一口径给每个候选打分。输出:与候选同序的分数。

        给诊断脚本用(calibrate_threshold.py 靠它量「库内近邻有多像」)。
        它必须和 best_match 走同一个私有实现 —— 否则脚本量出来的口径和线上跑的口径是两回事。
        **返回的是归一化分(置信度),不是排序依据**;排序次序见 ``_rank``。
        """
        entries = list(candidates)
        if not entries:
            return []
        _, _, normalized = self._rank(query, entries)
        return normalized

    def _rank(
        self, query: str, entries: list[FaqEntry]
    ) -> tuple[int, list[float], list[float]]:
        """输入:问题 + 候选。行为:先用**原始 BM25 分**定 top1,再把分归一化成置信度。输出:(top1 下标, 原始分, 同序归一化分)。

        排序必须用原始分,不能用归一化分:
          归一化分被夹在 1.0(见下),语料一大就有一批候选同时顶到 1.0,
          这时拿归一化分 argmax 等于按 id 顺序抽签 —— 实测在 463 条真实 FAQ 上,
          给原问题接一句无关请求后,13.8% 的提问会答错条目且分数仍是 1.000(直接越过阈值)。
          归一化分只当**置信度**用,不当排序依据。

        参照系只由 top1 定义:同一个索引里,拿 top1 的问题去问它自己得几分。
        分母与分子同源 —— 语料变大变冗余时两者一起变,所以分数不漂移。
        """
        index = self._index_for(entries)  # ← 唯一改动:不再每次 new
        raw = [float(s) for s in index.get_scores(_tokens(query))]

        # 取第一个最大值(同分时保留 id 更小的那条,顺序稳定)
        top_index = max(range(len(raw)), key=lambda i: raw[i])
        baseline = float(
            index.get_scores(_tokens(entries[top_index].question))[top_index]
        )

        if raw[top_index] <= 0.0 or baseline <= 0.0:
            # 没有可比性(或除零):记 0 分而不是返回 None —— 调用方照样走兜底,
            # 但日志里还能看到最接近的那条。
            return top_index, raw, [0.0] * len(raw)

        # 夹到 0~1:BM25 按 query 词频累计,同一个词在提问里重复多遍会让分子超过分母
        normalized = [max(0.0, min(1.0, s / baseline)) for s in raw]
        return top_index, raw, normalized

    def _index_for(self, entries: list[FaqEntry]) -> BM25Plus:
        """
        语料没变就复用索引,变了就重建。
        输入:问答对列表
        行为:比对 id+question 指纹,变了才重建
        输出:BM25 索引
        """
        fingerprint = tuple((e.id, e.question) for e in entries)
        if fingerprint != self._fingerprint or self._index is None:
            self._index = BM25Plus([_tokens(e.question) for e in entries])
            self._fingerprint = fingerprint
        return self._index

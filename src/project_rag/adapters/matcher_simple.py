"""
字符串相似度匹配适配器:jieba 分词 + difflib 序列比对,BM25后面做,切换方案只需要切换适配器。

为什么是这个组合:中文没有空格,直接对字符做相似度会被标点和语气词拉低;
jieba 先切成词,再对「词序列」做 difflib 比对,改写过的问法也能拿到高分。

这里刻意不做阈值判断 —— 只为「最像的那条」打分,是否命中由用例层决定。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

import jieba

from project_rag.domain import FaqEntry, FaqMatch
from project_rag.ports import TextMatcher

# 只保留含「词字符」的 token,过滤掉标点和空白(中文标点不在 \w 里)
_WORDISH = re.compile(r"\w")


def _tokens(text: str) -> list[str]:
    """jieba 分词,丢掉标点和空白。"""
    return [
        t for t in (tok.strip() for tok in jieba.lcut(text)) if t and _WORDISH.search(t)
    ]


def _ratio(left: list[str], right: list[str]) -> float:
    """两个词序列的相似度(0~1)。

    任一侧为空直接记 0 —— 否则 SequenceMatcher 会把「两个空序列」判成 1.0,
    于是空问题会命中第一条 FAQ。
    """
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


class SimpleTextMatcher(TextMatcher):
    """字符串相似度匹配(先能用,不做语义)。语义化匹配见 docs/deferred.md。"""

    def best_match(self, query: str, candidates: Sequence[FaqEntry]) -> FaqMatch | None:
        query_tokens = _tokens(query)
        best: FaqMatch | None = None
        # 用 `score > best.score` 而不是 max():分数相同时保留 id 更小的那条(顺序稳定)
        for entry in candidates:
            score = _ratio(query_tokens, _tokens(entry.question))
            if best is None or score > best.score:
                best = FaqMatch(entry=entry, score=score)
        return best

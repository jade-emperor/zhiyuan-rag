"""
端口:文本相似匹配能力。

将来升级成嵌入向量匹配时只需要换一个适配器,用例层一行都不改。

只依赖 domain + 标准库。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import FaqEntry, FaqMatch


class TextMatcher(Protocol):
    """在候选里找最像的那一条。

    返回 None 表示「没有任何可比的东西」(候选为空);
    分数高低不代表命中与否 —— 是否命中由用例层拿阈值判定。
    把阈值判断塞进适配器,换匹配算法时阈值语义就会跟着漂移。
    """

    def best_match(
        self, query: str, candidates: Sequence[FaqEntry]
    ) -> FaqMatch | None: ...

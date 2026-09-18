"""
FAQ问答对存储能力模块
"""

# 让类型注解延迟求值、存成字符串，解决前向引用和循环引用，提升启动性能。
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain import FaqEntry


class FaqStore(Protocol):
    """FAQ问答对存储接口"""

    def load_all(self) -> Sequence[FaqEntry]:
        """输入:无。行为:读取全部问答对,顺序稳定。输出:问答对序列。"""
        ...

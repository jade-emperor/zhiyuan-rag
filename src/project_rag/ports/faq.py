"""
FAQ问答对存储能力模块

分两个端口而不是一个:**答题路径只需要读**。把 ``add/update/remove`` 塞进 ``FaqStore``,
每个只读的假实现(验收里有一堆)都得凭空长三个用不到的方法。
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


class FaqEditor(Protocol):
    """问答对的写入能力(管理台)。

    三个方法都只碰一行,不负责校验 —— 空问题、重复问题、超长答案由用例层判,
    存储层只承诺「存进去 / 改到了 / 删掉了」。
    """

    def add(self, question: str, answer: str) -> FaqEntry:
        """输入:问题、答案。行为:插入一行。输出:带新 ID 的问答对。"""
        ...

    def update(
        self, entry_id: int, *, question: str | None = None, answer: str | None = None
    ) -> FaqEntry | None:
        """输入:ID + 要改的字段(``None`` = 不动)。行为:更新并读回。输出:改后的行;ID 不存在给 ``None``。"""
        ...

    def remove(self, entry_id: int) -> int:
        """输入:ID。行为:删掉这一行。输出:删掉的行数(0 = 本来就没有,不抛错)。"""
        ...


class FaqAdminStore(FaqStore, FaqEditor, Protocol):
    """管理台要的两半:读全量(查重与列表)+ 增改删。同一个 MySQL 适配器同时满足。"""

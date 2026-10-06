"""
端口:文档登记表的读写。

**为什么单独一张表、而不是从向量库推**:向量库里只有 chunk 级的 ``doc_id``,
推出"文件名/大小/上传时间/处理状态"是不可能的;而这些恰恰是前端要显示的东西。
**登记表是"有哪些文档"的唯一事实来源**,向量库是它的派生物 —— 删除时按这个顺序清:
标记 ``deleting`` → 删向量 → 删文件 → 删行(见 ``application/documents.py``)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain.documents import DocRecord, DocStatus


class DocumentStore(Protocol):
    """文档登记表的读写。"""

    def init_schema(self) -> None:
        """输入:无。行为:建表(**幂等**)。输出:无。"""
        ...

    def add(self, record: DocRecord) -> None:
        """输入:登记信息。行为:插入一行(主键重复抛错)。输出:无。"""
        ...

    def list_all(self) -> Sequence[DocRecord]:
        """输入:无。行为:全部文档,按创建时间**倒序**(新的在前)。输出:记录序列。"""
        ...

    def get(self, document_id: str) -> DocRecord | None:
        """输入:文档 ID。行为:取一行。输出:记录;没有给 None。"""
        ...

    def set_status(
        self,
        document_id: str,
        status: DocStatus,
        *,
        chunks: int | None = None,
        error: str | None = None,
    ) -> None:
        """输入:文档 ID、新状态、可选块数与错误。行为:改状态(只改给到的字段)。输出:无。"""
        ...

    def remove(self, document_id: str) -> int:
        """输入:文档 ID。行为:删掉这一行。输出:删除条数(0 = 本来就没有)。"""
        ...

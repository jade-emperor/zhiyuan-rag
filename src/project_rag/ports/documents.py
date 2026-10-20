"""
端口:文档登记表的读写。

**为什么单独一张表、而不是从向量库推**:向量库里只有 chunk 级的 ``doc_id``,
推出"文件名/大小/上传时间/处理状态"是不可能的;而这些恰恰是前端要显示的东西。
删除时按这个顺序清:标记 ``deleting`` → 删向量 → 删文件 → 删行(见 ``application/documents.py``)。

**一句话限定**:登记表是"**走过正规写入路径的那些文档**"的事实来源,不是"库里到底有什么"的 ——
``project-rag ingest`` 灌完的语料、有人往语料目录里直接放的文件,登记表都不知道。
清单接口因此要三处对账(见 ``application/documents.py`` 的 ``document_views``),
而写入方(上传与 CLI)有责任把结果登记回来(``upsert``)。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from project_rag.domain.documents import DocRecord, DocStatus, Folder


class DiskFile(Protocol):
    """磁盘上的一个语料文件(**还没读正文**)—— 清单对账要的"磁盘那一处"。

    实现是 ``adapters/document_loader.py`` 的 ``DiskFile``(同名同形,结构化匹配):
    用例层只认识这个协议,不认识那个适配器(分层法律:adapters 之外不许 import adapters)。

    ``doc_ids`` 是"从各个可能的入库根看它"的候选 id,从具体到宽泛 ——
    同一个文件既能当 ``x.md`` 灌进去,也能当 ``bagu_ai_data/x.md`` 灌进去,
    两种登记行都该认出磁盘上这同一份(否则会有一边被误报成「文件不在」)。
    """

    path: Path
    size_bytes: int
    source: str
    doc_ids: tuple[str, ...]

    @property
    def doc_id(self) -> str:
        """输入:无。行为:取首选的那个候选 id(最具体的入库根)。输出:doc_id。"""
        ...


class DocumentStore(Protocol):
    """文档登记表的读写。"""

    def init_schema(self) -> None:
        """输入:无。行为:建表(**幂等**)。输出:无。"""
        ...

    def add(self, record: DocRecord) -> None:
        """输入:登记信息。行为:插入一行(主键重复抛错)。输出:无。"""
        ...

    def upsert(self, record: DocRecord) -> None:
        """输入:登记信息。行为:**整行**写入(有就覆盖,没有就插入)。输出:无。

        与 ``add`` 的分工:``add`` 是"第一次登记"(重名要炸,免得悄悄顶掉别人的行),
        ``upsert`` 是"写入方把这次真的做成的结果登记回来" —— CLI 重灌同一份文件、
        上传重传同名文件都走它。**覆盖的是状态/块数/大小/来源/错误,不覆盖 ``created_at``**:
        那个字段记的是"这份文档第一次出现的时间",重灌不是第一次来。
        """
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

    # ══ 一级目录 ═══════════════════════════════════════════════════════════
    # 目录与文档在**同一个存储**里(同一份 MySQL 配置、同一族表):它们是"文档治理"的
    # 两半,拆成两个工厂只会让"登记一行时顺带写目录"变成一次跨适配器事务。
    # 归属存在 ``documents.folder`` 一列上,空串 = 未分类 —— 所以**读目录的文档清单**
    # 仍然走 ``list_all()``,这一组方法只管目录自己的增删改。
    # 建表/补列/回填都在 ``init_schema()`` 里(启动时那一处),这里不另开一个入口。

    def list_folders(self) -> Sequence[Folder]:
        """输入:无。行为:全部目录(按名字排)。输出:目录序列。"""
        ...

    def get_folder(self, folder_id: str) -> Folder | None:
        """输入:目录 ID。行为:取一个。输出:目录;没有给 None。"""
        ...

    def folder_named(self, name: str) -> Folder | None:
        """输入:目录名。行为:按名字取一个(名字唯一)。输出:目录;没有给 None。"""
        ...

    def add_folder(self, folder: Folder) -> None:
        """输入:目录。行为:插入一行;名字或 ID 重复抛 ValueError。输出:无。"""
        ...

    def update_folder(self, folder: Folder) -> None:
        """输入:目录(整行)。行为:改名字/说明/更新时间(ID 与创建时间不动)。输出:无。"""
        ...

    def remove_folder(self, folder_id: str) -> int:
        """输入:目录 ID。行为:删掉这一行(**不动任何文档**)。输出:删除条数。"""
        ...

    def set_folder(self, document_id: str, folder_id: str) -> int:
        """输入:文档 ID + 目录 ID(空串 = 未分类)。行为:改这一份的归属。输出:改动条数(0 = 没这份文档)。"""
        ...

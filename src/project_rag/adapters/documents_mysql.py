"""
MySQL 文档登记适配器:``documents`` 与 ``document_folders`` 两张表。

**为什么状态用 VARCHAR 而不是 ENUM**:``ENUM`` 改集合要 ALTER TABLE,
而状态机是会长大的(``deleting`` 就是后加的);校验放在 ``domain/documents.py``
的闭集里 —— 读回来时当场校验,脏数据立刻炸,而不是被默默当成某个合法状态。

**目录为什么不是第三张"归属表"**:归属就是 ``documents.folder`` 一列。多一张
``document_folder_map(doc_id, folder_id)`` 只是把同一个键抄了两遍 —— 删文档时忘一次,
就留下一行指向不存在文档的归属,而清单会按它数出一个空的目录。空串 = 未分类,
它**是**一个合法值,不是缺失。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS, align_tables
from project_rag.config import MySQLConfig
from project_rag.domain.documents import DOC_STATUSES, DocRecord, DocStatus, Folder, new_folder_id
from project_rag.observability import get_logger
from project_rag.ports import DocumentStore

_logger = get_logger("adapters.documents_mysql")

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_TABLE = "documents"
_DEFAULT_FOLDERS = "document_folders"

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  document_id VARCHAR(64) PRIMARY KEY,
  source VARCHAR(64) NOT NULL,
  filename VARCHAR(255) NOT NULL,
  status VARCHAR(16) NOT NULL,
  chunks INT NOT NULL DEFAULT 0,
  size_bytes BIGINT NOT NULL DEFAULT 0,
  created_at VARCHAR(32) NOT NULL,
  error VARCHAR(512) NULL,
  folder VARCHAR(64) NOT NULL DEFAULT '',
  KEY idx_documents_created (created_at),
  KEY idx_documents_source (source),
  KEY idx_documents_folder (folder)
) {options}
"""

_FOLDERS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  folder_id VARCHAR(64) PRIMARY KEY,
  name VARCHAR(96) NOT NULL,
  description VARCHAR(255) NOT NULL DEFAULT '',
  created_at VARCHAR(32) NOT NULL,
  updated_at VARCHAR(32) NOT NULL,
  UNIQUE KEY uk_folders_name (name)
) {options}
"""

# 老库补列。``IF NOT EXISTS`` 在 MySQL 8 的 ADD COLUMN 上**不存在**(那是 MariaDB 的方言),
# 所以先问 information_schema 再决定要不要动手 —— 这也是"只回填一次"的判据:
# 列是这次才加上去的,就说明老行还没分过目录。
_FOLDER_COLUMN = "folder"
_COLUMN_DDL = "ALTER TABLE {table} ADD COLUMN folder VARCHAR(64) NOT NULL DEFAULT '' AFTER error"


def _status(value: str) -> DocStatus:
    """输入:库里的状态文本。行为:校验落在闭集里。输出:DocStatus。"""
    if value not in DOC_STATUSES:
        raise RuntimeError(f"库里的文档状态 {value!r} 不在 {DOC_STATUSES} 里 —— 数据被改过?")
    return value  # type: ignore[return-value]


class MySQLDocumentStore(DocumentStore):
    """``documents`` 表的读写。构造不连库。"""

    def __init__(
        self,
        config: MySQLConfig,
        table: str = _DEFAULT_TABLE,
        folders_table: str = _DEFAULT_FOLDERS,
    ) -> None:
        """输入:MySQL 配置、表名(过白名单)、目录表名。行为:记下。输出:无。"""
        for name in (table, folders_table):
            if not _SAFE_TABLE.match(name):
                raise ValueError(f"表名不合法:{name!r}")
        if table == folders_table:
            raise ValueError("文档表与目录表不能同名")
        self._config = config
        self._table = table
        self._folders = folders_table

    def init_schema(self) -> None:
        """输入:无。行为:建表(幂等),并把老表的排序规则扳回来。输出:无。

        ``CREATE TABLE IF NOT EXISTS`` 不给老表改排序规则(理由见 ``adapters/mysql_schema.py``):
        这张表以前只写了 ``CHARSET``,而可见范围那些表写的是 ``COLLATE`` —— 两族混着,
        按 ``document_id`` JOIN 起来就是 1267。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_FOLDERS_DDL.format(table=self._folders, options=TABLE_OPTIONS))
                cur.execute(_DDL.format(table=self._table, options=TABLE_OPTIONS))
                added = self._add_folder_column(cur)
                stale = align_tables(cur, (self._table, self._folders))
                if stale:
                    _logger.info("排序规则已对齐到 %s(老表):%s", COLLATION, "、".join(stale))
                if added:
                    self._backfill_folders(cur)
        finally:
            conn.close()

    def _add_folder_column(self, cur) -> bool:  # noqa: ANN001 - pymysql 游标
        """输入:游标。行为:老库没有 ``folder`` 列就补上。输出:这次是不是**新加的**。

        返回值是"要不要回填"的判据 —— 只有加列的那一次才回填。写成"凡是 folder 为空
        就按来源塞回去"会有一个静默的坏处:用户把文档移回未分类,重启一次又被塞回原目录。
        """
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (self._table, _FOLDER_COLUMN),
        )
        if cur.fetchone() is not None:
            return False
        cur.execute(_COLUMN_DDL.format(table=self._table))
        _logger.info("documents 表补上 folder 列(老库升级)")
        return True

    def _backfill_folders(self, cur) -> None:  # noqa: ANN001 - pymysql 游标
        """输入:游标。行为:把已有文档的 ``source`` 变成同名目录并归进去(只跑一次)。

        **为什么按 source 回填**:老库里唯一的"分组"痕迹就是来源标签(顶层语料目录名
        去掉 ``_data``)。按它建出来的是**物理分组**(``pdf`` / ``doc`` / ``bagu_ai``),
        不是业务分组 —— 但那是库里现成的唯一事实,改名与重分都可以在界面上做;
        凭空编一套"公司制度 / 产品资料"才是把猜测写进数据库。
        """
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute(
            f"SELECT DISTINCT source FROM {self._table} WHERE folder = '' ORDER BY source"
        )
        sources = [str(row[0]) for row in cur.fetchall()]
        for source in sources:
            name = source.strip() or "未命名来源"
            cur.execute(
                f"SELECT folder_id FROM {self._folders} WHERE name = %s", (name,)
            )
            row = cur.fetchone()
            if row is not None:
                folder_id = str(row[0])
            else:
                folder_id = new_folder_id()
                cur.execute(
                    f"INSERT INTO {self._folders}"
                    " (folder_id, name, description, created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (folder_id, name, "由旧数据的来源标签回填", now, now),
                )
            cur.execute(
                f"UPDATE {self._table} SET folder = %s WHERE source = %s AND folder = ''",
                (folder_id, source),
            )
        if sources:
            _logger.info("老数据已按来源归入目录:%s", "、".join(sources))

    def add(self, record: DocRecord) -> None:
        """输入:登记信息。行为:插入一行。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (document_id, source, filename, status, chunks,"
                    " size_bytes, created_at, error, folder)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.document_id,
                        record.source,
                        record.filename,
                        record.status,
                        record.chunks,
                        record.size_bytes,
                        record.created_at,
                        record.error,
                        record.folder,
                    ),
                )
        except pymysql.err.IntegrityError as exc:
            raise ValueError(f"文档 {record.document_id!r} 已登记过") from exc
        finally:
            conn.close()

    def list_all(self) -> Sequence[DocRecord]:
        """输入:无。行为:按 created_at 倒序取全部。输出:记录序列。"""
        return self._query("ORDER BY created_at DESC", ())

    def upsert(self, record: DocRecord) -> None:
        """输入:登记信息。行为:整行写入(有就覆盖,没有就插入)。输出:无。

        ``created_at`` **不在 UPDATE 列表里**:它记的是"这份文档第一次出现的时间",
        重灌不是第一次来(见端口 docstring)。其余字段按本次的真实结果覆盖 ——
        尤其是 ``size_bytes``:它记 0(当年没记过)时"文件改没改"永远判不出来,
        所以写入方必须把真实大小交上来。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (document_id, source, filename, status, chunks,"
                    " size_bytes, created_at, error, folder)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE source = VALUES(source),"
                    " filename = VALUES(filename), status = VALUES(status),"
                    " chunks = VALUES(chunks), size_bytes = VALUES(size_bytes),"
                    " error = VALUES(error), folder = VALUES(folder)",
                    (
                        record.document_id,
                        record.source,
                        record.filename,
                        record.status,
                        record.chunks,
                        record.size_bytes,
                        record.created_at,
                        record.error,
                        record.folder,
                    ),
                )
        finally:
            conn.close()

    def get(self, document_id: str) -> DocRecord | None:
        """输入:文档 ID。行为:取一行。输出:记录或 None。"""
        rows = self._query("WHERE document_id = %s", (document_id,))
        return rows[0] if rows else None

    def set_status(
        self,
        document_id: str,
        status: DocStatus,
        *,
        chunks: int | None = None,
        error: str | None = None,
    ) -> None:
        """输入:文档 ID、状态、可选块数与错误。行为:UPDATE(只改给到的字段)。输出:无。"""
        sets = ["status = %s"]
        values: list[object] = [status]
        if chunks is not None:
            sets.append("chunks = %s")
            values.append(chunks)
        if error is not None:
            sets.append("error = %s")
            values.append(error[:512])
        values.append(document_id)
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._table} SET {', '.join(sets)} WHERE document_id = %s",
                    tuple(values),
                )
        finally:
            conn.close()

    def remove(self, document_id: str) -> int:
        """输入:文档 ID。行为:删行。输出:删除条数。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table} WHERE document_id = %s", (document_id,))
                return int(cur.rowcount)
        finally:
            conn.close()

    # ══ 一级目录 ═══════════════════════════════════════════════════════════
    def list_folders(self) -> Sequence[Folder]:
        """输入:无。行为:全部目录(按名字排)。输出:目录序列。"""
        return self._folder_query("ORDER BY name", ())

    def get_folder(self, folder_id: str) -> Folder | None:
        """输入:目录 ID。行为:取一个。输出:目录;没有给 None。"""
        rows = self._folder_query("WHERE folder_id = %s", (folder_id,))
        return rows[0] if rows else None

    def folder_named(self, name: str) -> Folder | None:
        """输入:目录名。行为:按名字取一个。输出:目录;没有给 None。"""
        rows = self._folder_query("WHERE name = %s", (name,))
        return rows[0] if rows else None

    def add_folder(self, folder: Folder) -> None:
        """输入:目录。行为:插入一行。输出:无。名字或 ID 重复抛 ValueError。

        **重复抛 ValueError 而不是静默覆盖**:目录名是命令行 ``--folder`` 的解析键,
        两个同名目录会让"入到哪个目录"变成一次掷硬币。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._folders}"
                    " (folder_id, name, description, created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (
                        folder.folder_id,
                        folder.name,
                        folder.description,
                        folder.created_at,
                        folder.updated_at,
                    ),
                )
        except pymysql.err.IntegrityError as exc:
            raise ValueError(f"目录名 {folder.name!r} 已被占用") from exc
        finally:
            conn.close()

    def update_folder(self, folder: Folder) -> None:
        """输入:目录(整行)。行为:改名字/说明/更新时间。输出:无。

        ``folder_id`` 与 ``created_at`` **不在 UPDATE 列表里**:ID 是路由与 ``documents.folder``
        的键,改名不该让目录里的文档换主人。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._folders} SET name = %s, description = %s, updated_at = %s"
                    " WHERE folder_id = %s",
                    (folder.name, folder.description, folder.updated_at, folder.folder_id),
                )
        except pymysql.err.IntegrityError as exc:
            raise ValueError(f"目录名 {folder.name!r} 已被占用") from exc
        finally:
            conn.close()

    def remove_folder(self, folder_id: str) -> int:
        """输入:目录 ID。行为:删掉这一行(**不碰任何文档**)。输出:删除条数。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._folders} WHERE folder_id = %s", (folder_id,))
                return int(cur.rowcount)
        finally:
            conn.close()

    def set_folder(self, document_id: str, folder_id: str) -> int:
        """输入:文档 ID + 目录 ID。行为:改这一份的归属。输出:改动条数(0 = 没这份文档)。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._table} SET folder = %s WHERE document_id = %s",
                    (folder_id, document_id),
                )
                return int(cur.rowcount)
        finally:
            conn.close()

    def _folder_query(self, tail: str, params: tuple[object, ...]) -> list[Folder]:
        """输入:SQL 尾巴与参数。行为:查并转成目录。输出:目录列表。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT folder_id, name, description, created_at, updated_at"
                    f" FROM {self._folders} {tail}",
                    params,
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            Folder(
                folder_id=str(r[0]),
                name=str(r[1]),
                description=str(r[2]),
                created_at=str(r[3]),
                updated_at=str(r[4]),
            )
            for r in rows
        ]

    def _query(self, tail: str, params: tuple[object, ...]) -> list[DocRecord]:
        """输入:SQL 尾巴与参数。行为:查并转成记录。输出:记录列表。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT document_id, source, filename, status, chunks, size_bytes,"
                    f" created_at, error, folder FROM {self._table} {tail}",
                    params,
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            DocRecord(
                document_id=r[0],
                source=r[1],
                filename=r[2],
                status=_status(r[3]),
                chunks=int(r[4]),
                size_bytes=int(r[5]),
                created_at=r[6],
                error=r[7],
                folder=str(r[8]),
            )
            for r in rows
        ]

    def _connect(self, *, autocommit: bool):
        """输入:是否自动提交。行为:连库,失败点名 host:port。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),
                database=self._config.database,
                charset="utf8mb4",
                connect_timeout=self._config.connect_timeout_s,
                autocommit=autocommit,
            )
        except pymysql.MySQLError as exc:
            raise RuntimeError(f"连不上 MySQL {target}:{exc}") from exc

"""
MySQL 文档登记适配器:``documents`` 表。

**为什么状态用 VARCHAR 而不是 ENUM**:``ENUM`` 改集合要 ALTER TABLE,
而状态机是会长大的(``deleting`` 就是后加的);校验放在 ``domain/documents.py``
的闭集里 —— 读回来时当场校验,脏数据立刻炸,而不是被默默当成某个合法状态。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS, align_tables
from project_rag.config import MySQLConfig
from project_rag.domain.documents import DOC_STATUSES, DocRecord, DocStatus
from project_rag.observability import get_logger
from project_rag.ports import DocumentStore

_logger = get_logger("adapters.documents_mysql")

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_TABLE = "documents"

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
  KEY idx_documents_created (created_at),
  KEY idx_documents_source (source)
) {options}
"""


def _status(value: str) -> DocStatus:
    """输入:库里的状态文本。行为:校验落在闭集里。输出:DocStatus。"""
    if value not in DOC_STATUSES:
        raise RuntimeError(f"库里的文档状态 {value!r} 不在 {DOC_STATUSES} 里 —— 数据被改过?")
    return value  # type: ignore[return-value]


class MySQLDocumentStore(DocumentStore):
    """``documents`` 表的读写。构造不连库。"""

    def __init__(self, config: MySQLConfig, table: str = _DEFAULT_TABLE) -> None:
        """输入:MySQL 配置、表名(过白名单)。行为:记下。输出:无。"""
        if not _SAFE_TABLE.match(table):
            raise ValueError(f"表名不合法:{table!r}")
        self._config = config
        self._table = table

    def init_schema(self) -> None:
        """输入:无。行为:建表(幂等),并把老表的排序规则扳回来。输出:无。

        ``CREATE TABLE IF NOT EXISTS`` 不给老表改排序规则(理由见 ``adapters/mysql_schema.py``):
        这张表以前只写了 ``CHARSET``,而可见范围那些表写的是 ``COLLATE`` —— 两族混着,
        按 ``document_id`` JOIN 起来就是 1267。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_DDL.format(table=self._table, options=TABLE_OPTIONS))
                stale = align_tables(cur, (self._table,))
                if stale:
                    _logger.info("排序规则已对齐到 %s(老表):%s", COLLATION, "、".join(stale))
        finally:
            conn.close()

    def add(self, record: DocRecord) -> None:
        """输入:登记信息。行为:插入一行。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (document_id, source, filename, status, chunks,"
                    " size_bytes, created_at, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.document_id,
                        record.source,
                        record.filename,
                        record.status,
                        record.chunks,
                        record.size_bytes,
                        record.created_at,
                        record.error,
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
                    " size_bytes, created_at, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE source = VALUES(source),"
                    " filename = VALUES(filename), status = VALUES(status),"
                    " chunks = VALUES(chunks), size_bytes = VALUES(size_bytes),"
                    " error = VALUES(error)",
                    (
                        record.document_id,
                        record.source,
                        record.filename,
                        record.status,
                        record.chunks,
                        record.size_bytes,
                        record.created_at,
                        record.error,
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

    def _query(self, tail: str, params: tuple[object, ...]) -> list[DocRecord]:
        """输入:SQL 尾巴与参数。行为:查并转成记录。输出:记录列表。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT document_id, source, filename, status, chunks, size_bytes,"
                    f" created_at, error FROM {self._table} {tail}",
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

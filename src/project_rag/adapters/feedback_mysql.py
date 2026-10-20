"""
MySQL 适配器:答案反馈(``answer_feedback`` 表)。

三处刻意的设计:

1. **``turn_key`` 唯一**:同一个人对同一轮反复点同一个按钮不该多出一行 ——
   "15 条没帮助"可能只是同一个人在反复点(前端的乐观 UI 会重发);
2. **快照一起存**:被反馈的问题、答案、通道、备注都在这一行里。运维台复盘时
   不需要再按 ``session_id`` 去翻会话(那还得过归属那道门,而且历史可能已被清空);
3. **``LEFT JOIN users``**:账号删了,反馈还在(它是一份质量证据,不是账号的附属物),
   ``username`` 给空串。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS
from project_rag.config import MySQLConfig
from project_rag.domain import FeedbackRecord

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS {table} (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    turn_key CHAR(64) NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(32) NOT NULL,
    rating VARCHAR(8) NOT NULL,
    question TEXT NOT NULL,
    answer MEDIUMTEXT NOT NULL,
    channel VARCHAR(16) NOT NULL DEFAULT '',
    comment VARCHAR(200) NOT NULL DEFAULT '',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_turn_key (turn_key),
    INDEX idx_created_at (created_at),
    INDEX idx_rating (rating)
) {options}"""

_COLUMNS = (
    "f.id, f.session_id, f.user_id, u.username, f.rating, f.question, f.answer,"
    " f.channel, f.comment, f.created_at, f.updated_at"
)


class MySQLFeedbackStore:
    """反馈的 MySQL 实现。构造不连库。"""

    def __init__(self, mysql_config: MySQLConfig, *, table: str = "answer_feedback") -> None:
        """输入:MySQL 配置、表名。行为:校验表名(要拼进 SQL)并记下。输出:无。"""
        if not _SAFE_TABLE.match(table):
            raise ValueError(f"表名不合法:{table!r}")
        self._config = mysql_config
        self._table = table

    def ensure_table(self) -> None:
        """输入:无。行为:建表(幂等)+ 把排序规则扳到当前口径。输出:无。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE.format(table=self._table, options=TABLE_OPTIONS))
                cur.execute(
                    "SELECT TABLE_COLLATION FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                    (self._table,),
                )
                row = cur.fetchone()
                if row is not None and str(row[0]) != COLLATION:
                    cur.execute(
                        f"ALTER TABLE {self._table} CONVERT TO CHARACTER SET utf8mb4"
                        f" COLLATE {COLLATION}"
                    )
        finally:
            conn.close()

    def upsert(
        self,
        *,
        turn_key: str,
        session_id: str,
        user_id: str,
        rating: str,
        question: str,
        answer: str,
        channel: str,
        comment: str,
    ) -> FeedbackRecord:
        """输入:稳定键 + 快照 + 评级。行为:按 ``turn_key`` 覆盖写。输出:落库后的记录。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table}"
                    " (turn_key, session_id, user_id, rating, question, answer, channel, comment)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE rating = VALUES(rating),"
                    " comment = VALUES(comment), channel = VALUES(channel),"
                    " updated_at = CURRENT_TIMESTAMP(3)",
                    (turn_key, session_id, user_id, rating, question, answer, channel, comment),
                )
                cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} f"
                    " LEFT JOIN users u ON u.user_id = f.user_id"
                    " WHERE f.turn_key = %s",
                    (turn_key,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:  # 插入/覆盖之后一定读得到;读不到就是库在说谎
            raise RuntimeError("反馈写入后读不回来")
        return _record(row)

    def remove(self, turn_key: str, user_id: str) -> int:
        """输入:稳定键、当前用户。行为:删**自己**的那一条。输出:删掉的条数。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._table} WHERE turn_key = %s AND user_id = %s",
                    (turn_key, user_id),
                )
                return int(cur.rowcount)
        finally:
            conn.close()

    def list_recent(
        self, *, limit: int, rating: str | None = None, since: datetime | None = None
    ) -> Sequence[FeedbackRecord]:
        """输入:最多几条、只看哪一档(可空)、起始时刻(可空)。行为:按时间倒序。输出:记录序列。"""
        if limit <= 0:
            return ()
        clauses: list[str] = []
        params: list[object] = []
        if rating is not None:
            clauses.append("f.rating = %s")
            params.append(rating)
        if since is not None:
            clauses.append("f.created_at >= %s")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM {self._table} f"
                    " LEFT JOIN users u ON u.user_id = f.user_id"
                    f"{where} ORDER BY f.id DESC LIMIT %s",
                    (*params, limit),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return tuple(_record(row) for row in rows)

    def counts(self, *, since: datetime | None = None) -> tuple[int, int]:
        """输入:起始时刻(可空)。行为:按评级分组计数。输出:``(up, down)``。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if since is None:
                    cur.execute(f"SELECT rating, COUNT(*) FROM {self._table} GROUP BY rating")
                else:
                    cur.execute(
                        f"SELECT rating, COUNT(*) FROM {self._table}"
                        " WHERE created_at >= %s GROUP BY rating",
                        (since,),
                    )
                rows = cur.fetchall()
        finally:
            conn.close()
        found = {str(row[0]): int(row[1]) for row in rows}
        return found.get("up", 0), found.get("down", 0)

    def _connect(self) -> pymysql.connections.Connection:
        """输入:无。行为:连库(自动提交),失败点名 host:port。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),
                database=self._config.database,
                connect_timeout=self._config.connect_timeout_s,
                charset="utf8mb4",
                autocommit=True,
            )
        except Exception as exc:
            raise RuntimeError(f"MySQL 连接失败（{target}）：{exc}") from exc


def _record(row: Sequence[object]) -> FeedbackRecord:
    """输入:一行(列表套元组)。行为:转成领域类型。输出:FeedbackRecord。"""
    return FeedbackRecord(
        id=int(str(row[0])),
        session_id=str(row[1]),
        user_id=str(row[2]),
        username=str(row[3] or ""),
        rating=str(row[4]),
        question=str(row[5]),
        answer=str(row[6]),
        channel=str(row[7] or ""),
        comment=str(row[8] or ""),
        created_at=_stamp(row[9]),
        updated_at=_stamp(row[10]),
    )


def _stamp(value: object) -> str:
    """输入:库里的时间。行为:统一成 ISO 字符串(与会话/文档同一格式)。输出:字符串。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)

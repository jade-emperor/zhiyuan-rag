"""
MySQL 会话目录适配器:把 ``session_owner``(谁的)与 ``conversations``(聊了什么)拼起来。

**为什么是一次 JOIN 而不是"先列归属、再逐个查轮数"**:后者是 N+1 —— 20 段会话就是
21 次往返,而这里只需要两次固定查询(第二次只为取标题)。

**为什么标题要单独一次查询**:MySQL 没有"取每组第一行"的窗口语法糖(8.0 有
``ROW_NUMBER``,但把窗口函数写进这条按需查询里,读的人得先在脑子里跑一遍分区)。
用 ``MIN(id)`` 选出每段会话的第一轮再 JOIN 回来,SQL 短、语义直白,而且
``id`` 是自增主键,``MIN(id)`` 就是"最早那一轮"。

**为什么标题不在 SQL 里截**:裁剪规则(压空白、按字符数)是领域规则,
放在 ``domain.sessions.session_title`` 一处 —— 写在 SQL 里就要用 ``LEFT()`` 按字节切,
中文会切出半个字。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

import pymysql

from project_rag.config import ConversationConfig, MySQLConfig
from project_rag.domain.sessions import SessionSummary, session_title
from project_rag.ports import SessionCatalog

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MySQLSessionCatalog:
    """会话目录的 MySQL 实现(只读:不建表、不改任何行)。"""

    def __init__(
        self,
        mysql_config: MySQLConfig,
        conversation_config: ConversationConfig,
        *,
        owners_table: str = "session_owner",
    ) -> None:
        """输入:MySQL 配置、会话配置(取表名)、归属表名。行为:校验表名并记下。输出:无。"""
        for table in (conversation_config.table, owners_table):
            if not _SAFE_TABLE.match(table):
                raise ValueError(f"表名不合法:{table!r};只允许字母、数字、下划线")
        self._config = mysql_config
        self._table = conversation_config.table
        self._owners = owners_table

    def list_for(self, user_id: str, *, limit: int) -> Sequence[SessionSummary]:
        """输入:用户 ID、最多几条。行为:两次查询(聚合 + 标题)。输出:摘要序列(最近活动在前)。

        ``limit <= 0`` 直接给空序列而不是抛错:那是"不要任何东西"的合法表达,
        而 SQL 的 ``LIMIT 0`` 与 ``LIMIT -1`` 行为完全不同(后者是语法错)。
        """
        if limit <= 0:
            return ()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT o.session_id, o.created_at, COUNT(c.id), MAX(c.created_at)"
                    f" FROM {self._owners} o JOIN {self._table} c"
                    " ON c.session_id = o.session_id"
                    " WHERE o.user_id = %s"
                    " GROUP BY o.session_id, o.created_at"
                    " ORDER BY MAX(c.created_at) DESC, o.created_at DESC"
                    " LIMIT %s",
                    (user_id, limit),
                )
                rows = cur.fetchall()
                if not rows:
                    return ()
                titles = self._titles(cur, [str(row[0]) for row in rows])
        finally:
            conn.close()
        return tuple(
            SessionSummary(
                session_id=str(session_id),
                # 取不到标题给空串(前端显示「新会话」)—— 不给它编一个占位标题
                title=titles.get(str(session_id), ""),
                rounds=int(rounds),
                created_at=_stamp(created_at),
                updated_at=_stamp(updated_at),
            )
            for session_id, created_at, rounds, updated_at in rows
        )

    def _titles(self, cur: pymysql.cursors.Cursor, session_ids: list[str]) -> dict[str, str]:
        """输入:游标、会话 ID 列表。行为:取每段会话**第一轮**的问题并裁成标题。输出:ID -> 标题。

        占位符按数量拼:``IN (%s, %s, …)`` 的参数个数必须是常量,不能把列表塞进一个 ``%s``
        (那会变成拿一个元组去比字符串,永远不匹配 —— 静默返回空标题)。
        """
        placeholders = ", ".join(["%s"] * len(session_ids))
        cur.execute(
            f"SELECT c.session_id, c.question FROM {self._table} c"
            f" JOIN (SELECT session_id, MIN(id) AS id FROM {self._table}"
            f" WHERE session_id IN ({placeholders}) GROUP BY session_id) f ON f.id = c.id",
            tuple(session_ids),
        )
        return {
            str(session_id): session_title(str(question)) for session_id, question in cur.fetchall()
        }

    def _connect(self):
        """输入:无。行为:连库,失败点名 host:port。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                database=self._config.database,
                charset="utf8mb4",
                autocommit=True,
            )
        except pymysql.Error as exc:
            raise RuntimeError(f"连不上 MySQL({target}):{exc}") from exc


def _stamp(value: object) -> str:
    """输入:库里的时间(``datetime`` 或已经是字符串)。行为:统一成 ISO 字符串。输出:字符串。

    与 ``documents.created_at`` 用同一个格式(``isoformat(timespec="seconds")``,带 ``T``、
    不带时区):前端只面对一种时间形状,不用为两张表写两种解析。
    """
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)

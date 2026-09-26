"""
MySQL 适配器:对话历史的存取(``conversations`` 表)。

三处刻意的设计,每条都有理由:

1. **排序用 ``id`` 而不是 ``timestamp``**:DATETIME 只精确到秒,同一秒里的两轮
   谁先谁后由 MySQL 自己定 —— 「最近 5 轮」会随排序抖动,而且这种抖动**查不出来**
   (它只在同一秒内连续提问时出现)。自增主键才是唯一的稳定顺序。时间戳仍然留着
   (``created_at``),但只用来给人看。
2. **列名 ``created_at`` 而不是 ``timestamp``**:后者与 MySQL 的类型名同名,
   读 SQL 时容易看岔;语义完全一样。
3. **裁剪的删除语句带一层派生表**:MySQL 不允许在 ``IN`` 的子查询里直接写 ``LIMIT``
   ("This version of MySQL doesn't yet support 'LIMIT & IN/ALL/ANY/SOME subquery'"),
   所以那层 ``SELECT ... FROM (...) AS keep_rows`` 不是装饰,删掉就报语法错。

表名要拼进 SQL(参数化查询管不到表名),所以过一层白名单校验:
配置里写错一个字符就可能拼出别的语句。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import pymysql

from project_rag.config import ConversationConfig, MySQLConfig
from project_rag.domain import ConversationTurn
from project_rag.ports import ConversationStore

_CONNECT_TIMEOUT_S = 5
_SAFE_TABLE = re.compile(r"^[A-Za-z0-9_]+$")

# 表结构。``check_m5_history.py`` 的 docstring 里是同一份契约(改这里就要同步那里)。
_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS {table} (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_id (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

# VARCHAR(36) 就是 uuid4 的长度;超长会在 MySQL 严格模式下报错,这里提前给出人话
_MAX_SESSION_CHARS = 36


def _require_session(session_id: str) -> str:
    """输入:调用方给的会话 ID。行为:非空且不超长才放行。输出:原样的 ID。"""
    if not session_id.strip():
        raise ValueError("session_id 不能为空 —— 空串会被当成一个真实的会话写进库里")
    if len(session_id) > _MAX_SESSION_CHARS:
        raise ValueError(
            f"session_id 最长 {_MAX_SESSION_CHARS} 字符(表里列的定义),"
            f"收到 {len(session_id)} 字符;不静默截断 —— 截断会把两个会话并成一个"
        )
    return session_id


class MySQLConversationStore(ConversationStore):
    def __init__(
        self, mysql_config: MySQLConfig, conversation_config: ConversationConfig
    ) -> None:
        """输入:MySQL 配置 + 会话配置。行为:记下连接参数与表名(表名过白名单),不建连接。输出:无。"""
        if not _SAFE_TABLE.match(conversation_config.table):
            raise ValueError(
                f"表名不合法:{conversation_config.table!r};只允许字母、数字、下划线"
            )
        self._config = mysql_config
        self._table = conversation_config.table

    def ensure_table(self) -> None:
        """输入:无。行为:建表(``IF NOT EXISTS``,幂等)。输出:无。

        刻意**不放在 ``__init__`` 里**:构造适配器不该有副作用(``--help`` 也可能构造它)。
        生产上由 ``scripts/tools/init_conversation.py`` 跑一次,服务启动时也调它。
        """
        target = f"{self._config.host}:{self._config.port}"
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE.format(table=self._table))
        except Exception as exc:
            raise RuntimeError(f"建表失败（{target}.{self._table}）：{exc}") from exc
        finally:
            conn.close()

    def record(self, session_id: str, turn: ConversationTurn, *, keep: int) -> None:
        """输入:会话 ID、这一轮的问答、保留轮数。行为:插入 + 裁剪到最近 keep 轮(一个事务)。输出:无。"""
        sid = _require_session(session_id)
        if keep <= 0:
            raise ValueError(f"keep 必须为正数,收到 {keep}")

        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (session_id, question, answer) "
                    "VALUES (%s, %s, %s)",
                    (sid, turn.question, turn.answer),
                )
                cur.execute(
                    f"DELETE FROM {self._table} WHERE session_id = %s AND id NOT IN ("
                    f"  SELECT id FROM ("
                    f"    SELECT id FROM {self._table} WHERE session_id = %s "
                    "ORDER BY id DESC LIMIT %s"
                    f"  ) AS keep_rows)",
                    (sid, sid, keep),
                )
            # 写入与裁剪一起生效:要么这一轮在库里、要么这一轮和裁剪都没发生
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recent(self, session_id: str, *, limit: int) -> Sequence[ConversationTurn]:
        """输入:会话 ID、最多取几轮。行为:取最近 limit 轮并翻成时间正序。输出:问答轮序列。"""
        sid = _require_session(session_id)
        if limit <= 0:
            return ()

        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT question, answer FROM {self._table} "
                    "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                    (sid, limit),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # SQL 里按 id 倒序取「最近的 limit 轮」,返回前翻正 —— 端口承诺时间正序
        return tuple(
            ConversationTurn(question=str(row[0]), answer=str(row[1]))
            for row in reversed(rows)
        )

    def clear(self, session_id: str) -> int:
        """输入:会话 ID。行为:删掉该会话的全部历史。输出:删掉的轮数。"""
        sid = _require_session(session_id)
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table} WHERE session_id = %s", (sid,))
                return int(cur.rowcount)
        finally:
            conn.close()

    def _connect(self, *, autocommit: bool):
        """输入:是否自动提交。行为:连库,连不上抛错点名 host:port(同 FAQ 适配器)。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),
                database=self._config.database,
                connect_timeout=_CONNECT_TIMEOUT_S,
                charset="utf8mb4",
                autocommit=autocommit,
            )
        except Exception as exc:
            raise RuntimeError(
                f"MySQL 连接失败（{target}）：{type(exc).__name__}: {exc}"
            ) from exc

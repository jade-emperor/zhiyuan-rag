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

import json
import re
from collections.abc import Sequence
from dataclasses import asdict

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS, align_tables
from project_rag.observability import get_logger
from project_rag.config import ConversationConfig, MySQLConfig
from project_rag.domain import CitationSnapshot, ConversationTurn
from project_rag.ports import ConversationStore

_logger = get_logger("adapters.conversation_mysql")

_SAFE_TABLE = re.compile(r"^[A-Za-z0-9_]+$")

# 表结构。验收脚本的 docstring 里是同一份契约(改这里就要同步那里)。
_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS {table} (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_id (session_id)
) {options}"""

# VARCHAR(36) 就是 uuid4 的长度;超长会在 MySQL 严格模式下报错,这里提前给出人话
_MAX_SESSION_CHARS = 36

def _dump_citations(citations: Sequence[CitationSnapshot]) -> str | None:
    """输入:这一轮的引用快照。行为:序列化成 JSON 文本。输出:文本;没有引用给 ``None``。

    存 ``None`` 而不是 ``"[]"``:两者读回来都是空元组,但``NULL`` 更省地方,
    而且"老行没有这一列"与"这一轮没有出处"在数据上长得一样 —— 界面本来也分不出、不需要分。
    """
    if not citations:
        return None
    return json.dumps([asdict(item) for item in citations], ensure_ascii=False)


def _load_citations(raw: object) -> tuple[CitationSnapshot, ...]:
    """输入:库里的 JSON 列(pymysql 可能给 str / bytes / None)。行为:翻回快照元组。输出:快照元组。

    **坏数据当场炸**(与 ``auth_mysql`` 读角色同一条口径):这一列只由 ``record`` 写,
    解不开说明有人手改过库 —— 那时静默给空元组会让"引用不见了"变成一个查不出来的谜。
    """
    if raw is None or raw == "":
        return ()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"citations 列不是合法 JSON(有人手改过库?):{text[:120]!r}") from exc
    try:
        return tuple(
            CitationSnapshot(
                index=int(item["index"]),
                source=str(item["source"]),
                doc_id=str(item["doc_id"]),
                preview=str(item["preview"]),
                score=float(item["score"]),
                rerank_score=(
                    None if item.get("rerank_score") is None else float(item["rerank_score"])
                ),
                page=None if item.get("page") is None else int(item["page"]),
            )
            for item in items
        )
    except (KeyError, TypeError, ValueError) as exc:
        # 形状不对只有一种来路:写入方没走 ``domain.snapshot_citations``(把整个 Citation
        # 塞进来就会这样 —— 它的字段是 chunk/score,没有 source)。当场炸并点出来路,
        # 别让它变成一个"引用莫名其妙不见了"的谜。
        raise RuntimeError(
            f"citations 列的形状不对({type(exc).__name__}: {exc});"
            f"这一列只由 record 写,字段来自 domain.snapshot_citations —— {text[:120]!r}"
        ) from exc


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
                cur.execute(_CREATE_TABLE.format(table=self._table, options=TABLE_OPTIONS))
                # 老表(加 citations 之前建的)不会因为 IF NOT EXISTS 长出这一列 ——
                # 补一次,否则"切回旧会话看不到引用"会一直在,而且只在老库上表现
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
                    (self._table, "citations"),
                )
                found = cur.fetchone()
                if not found or not found[0]:
                    cur.execute(f"ALTER TABLE {self._table} ADD COLUMN citations JSON NULL")
                # 老表只写了 CHARSET(见 adapters/mysql_schema.py):与另一族表 JOIN 报 1267。
                # 会话目录正是按 session_id JOIN 这张表 —— 那一次的表现是"整页会话读不出来"。
                stale = align_tables(cur, (self._table,))
                if stale:
                    _logger.info("排序规则已对齐到 %s(老表):%s", COLLATION, "、".join(stale))
        except Exception as exc:
            raise RuntimeError(f"建表失败（{target}.{self._table}）：{exc}") from exc
        finally:
            conn.close()

    def record(self, session_id: str, turn: ConversationTurn, *, keep: int) -> None:
        """输入:会话 ID、这一轮的问答(+引用快照)、保留轮数。行为:插入 + 裁剪到最近 keep 轮(一个事务)。输出:无。

        ``citations`` 存成 JSON 列:没有引用时写 ``NULL``(不是 ``[]``),
        这样"老行没这列/没引用"与"这一轮真的没有出处"在读回来时都得到空元组 —— 两者对界面是一回事。
        """
        sid = _require_session(session_id)
        if keep <= 0:
            raise ValueError(f"keep 必须为正数,收到 {keep}")

        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (session_id, question, answer, citations) "
                    "VALUES (%s, %s, %s, %s)",
                    (sid, turn.question, turn.answer, _dump_citations(turn.citations)),
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
                    f"SELECT question, answer, citations FROM {self._table} "
                    "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                    (sid, limit),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        # SQL 里按 id 倒序取「最近的 limit 轮」,返回前翻正 —— 端口承诺时间正序
        return tuple(
            ConversationTurn(
                question=str(row[0]),
                answer=str(row[1]),
                citations=_load_citations(row[2]),
            )
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
                connect_timeout=self._config.connect_timeout_s,
                charset="utf8mb4",
                autocommit=autocommit,
            )
        except Exception as exc:
            raise RuntimeError(
                f"MySQL 连接失败（{target}）：{type(exc).__name__}: {exc}"
            ) from exc

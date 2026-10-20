"""
mysql适配器,FAQ问答对的存储实现

读写在同一个类里(答题路径与运维台用的是同一张表),但**接口分两个端口**:
``FaqStore`` 只有 ``load_all``,``FaqEditor`` 才是增改删 —— 见 ``ports/faq.py``。
"""

# 注解延迟求值(存成字符串),省掉导入时的求值开销
from __future__ import annotations

import re
from collections.abc import Sequence

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS
from project_rag.config import FaqConfig, MySQLConfig
from project_rag.domain import FaqEntry
from project_rag.ports import FaqAdminStore

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 与老表同形:问题 512 字(与页面上的上限一致),答案 TEXT。
_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS {table} (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    question VARCHAR(512) NOT NULL,
    answer TEXT NOT NULL
) {options}"""


class MySQLFaqStore(FaqAdminStore):
    def __init__(self, mysql_config: MySQLConfig, faq_config: FaqConfig) -> None:
        """输入:MySQL 配置 + FAQ 配置。行为:校验表名并记下连接参数,不建连接。输出:无。"""
        if not _SAFE_TABLE.match(faq_config.table):
            raise ValueError(f"表名不合法:{faq_config.table!r}")
        self._config = mysql_config
        self._table = faq_config.table

    # ── 读 ────────────────────────────────────────────────────────────────
    def load_all(self) -> Sequence[FaqEntry]:
        """输入:无。行为:连库查全表并按 id 排序,连不上抛错点名 host:port。输出:领域问答对序列。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # 表名只来自配置,不来自用户输入;ORDER BY id 是契约要求的稳定顺序
                cur.execute(f"SELECT id, question, answer FROM {self._table} ORDER BY id ASC")
                rows = cur.fetchall()
        finally:
            conn.close()  # 无论成败都要关连接
        return [_entry(row) for row in rows]

    # ── 写(管理台)──────────────────────────────────────────────────────
    def ensure_table(self) -> None:
        """输入:无。行为:建表(幂等)+ 把老表的排序规则扳到当前口径。输出:无。"""
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
                    # 表名过了白名单(且来自配置),拼进 DDL 是安全的
                    cur.execute(
                        f"ALTER TABLE {self._table} CONVERT TO CHARACTER SET utf8mb4"
                        f" COLLATE {COLLATION}"
                    )
        finally:
            conn.close()

    def add(self, question: str, answer: str) -> FaqEntry:
        """输入:问题、答案。行为:插入一行并读回(用自增主键)。输出:问答对。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (question, answer) VALUES (%s, %s)",
                    (question, answer),
                )
                entry_id = int(cur.lastrowid or 0)
        finally:
            conn.close()
        return FaqEntry(id=entry_id, question=question, answer=answer)

    def update(
        self, entry_id: int, *, question: str | None = None, answer: str | None = None
    ) -> FaqEntry | None:
        """输入:ID + 要改的字段。行为:更新后**读回**那一行(列名只来自本文件)。输出:改后的行;ID 不存在给 None。

        读回而不是拿入参拼:只改了一个字段时,另一个字段的现值只有库里有。
        """
        sets: list[str] = []
        params: list[object] = []
        if question is not None:
            sets.append("question = %s")
            params.append(question)
        if answer is not None:
            sets.append("answer = %s")
            params.append(answer)
        if not sets:
            return self._get(entry_id)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._table} SET {', '.join(sets)} WHERE id = %s",
                    (*params, entry_id),
                )
                cur.execute(
                    f"SELECT id, question, answer FROM {self._table} WHERE id = %s",
                    (entry_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return None if row is None else _entry(row)

    def remove(self, entry_id: int) -> int:
        """输入:ID。行为:删掉这一行。输出:删掉的行数(0 = 本来就没有)。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table} WHERE id = %s", (entry_id,))
                return int(cur.rowcount)
        finally:
            conn.close()

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _get(self, entry_id: int) -> FaqEntry | None:
        """输入:ID。行为:读一行。输出:问答对或 None。"""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, question, answer FROM {self._table} WHERE id = %s",
                    (entry_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return None if row is None else _entry(row)

    def _connect(self) -> pymysql.connections.Connection:
        """输入:无。行为:连库(自动提交),失败点名 host:port。输出:连接。"""
        target = f"{self._config.host}:{self._config.port}"
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password.get_secret_value(),  # 取明文
                database=self._config.database,
                connect_timeout=self._config.connect_timeout_s,
                charset="utf8mb4",
                autocommit=True,  # 自动提交事务
            )
        except Exception as exc:
            raise RuntimeError(
                f"MySQL 连接失败（{target}）：{type(exc).__name__}: {exc}"
            ) from exc  # 保留原始异常信息链


def _entry(row: Sequence[object]) -> FaqEntry:
    """输入:一行(列表套元组)。行为:转成领域类型。输出:问答对。"""
    return FaqEntry(id=int(str(row[0])), question=str(row[1]), answer=str(row[2]))


if __name__ == "__main__":
    from project_rag.config import get_settings

    s = get_settings()
    result = MySQLFaqStore(s.mysql, s.faq).load_all()
    for e in result:
        print(f"id:{e.id}    问题: {e.question}    回答: {e.answer}")

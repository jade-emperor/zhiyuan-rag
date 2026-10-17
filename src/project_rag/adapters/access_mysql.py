"""
MySQL 可见范围适配器:组 / 组员 / 文档授权三张表。

**为什么是独立的三张表、而不是给 ``documents`` 加一列**:一份文档可以授权给"很多人 + 很多组",
那是一对多,一个列存不下(存 JSON 就得整列读回来再解析,而且**没法按组反查** ——
"哪些文档授权给了销售部"是最常问的一个问题)。三张表各自回答一个问题:
有哪些组 / 谁在组里 / 这份文档给了谁。

**为什么授权表用一行一个对象,而不是"用户列 + 组列"**:两类对象是同一件事的两种取值
(``subject_kind`` ∈ user/group),拆成两列就要有一列永远为空,而且每次读回来都要
"两列拼一份名单"(拼错了不会报错,只是少了几个能看的人)。合起来之后
"把这行加进去"就是"给这个人授权",没有第二种写法。

**为什么没有外键**:``document_id`` 指向的是登记表 —— 而登记表、向量库、磁盘三处
本来就允许不一致(见 ``application/documents.py`` 的三处对账)。加外键会把"对不上"
变成写不进去,反而挡住对账这条路。**代价是删文档时必须显式清授权**
(``forget_document``),这条在用例里是有调用点的(不是"以后记得")。

**唯一索引与主键的取法**:组名唯一(重名建组会串权限);组员表的主键是
``(group_id, user_id)``(同一个人重复入组是幂等的,不是两行);授权表的主键是
``(document_id, subject_kind, subject_id)`` —— 于是"重复授权同一个人"天然幂等,
不需要调用方先去重。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

import pymysql

from project_rag.adapters.mysql_schema import COLLATION, TABLE_OPTIONS, align_tables
from project_rag.config import MySQLConfig
from project_rag.domain.access import SUBJECT_KINDS, DocumentAcl, Group
from project_rag.observability import get_logger
from project_rag.ports import AccessStore

_logger = get_logger("adapters.access_mysql")

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DEFAULT_TABLES = {
    "groups": "acl_groups",
    "members": "acl_group_members",
    "subjects": "acl_document_subjects",
}

_DDL = (
    """
CREATE TABLE IF NOT EXISTS {groups} (
  group_id VARCHAR(32) PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE,
  created_at DATETIME(3) NOT NULL
) {options}
""",
    """
CREATE TABLE IF NOT EXISTS {members} (
  group_id VARCHAR(32) NOT NULL,
  user_id VARCHAR(32) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (group_id, user_id),
  KEY idx_members_user (user_id)
) {options}
""",
    """
CREATE TABLE IF NOT EXISTS {subjects} (
  document_id VARCHAR(64) NOT NULL,
  subject_kind VARCHAR(8) NOT NULL,
  subject_id VARCHAR(32) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  PRIMARY KEY (document_id, subject_kind, subject_id),
  KEY idx_subjects_subject (subject_kind, subject_id)
) {options}
""",
)


def _kind(value: str) -> str:
    """输入:库里的授权对象类型。行为:校验落在闭集里。输出:类型文本。

    脏数据必须当场炸 —— 默默当成 ``user`` 会让一个**组**的授权被读成"某个 ID"
    (组 ID 与用户 ID 都长得像随机串),于是那些人的权限凭空消失,而且不报错。
    """
    if value not in SUBJECT_KINDS:
        raise RuntimeError(f"库里的授权对象类型 {value!r} 不在 {SUBJECT_KINDS} 里 —— 数据被改过?")
    return value


def _group_id() -> str:
    """输入:无。行为:生成组 ID。输出:32 位十六进制。

    与用户 ID 同一套(``application.security.new_user_id``):随机而不是自增 ——
    自增 ID 会让"猜下一个组"变成可能,而组 ID 会出现在接口参数里。
    """
    from project_rag.application.security import new_user_id

    return new_user_id()


class MySQLAccessStore(AccessStore):
    """组 / 组员 / 文档授权名单的读写。构造不连库。"""

    def __init__(
        self,
        config: MySQLConfig,
        *,
        groups_table: str = _DEFAULT_TABLES["groups"],
        members_table: str = _DEFAULT_TABLES["members"],
        subjects_table: str = _DEFAULT_TABLES["subjects"],
    ) -> None:
        """输入:MySQL 配置 + 三个表名。行为:校验表名(白名单)并记下。输出:无。"""
        for table in (groups_table, members_table, subjects_table):
            if not _SAFE_TABLE.match(table):
                raise ValueError(f"表名不合法:{table!r}")
        self._config = config
        self._groups = groups_table
        self._members = members_table
        self._subjects = subjects_table

    def init_schema(self) -> None:
        """输入:无。行为:建三张表(幂等),并把老表的排序规则扳回来。输出:无。

        ``CREATE TABLE IF NOT EXISTS`` 不给老表改排序规则,而这一族表以前建的时候只写了
        ``CHARSET`` —— 混着的两族表一旦 JOIN 就是 1267(见 ``adapters/mysql_schema.py``)。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                for ddl in _DDL:
                    cur.execute(
                        ddl.format(
                            groups=self._groups,
                            members=self._members,
                            subjects=self._subjects,
                            options=TABLE_OPTIONS,
                        )
                    )
                stale = align_tables(cur, (self._groups, self._members, self._subjects))
                if stale:
                    _logger.info("排序规则已对齐到 %s(老表):%s", COLLATION, "、".join(stale))
        finally:
            conn.close()

    # ── 组 ────────────────────────────────────────────────────────────────
    def create_group(self, name: str) -> Group:
        """输入:组名。行为:插入一行(重名抛错)。输出:新建的组。"""
        group = Group(group_id=_group_id(), name=name)
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._groups} (group_id, name, created_at) VALUES (%s, %s, %s)",
                    (group.group_id, group.name, datetime.now()),
                )
        except pymysql.err.IntegrityError as exc:
            raise ValueError(f"组名 {name!r} 已存在") from exc
        finally:
            conn.close()
        return group

    def list_groups(self) -> Sequence[Group]:
        """输入:无。行为:按名字升序取全部组。输出:组序列。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT group_id, name FROM {self._groups} ORDER BY name")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [Group(group_id=row[0], name=row[1]) for row in rows]

    def delete_group(self, group_id: str) -> int:
        """输入:组 ID。行为:删组 + 它的组员 + 授权给它的那些行(一个事务)。输出:删掉的组行数。"""
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._subjects} WHERE subject_kind = 'group' AND subject_id = %s",
                    (group_id,),
                )
                cur.execute(f"DELETE FROM {self._members} WHERE group_id = %s", (group_id,))
                cur.execute(f"DELETE FROM {self._groups} WHERE group_id = %s", (group_id,))
                removed = int(cur.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return removed

    # ── 组员 ──────────────────────────────────────────────────────────────
    def set_members(self, group_id: str, user_ids: Sequence[str]) -> None:
        """输入:组 ID + 整份成员名单。行为:一个事务里先清后插。输出:无。

        **先清后插而不是逐个 diff**:名单通常就几个人,而 diff 要先把现状读回来再算 ——
        多一次往返、多一处"算错了但看起来没事"。清空再插入在事务里是原子的,
        中途失败回滚后仍是原来那份名单(不会留下半份)。
        """
        wanted = list(dict.fromkeys(user_ids))  # 去重且保序:重复的 ID 是调用方的手误,不该炸
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._members} WHERE group_id = %s", (group_id,))
                if wanted:
                    now = datetime.now()
                    cur.executemany(
                        f"INSERT INTO {self._members} (group_id, user_id, created_at)"
                        " VALUES (%s, %s, %s)",
                        [(group_id, user_id, now) for user_id in wanted],
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def members_of(self, group_id: str) -> Sequence[str]:
        """输入:组 ID。行为:取成员 ID(升序)。输出:ID 序列。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id FROM {self._members} WHERE group_id = %s ORDER BY user_id",
                    (group_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [str(row[0]) for row in rows]

    def groups_by_user(self) -> Mapping[str, tuple[str, ...]]:
        """输入:无。行为:一次读全部组员行,按用户归拢。输出:``user_id -> 组 ID 元组``。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id, group_id FROM {self._members} ORDER BY user_id, group_id")
                rows = cur.fetchall()
        finally:
            conn.close()
        grouped: dict[str, list[str]] = {}
        for user_id, group_id in rows:
            grouped.setdefault(str(user_id), []).append(str(group_id))
        return {user_id: tuple(groups) for user_id, groups in grouped.items()}

    # ── 文档授权 ──────────────────────────────────────────────────────────
    def set_document_subjects(
        self, document_id: str, *, user_ids: Sequence[str], group_ids: Sequence[str]
    ) -> None:
        """输入:文档 ID + 两份整名单。行为:一个事务里先清后插;两份都空就是删规则。输出:无。"""
        users = list(dict.fromkeys(user_ids))
        groups = list(dict.fromkeys(group_ids))
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._subjects} WHERE document_id = %s", (document_id,))
                rows = [
                    (document_id, "user", subject_id, datetime.now()) for subject_id in users
                ] + [(document_id, "group", subject_id, datetime.now()) for subject_id in groups]
                if rows:
                    cur.executemany(
                        f"INSERT INTO {self._subjects} (document_id, subject_kind, subject_id,"
                        " created_at) VALUES (%s, %s, %s, %s)",
                        rows,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def subjects_of(self, document_id: str) -> DocumentAcl:
        """输入:文档 ID。行为:取它的授权对象(没有规则给空的那一份)。输出:``DocumentAcl``。"""
        rows = self._subject_rows("WHERE document_id = %s", (document_id,))
        return _acl_of(document_id, [(kind, subject) for _doc, kind, subject in rows])

    def rules(self) -> Mapping[str, DocumentAcl]:
        """输入:无。行为:一次读全部授权行并按文档归拢。输出:``document_id -> DocumentAcl``。"""
        grouped: dict[str, list[tuple[str, str]]] = {}
        for document_id, kind, subject_id in self._subject_rows("", ()):
            grouped.setdefault(document_id, []).append((kind, subject_id))
        return {
            document_id: _acl_of(document_id, subjects)
            for document_id, subjects in grouped.items()
        }

    def forget_document(self, document_id: str) -> int:
        """输入:文档 ID。行为:删掉它的授权行。输出:删掉的行数。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._subjects} WHERE document_id = %s", (document_id,))
                removed = int(cur.rowcount)
        finally:
            conn.close()
        return removed

    def forget_user(self, user_id: str) -> int:
        """输入:用户 ID。行为:删组员行 + 授权行里以他为对象的那些(一个事务)。输出:删掉的行数。"""
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._members} WHERE user_id = %s", (user_id,))
                removed = int(cur.rowcount)
                cur.execute(
                    f"DELETE FROM {self._subjects} WHERE subject_kind = 'user' AND subject_id = %s",
                    (user_id,),
                )
                removed += int(cur.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return removed

    def _subject_rows(self, where: str, params: tuple) -> list[tuple[str, str, str]]:
        """输入:WHERE 片段与参数。行为:读授权行(已校验类型)。输出:``(文档, 类型, 对象)`` 列表。

        ``where`` 只由本模块的两个方法给(一个字面量片段),**不是外部输入** ——
        这一点写在签名旁边,免得后来者以为这里可以拼用户给的字符串。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT document_id, subject_kind, subject_id FROM {self._subjects}"
                    f" {where}",
                    params,
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [(str(row[0]), _kind(row[1]), str(row[2])) for row in rows]

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


def _acl_of(document_id: str, subjects: Sequence[tuple[str, str]]) -> DocumentAcl:
    """输入:文档 ID + 它的授权对象(``(类型, 对象 ID)``)。行为:按类型分成两份名单(都排好序)。输出:``DocumentAcl``。

    排序是给界面看的:同一次授权读两次应该长得一样(否则评审时两边对不上,
    会让人怀疑"是不是又被改了")。
    """
    users = tuple(sorted(subject for kind, subject in subjects if kind == "user"))
    groups = tuple(sorted(subject for kind, subject in subjects if kind == "group"))
    return DocumentAcl(document_id=document_id, user_ids=users, group_ids=groups)

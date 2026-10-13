"""
MySQL 认证适配器:用户 / 令牌 / 会话归属三张表。

**为什么是三张独立的表,而不是给 ``conversations`` 加一列**:加列要迁移已经装好的库
(MySQL 8 的 ``ADD COLUMN`` 没有 ``IF NOT EXISTS``,得先查 information_schema 再 ALTER),
而**会话归属与对话内容本来就是两件事** —— 前者是授权数据(读得少、写得少、错了就是越权),
后者是业务数据。分开之后 ``conversations`` 那张表与它的 32 条契约一行都不用动。

**为什么令牌表在 MySQL 而不是 Redis**:Redis 一 flush 所有人被登出;而"谁能登录"
是安全状态,不该活在缓存里。代价是每个请求多一次查询(一次主键查,~1ms 量级),
真嫌慢再给 ``auth_tokens`` 加一层 Redis 缓存(记在 deferred)。

**时间一律用数据库的 ``NOW(3)`` 判过期,不用 Python 的 now()**:应用与数据库的时钟
可能不一致,而"过期"这件事只该有一个裁判。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

import pymysql

from project_rag.config import MySQLConfig
from project_rag.domain.auth import USER_ROLES, User, UserRole
from project_rag.ports import AuthStore

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS {users} (
  user_id VARCHAR(32) PRIMARY KEY,
  username VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(16) NOT NULL,
  created_at DATETIME(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS {tokens} (
  token_hash CHAR(64) PRIMARY KEY,
  user_id VARCHAR(32) NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  KEY idx_tokens_user (user_id),
  KEY idx_tokens_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
    """
CREATE TABLE IF NOT EXISTS {owners} (
  session_id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(32) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  KEY idx_owners_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""",
)


def _role(value: str) -> UserRole:
    """输入:库里的角色文本。行为:校验落在闭集里。输出:Role。

    脏数据(手改过库 / 老版本写的)必须当场炸:默默当成 employee 会让一个管理员
    "看起来登录成功、实际没有权限",那比报错难查得多。
    """
    if value not in USER_ROLES:
        raise RuntimeError(f"库里的角色 {value!r} 不在 {USER_ROLES} 里 —— 数据被改过?")
    return value  # type: ignore[return-value]  # 上面已收窄到 USER_ROLES 的字面量集合


class MySQLAuthStore(AuthStore):
    """用户 / 令牌 / 会话归属的读写。构造不连库。"""

    def __init__(
        self,
        config: MySQLConfig,
        *,
        users_table: str = "users",
        tokens_table: str = "auth_tokens",
        owners_table: str = "session_owner",
    ) -> None:
        """输入:MySQL 配置 + 三个表名。行为:校验表名(白名单)并记下。输出:无。"""
        for table in (users_table, tokens_table, owners_table):
            if not _SAFE_TABLE.match(table):
                raise ValueError(f"表名不合法:{table!r}")
        self._config = config
        self._users = users_table
        self._tokens = tokens_table
        self._owners = owners_table

    def init_schema(self) -> None:
        """输入:无。行为:建三张表(幂等)。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                for ddl in _DDL:
                    cur.execute(
                        ddl.format(users=self._users, tokens=self._tokens, owners=self._owners)
                    )
        finally:
            conn.close()

    def create_user(self, username: str, password_hash: str, role: UserRole) -> User:
        """输入:用户名、密码哈希、角色。行为:插入(重名抛错)。输出:新用户。"""
        from project_rag.application.security import new_user_id

        user = User(user_id=new_user_id(), username=username, role=role)
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._users} (user_id, username, password_hash, role, created_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (user.user_id, username, password_hash, role, datetime.now()),
                )
        except pymysql.err.IntegrityError as exc:
            raise ValueError(f"用户名 {username!r} 已存在") from exc
        finally:
            conn.close()
        return user

    def find_by_username(self, username: str) -> tuple[User, str] | None:
        """输入:用户名。行为:取用户与密码哈希。输出:``(User, hash)`` 或 None。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id, username, role, password_hash FROM {self._users}"
                    " WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        user_id, name, role, password_hash = row
        return User(user_id=user_id, username=name, role=_role(role)), password_hash

    def set_password(self, user_id: str, password_hash: str) -> None:
        """输入:用户 ID、新哈希。行为:UPDATE 一行。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._users} SET password_hash = %s WHERE user_id = %s",
                    (password_hash, user_id),
                )
        finally:
            conn.close()

    def list_users(self) -> Sequence[User]:
        """输入:无。行为:按用户名升序取全部用户(不取哈希)。输出:用户序列。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id, username, role FROM {self._users} ORDER BY username")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [User(user_id=r[0], username=r[1], role=_role(r[2])) for r in rows]

    def delete_user(self, user_id: str) -> int:
        """输入:用户 ID。行为:删用户 + 他的令牌(一个事务)。输出:删掉的用户行数。

        **为什么两件事在一个事务里**:只删用户不删令牌,那些令牌行就永远留在表里
        (换不出用户,但也查不出主人);反过来只删令牌不删用户则等于没删。
        两条要么都成,要么都不成。

        会话归属(``session_owner``)**故意不动**:见 ``ports/auth.py`` 的说明 ——
        留着那一行,删号之后的会话仍然"有主人"(只是那个人不存在了),因此谁都读不到;
        而那正是我们要的结果,同时留下了"这些会话曾经属于谁"的证据。
        """
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._tokens} WHERE user_id = %s", (user_id,))
                cur.execute(f"DELETE FROM {self._users} WHERE user_id = %s", (user_id,))
                removed = int(cur.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return removed

    def issue_token(self, user: User, token_hash: str, ttl_seconds: int) -> None:
        """输入:用户、令牌哈希、有效期。行为:插入一行(过期时刻由**数据库**算)。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._tokens} (token_hash, user_id, expires_at, created_at)"
                    " VALUES (%s, %s, DATE_ADD(NOW(3), INTERVAL %s SECOND), NOW(3))",
                    (token_hash, user.user_id, ttl_seconds),
                )
        finally:
            conn.close()

    def user_for_token(self, token_hash: str) -> User | None:
        """输入:令牌哈希。行为:未过期才返回用户(过期由数据库判)。输出:用户或 None。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT u.user_id, u.username, u.role FROM {self._tokens} t"
                    f" JOIN {self._users} u ON u.user_id = t.user_id"
                    " WHERE t.token_hash = %s AND t.expires_at > NOW(3)",
                    (token_hash,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        user_id, name, role = row
        return User(user_id=user_id, username=name, role=_role(role))

    def revoke_token(self, token_hash: str) -> None:
        """输入:令牌哈希。行为:删掉它。输出:无。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._tokens} WHERE token_hash = %s", (token_hash,))
        finally:
            conn.close()

    def bind_session(self, session_id: str, user_id: str) -> None:
        """输入:会话 ID、用户 ID。行为:绑定归属;已绑过**不改**(``INSERT IGNORE``)。输出:无。

        为什么已绑过不改:改了等于"谁最后问谁拥有这段会话" —— 那正是我们要堵的洞。
        真要转移归属,应该是一个显式的管理操作,不是插入的副作用。
        """
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT IGNORE INTO {self._owners} (session_id, user_id, created_at)"
                    " VALUES (%s, %s, %s)",
                    (session_id, user_id, datetime.now()),
                )
        finally:
            conn.close()

    def owner_of(self, session_id: str) -> str | None:
        """输入:会话 ID。行为:查归属。输出:用户 ID 或 None(没绑过)。"""
        conn = self._connect(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id FROM {self._owners} WHERE session_id = %s", (session_id,)
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return None if row is None else str(row[0])

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

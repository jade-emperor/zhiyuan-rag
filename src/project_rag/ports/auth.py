"""
端口:认证与授权需要的持久化(用户、令牌、会话归属)。

**为什么"会话归属"在这个端口里**:它和用户是同一件事的两面 —— "这段会话是谁的"就是授权。
放在 ``ConversationStore`` 里的代价是改它的表结构(现有安装要迁移),
而**单独一张归属表**既不动已有的 ``conversations``,也让"谁能读哪段会话"这件事
只有一个查询入口(见 ``owner_of``)。

**密码哈希与令牌生成不在这里**:那是 ``application/security.py`` 的策略(标准库实现),
存储只负责"把已经算好的东西存下来、按条件取回来"。这样换算法不动表结构,
换表结构不动算法,而且两条都能各自离线测。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from project_rag.domain.auth import User, UserRole


class AuthStore(Protocol):
    """用户 / 令牌 / 会话归属的读写。"""

    def init_schema(self) -> None:
        """输入:无。行为:建三张表(用户 / 令牌 / 会话归属),**幂等**。输出:无。"""
        ...

    def create_user(self, username: str, password_hash: str, role: UserRole) -> User:
        """输入:用户名、**已算好的**密码哈希、角色。行为:插入一行(用户名唯一)。输出:新用户。

        重名必须抛错而不是覆盖:覆盖会让"给同事开个号"顺手改掉别人的密码。
        """
        ...

    def find_by_username(self, username: str) -> tuple[User, str] | None:
        """输入:用户名。行为:取用户与它的密码哈希。输出:``(User, hash)``;没这个人给 None。

        **为什么要连哈希一起返回**:"验证密码"必须由调用方(用例层)做,
        存储层不许出现"密码对不对"的判断 —— 那是策略,而且它要能离线测。
        """
        ...

    def set_password(self, user_id: str, password_hash: str) -> None:
        """输入:用户 ID、**已算好的**新密码哈希。行为:改掉它。输出:无。

        **为什么要有这条**(而不是"删号重建"):会话归属挂在 ``user_id`` 上,
        删号重建会换 id,于是"这个人以前的会话"全部变成无主会话 —— 改个密码顺手毁掉历史,
        那不是使用者预期的事。
        """
        ...

    def list_users(self) -> Sequence[User]:
        """输入:无。行为:列出全部用户(**不含密码哈希**)。输出:用户序列(按用户名升序)。"""
        ...

    def delete_user(self, user_id: str) -> int:
        """输入:用户 ID。行为:删掉这个用户**以及他的令牌**。输出:删掉的行数(0 = 本来就没有)。

        **连带删令牌是必须的**:令牌表按 ``user_id`` 存,``user_for_token`` 是
        ``JOIN users``,所以删了用户之后他的令牌自然换不出用户(实测口径见
        ``check_p0_6_access.py``)。显式删一遍是为了不让"已删用户的 token 行"堆在表里 ——
        它们已经不可能被用到,留着只是让审计时多一批查不出主人的行。

        **会话归属不删**(``session_owner`` 那些行留着):删号之后那些会话
        ``owner_of`` 仍然返回一个不存在的用户 ID,于是**谁都读不到** —— 这是我们要的
        (fail closed)。反过来"删号顺手把归属也删了",那些会话会变成
        "没有主人",而 ``owner_of`` 的 None 在接口层是**拒绝**(见 api/auth.py),
        结果一样但少了一层证据:出问题时看不出这些会话曾经属于谁。
        """
        ...

    def issue_token(self, user: User, token_hash: str, ttl_seconds: int) -> None:
        """输入:用户、**已算好的**令牌哈希、有效期秒数。行为:记一条令牌。输出:无。

        存哈希而不是令牌本身:库被读走时,拿到的**不是**能直接用的凭证。
        """
        ...

    def user_for_token(self, token_hash: str) -> User | None:
        """输入:令牌哈希。行为:未过期则返回它的用户。输出:用户;没有/过期给 None。"""
        ...

    def revoke_token(self, token_hash: str) -> None:
        """输入:令牌哈希。行为:删掉它(登出)。输出:无。"""
        ...

    def bind_session(self, session_id: str, user_id: str) -> None:
        """输入:会话 ID、用户 ID。行为:记下归属(**幂等**:重复绑定同一对不报错)。输出:无。"""
        ...

    def owner_of(self, session_id: str) -> str | None:
        """输入:会话 ID。行为:查它的归属。输出:用户 ID;没绑过给 None(调用方决定怎么处理)。

        ``None`` 的语义很关键:它是"这段会话没有主人"(历史遗留 / 从没绑过),
        **不等于"谁都能读"**。API 层的口径是:没主人就拒绝(见 ``_require_owner``)。
        """
        ...

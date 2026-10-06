"""
认证与授权的领域类型(**纯数据,不碰密码学、不碰存储**)。

**为什么把密码学的活留在这里不做**:这个模块要被"任何实现都能满足"的契约钉住,
它只需要回答「一个用户是谁、他是什么角色」。哈希怎么算、token 怎么签发,都是**策略**,
放在 ``application/security.py``(标准库)与适配器里 —— 换算法不该动领域类型。

**为什么叫 ``UserRole`` 而不是 ``Role``**:``domain/models.py`` 里已经有一个 ``Role``,
那是**聊天消息**的角色(system/user/assistant)。两个概念共用一个名字,迟早有人把
``Role`` 从 auth 导进消息那边(类型检查器也拦不住,因为字面量集合不重叠时它只看得到 str)。

**角色只有两种**(2026-10-06 定):``employee`` 只能检索与问答,``admin`` 还能上传/删除文档、
看观测报表。不做更细的权限矩阵:两个人的系统里,矩阵只会变成没人维护的注释。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 角色是**闭集**。用 Literal 而不是 str:写错一个字母,类型检查器当场报,而不是运行时 403。
UserRole = Literal["employee", "admin"]

USER_ROLES: tuple[UserRole, ...] = ("employee", "admin")


@dataclass(frozen=True, slots=True)
class User:
    """一个登录主体。**不含密码/哈希** —— 那些是存储与策略的事,不该跟着用户到处走。"""

    user_id: str
    username: str
    role: UserRole

    @property
    def is_admin(self) -> bool:
        """输入:无。行为:判是不是管理员。输出:布尔。

        **权限判断只写这一处**:散在各地写 ``role == "admin"``,改角色名时必然漏一处,
        而漏掉的那处是静默的(不报错,只是权限不对)。
        """
        return self.role == "admin"

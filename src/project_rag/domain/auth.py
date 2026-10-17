"""
认证与授权的领域类型(**纯数据,不碰密码学、不碰存储**)。

**为什么把密码学的活留在这里不做**:这个模块要被"任何实现都能满足"的契约钉住,
它只需要回答「一个用户是谁、他是什么角色」。哈希怎么算、token 怎么签发,都是**策略**,
放在 ``application/security.py``(标准库)与适配器里 —— 换算法不该动领域类型。

**为什么叫 ``UserRole`` 而不是 ``Role``**:``domain/models.py`` 里已经有一个 ``Role``,
那是**聊天消息**的角色(system/user/assistant)。两个概念共用一个名字,迟早有人把
``Role`` 从 auth 导进消息那边(类型检查器也拦不住,因为字面量集合不重叠时它只看得到 str)。

**三个角色**(定),每个都对应一句"他能干什么":

- ``employee`` 员工:检索与问答,外加看知识库清单(他要知道自己能检索什么);
- ``ops``      运维:能用知识库(检索与问答)+ 看报表(延迟 + 消耗)。**不管账号、不管文档**
  —— 他既要回答"这套系统跑得好不好、花了多少钱",也要能自己问一句验一验;
- ``admin``    管理员:全都看(对话/知识库/报表),外加全部管理动作
  (上传、删除、建号、删号、分组、文档可见范围)。

**"能干什么"只写在这里,不许散在调用点**:`role == "admin"` 这样的比较写在路由里,
改角色语义时必然漏一处,而漏掉的那处是静默的(不报错,只是权限不对)。
三个集合(``REPORT_ROLES`` / ``KNOWLEDGE_ROLES`` / ``MANAGE_ROLES``)是**运行时**的真源,
三个 property 是它们的唯一用法;接口层只调 property,不认字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 角色是**闭集**。用 Literal 而不是 str:写错一个字母,类型检查器当场报,而不是运行时 403。
UserRole = Literal["employee", "ops", "admin"]

USER_ROLES: tuple[UserRole, ...] = ("employee", "ops", "admin")

# ── 三张能力表(角色 → 能不能做某一类事)────────────────────────────────────
# 顺序与 USER_ROLES 一致,便于人一眼对照。**新增能力时在这里加一张表 + 一个 property**,
# 不要在调用点写 `role in (...)` —— 那正是"权限判断散开"的老路。
# 看报表:运维与管理员的共同点。延迟、消耗、流量这类运营数据两边都该看得到。
REPORT_ROLES: tuple[UserRole, ...] = ("ops", "admin")
# 用知识库:提问、看清单、看预览。**运维不在里面** —— 他的只读范围是报表,不是语料。
KNOWLEDGE_ROLES: tuple[UserRole, ...] = ("employee", "ops", "admin")
# 管理动作:上传/删除文档、建号删号、分组、改文档可见范围。只有管理员。
MANAGE_ROLES: tuple[UserRole, ...] = ("admin",)


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

    @property
    def is_ops(self) -> bool:
        """输入:无。行为:判是不是运维。输出:布尔。"""
        return self.role == "ops"

    @property
    def can_view_reports(self) -> bool:
        """输入:无。行为:判他能不能看报表(延迟 / 消耗)。输出:布尔。"""
        return self.role in REPORT_ROLES

    @property
    def can_use_knowledge(self) -> bool:
        """输入:无。行为:判他能不能用知识库(提问 / 清单 / 预览)。输出:布尔。"""
        return self.role in KNOWLEDGE_ROLES

    @property
    def can_manage(self) -> bool:
        """输入:无。行为:判他能不能做管理动作(文档、账号、分组、可见范围)。输出:布尔。"""
        return self.role in MANAGE_ROLES

    @property
    def sees_all_documents(self) -> bool:
        """输入:无。行为:判他是不是**不受文档可见范围限制**。输出:布尔。

        「可见范围」是"规则只用来收窄"(见 ``application/access.py``):没配规则的文档人人可见,
        配了规则的只有名单里的人可见。管理员**永远**在名单之外也算可见 —— 否则
        「谁能改这份文档的授权」会取决于那份授权,改错一次就再也改不回来(锁死自己)。
        """
        return self.is_admin

"""
用例:可见范围怎么落到**一次请求**上(纯函数,不碰存储)。

**为什么这是一个单独的模块**:判定"这个人能不能看这份文档"要在四个地方用到 ——
检索(把它变成禁止名单)、知识库清单(过滤行)、预览(拦住单个 ID)、以及管理员
自己看授权配置。四处各写一遍 ``if`` 的结果,是**四处口径迟早不一样**,而权限的口径
不一致时,通常是其中一处悄悄放宽了(没人会发现)。所以判定只有这里的两个函数:

* ``denied_documents`` —— 把"全部规则 + 我是谁"收成**禁止名单**(交给检索与清单);
* ``AccessContext``     —— 一次请求的结论(是不是不受限、禁止哪些、我属于哪些组)。

**为什么用禁止名单**:口径是"没配规则的文档人人可见"(见 ``domain/access.py``),
所以能看 = 全部 − 被限制且没授权给我的那几份。禁止名单通常只有几条,
而允许名单要把几百份文档的 ID 全传下去。

**为什么不在这里读库**:这一层只认识 ``Mapping[str, DocumentAcl]`` 这种数据形状,
于是它能在一台没有 MySQL 的机器上被逐条测(``check_p0_6_access.py`` 就是这么测的),
而且"库读不到时怎么办"这个决定留在接口层(那里的答案是:如实报错,不静默放行)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from project_rag.domain.access import DocumentAcl


@dataclass(frozen=True, slots=True)
class AccessContext:
    """一次请求里「这个人能看什么」的结论。**只有这一个类型在请求里传**。

    为什么不各自传三个参数(user_id / group_ids / denied):调用点会开始各取所需,
    而"某处只传了 user_id 忘了 groups"这种错不会报错,只会让属于组的人少看到几份文档。
    """

    user_id: str
    group_ids: tuple[str, ...] = ()
    denied: frozenset[str] = frozenset()
    sees_all: bool = False

    @property
    def restricted(self) -> bool:
        """输入:无。行为:判这个人是不是"有文档被挡在外面"。输出:布尔。

        ``False`` 是**最常见的一档**(员工没被任何规则排除、或管理员):调用方拿它
        跳过整段过滤。注意它说的是"没有东西被挡",不是"他只能看这些"。
        """
        return bool(self.denied) or self.sees_all

    def allows(self, document_id: str) -> bool:
        """输入:文档 ID。行为:判这份文档他能不能看。输出:布尔。"""
        return document_id not in self.denied


def denied_documents(
    rules: Mapping[str, DocumentAcl],
    *,
    user_id: str,
    group_ids: Sequence[str] = (),
    sees_all: bool = False,
) -> frozenset[str]:
    """输入:全部规则(``document_id -> DocumentAcl``)、用户 ID、他所属的组、是否不受限。

    行为:把**有规则且没授权给他**的那些文档收成禁止名单。输出:禁止的 document_id 集合。

    ``rules`` 里没有的文档 = 没有规则 = 谁都能看(所以它们不会出现在结果里)。
    管理员(``sees_all``)一律得空集 —— 见 ``domain/access.py`` 里"管理员永远可见"的理由。

    **空集是"什么都不禁",不是"什么都禁"**:这一点在返回类型上看不出来(都是 frozenset),
    所以写在这里,并由 ``check_p0_6_access.py`` 钉住(有一条专门喂空规则,断言结果为空)。
    """
    if sees_all:
        return frozenset()
    return frozenset(
        document_id
        for document_id, acl in rules.items()
        if not acl.allows(user_id=user_id, group_ids=group_ids)
    )


def context_of(
    rules: Mapping[str, DocumentAcl],
    *,
    user_id: str,
    group_ids: Sequence[str] = (),
    sees_all: bool = False,
) -> AccessContext:
    """输入:同 ``denied_documents``。行为:收成一个 ``AccessContext``。输出:它。"""
    return AccessContext(
        user_id=user_id,
        group_ids=tuple(group_ids),
        denied=denied_documents(
            rules, user_id=user_id, group_ids=group_ids, sees_all=sees_all
        ),
        sees_all=sees_all,
    )


# 不受限的空上下文:没接可见范围(注入式验收)与"这道题与文档无关"的路径用它。
# **它是"全程放行"** —— 所以只该由"确实没配"的地方构造,不该拿它当默认值兜底
# (读库失败时构造它 = 一次数据库抖动变成全网放行)。
UNRESTRICTED = AccessContext(user_id="", sees_all=True)

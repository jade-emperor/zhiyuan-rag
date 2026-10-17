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
于是它能在一台没有 MySQL 的机器上被逐条测,
而且"库读不到时怎么办"这个决定留在接口层(那里的答案是:如实报错,不静默放行)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

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
    所以写在这里(有一条检查专门喂空规则,断言结果为空)。
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


# 一份文档在**某一个组**眼里是什么样子(组侧授权页的三态):
#   open    = 没有规则 = 人人可见(本组本来就能看,这里禁不掉 —— 要限制得去收窄这份文档)
#   allowed = 有规则,而且本组在名单里
#   denied  = 有规则,但本组不在名单里
DocumentVisibility = Literal["open", "allowed", "denied"]


@dataclass(frozen=True, slots=True)
class GroupDocumentRow:
    """组侧授权页的一行:这份文档,这个组能不能看、以及"还授权给了谁"。"""

    document_id: str
    filename: str
    visibility: DocumentVisibility
    # 除本组之外,这份文档还授权给了几个对象(人 + 组)。``open`` 时无意义(恒为 0)。
    # 管理员要看得见这个数:取消本组之后**名单空了** = 这份文档会回到人人可见 ——
    # 那是一次比"少给一个组"大得多的变化,得让他看得见再决定。
    other_subjects: int = 0


@dataclass(frozen=True, slots=True)
class GroupGrantPlan:
    """把"这个组能看哪些文档"翻译成对规则表的增删(纯数据,不碰存储)。

    四份名单都是**要如实回报给管理员**的:加了什么、撤了什么、哪些本来就不用动、
    以及**哪几份会因为名单空了而回到人人可见**(最容易吓人一跳的那一类)。
    """

    grant: tuple[str, ...] = ()
    revoke: tuple[str, ...] = ()
    already_open: tuple[str, ...] = ()
    will_open: tuple[str, ...] = ()


def group_document_rows(
    documents: Sequence[tuple[str, str]],
    rules: Mapping[str, DocumentAcl],
    *,
    group_id: str,
) -> tuple[GroupDocumentRow, ...]:
    """输入:``(document_id, filename)`` 序列、全部规则、组 ID。行为:算每份文档对这个组的三态。

    输出:按文件名排序的行序列。

    **为什么按"文件"而不是按"规则"列**:管理员想回答的是"这个组能看哪些文档",
    而规则表里只有"被收窄过的那几份" —— 从规则出发会让他看不见"人人可见"的那一大片,
    而那正是"这个组实际上能看到的东西"的主体。
    """
    rows = []
    for document_id, filename in documents:
        acl = rules.get(document_id)
        if acl is None:
            rows.append(GroupDocumentRow(document_id, filename, "open"))
            continue
        in_list = group_id in acl.group_ids
        others = len(acl.user_ids) + len(acl.group_ids) - (1 if in_list else 0)
        rows.append(
            GroupDocumentRow(
                document_id,
                filename,
                "allowed" if in_list else "denied",
                other_subjects=others,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.filename, row.document_id)))


def plan_group_documents(
    documents: Sequence[tuple[str, str]],
    rules: Mapping[str, DocumentAcl],
    *,
    group_id: str,
    allowed: Sequence[str],
) -> tuple[GroupGrantPlan, tuple[str, ...]]:
    """输入:文档清单、全部规则、组 ID、**这个组该能看的文档 ID 列表**。

    行为:算出一份增删计划。输出:``(计划, 未知的文档 ID 元组)``;未知 ID 非空时调用方该报 400。

    三条语义(**不改**"规则只用来收窄"那条口径):

    * 请求里有、当前 ``denied`` 的 → **加进名单**(这就是"授权");
    * 当前 ``allowed``、请求里没有的 → **移出名单**(若名单因此空了,这份文档回到人人可见 ——
      记进 ``will_open``,由调用方如实告诉管理员);
    * 当前 ``open`` 的**一律不动**:它本来就人人可见,本组能不能看这件事在这里没有意义;
      要把它收窄成"只有本组能看",那是一次**动别人可见性**的操作,由文档侧的授权
      (或界面上那个显式的「收窄」按钮)来做 —— 藏在一个勾选框里太危险。

    未知 ID 不静默忽略:名单里出现不存在的文档时,没人能看出那份名单是错的
    (与 ``_set_document_subjects`` 校验账号/组存在同一条规矩)。
    """
    known = {document_id for document_id, _ in documents}
    unknown = tuple(sorted({doc for doc in allowed if doc not in known}))
    if unknown:
        return GroupGrantPlan(), unknown

    wanted = set(allowed)
    grant: list[str] = []
    revoke: list[str] = []
    already_open: list[str] = []
    will_open: list[str] = []
    for row in group_document_rows(documents, rules, group_id=group_id):
        if row.visibility == "open":
            if row.document_id in wanted:
                already_open.append(row.document_id)
            continue
        if row.visibility == "denied" and row.document_id in wanted:
            grant.append(row.document_id)
        elif row.visibility == "allowed" and row.document_id not in wanted:
            revoke.append(row.document_id)
            if row.other_subjects == 0:
                will_open.append(row.document_id)
    return (
        GroupGrantPlan(
            grant=tuple(grant),
            revoke=tuple(revoke),
            already_open=tuple(already_open),
            will_open=tuple(will_open),
        ),
        (),
    )

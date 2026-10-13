"""
可见范围的领域类型:**一个组**、以及**一份文档被授权给谁**。

**为什么可见范围要单独一个模块**:它是"谁能看到什么"的**规则本身**,与"文档登记信息"
(``domain/documents.py``)、与"用户是谁"(``domain/auth.py``)是三件不同的事:
规则可以没有(没配 = 人人可见),可以只授权给某个人,也可以授权给某一类人(组)。
混进 ``documents.py`` 会让"文档列表"这个纯展示用的类型长出权限语义 —— 那就晚了。

**一条口径,写在这里而不是调用点**(``allows``):
**规则只用来收窄**。没配规则的文档**所有登录账号都能看**(与加这套东西之前的行为逐字一致);
配了规则的,只有在名单里的人(直接授权,或经由他所属的组)能看。
"没配 = 人人可见"是刻意的选择:反过来(没配 = 只有管理员可见)会让开关一上线,
现有全部文档对员工立刻隐身,而修法是逐份授权 —— 一个功能把整个知识库关掉,
没人会觉得那是安全,只会觉得那是坏了。

管理员**永远可见**(``sees_all``):他是唯一能改授权的人,要是他也被规则挡住,
一次改错就再也改不回来 —— 那是最不该出现的一种锁死。

组**不是角色**:角色决定"能干什么"(能不能提问、能不能管账号),
组决定"能看哪些文档"。同一个人可以属于多个组,交叉得到的是**并集**(任一命中即可见)——
写成交集的话,"给研发部再加一份财务文档"就变成了要给每个人单独加,那正是组要省掉的事。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# 授权对象的两类:**某个人**、**某一类人**(组)。接口、存储、前端的话术全跟着它走。
SubjectKind = Literal["user", "group"]

SUBJECT_KINDS: tuple[SubjectKind, ...] = ("user", "group")


@dataclass(frozen=True, slots=True)
class Group:
    """一类员工(部门 / 岗位 / 项目组)。``group_id`` 稳定不变,``name`` 是给人看的。"""

    group_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DocumentAcl:
    """一份文档的可见范围:**授权的那些人**与**授权的那些组**。

    两个字段都空 = **没有规则** = 人人可见(``restricted`` 为 False)。
    所以"取消限制"就是把两个序列都清空 —— 不需要第三种表示(比如一个 ``restricted`` 布尔),
    多一个字段就有"布尔说限制了、名单却是空的"这种自相矛盾的状态。
    """

    document_id: str
    user_ids: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        """输入:无。行为:判这份文档是不是"收窄过"。输出:布尔(有任何一个授权对象即为真)。"""
        return bool(self.user_ids or self.group_ids)

    def allows(
        self, *, user_id: str, group_ids: Sequence[str], sees_all: bool = False
    ) -> bool:
        """输入:用户 ID、他所属的组、他是否不受限制(管理员)。

        行为:按"没规则=人人可见 / 有规则=名单内可见 / 管理员永远可见"判。
        输出:能不能看。

        **组命中取并集**:任一组在名单里就可见(理由见模块 docstring)。
        """
        if sees_all or not self.restricted:
            return True
        if user_id in self.user_ids:
            return True
        return bool(set(self.group_ids) & set(group_ids))

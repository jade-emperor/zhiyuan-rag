"""
端口:可见范围的持久化(**组 / 组员 / 每份文档授权给谁**)。

**为什么单独一个端口,而不是塞进 ``AuthStore``**:两者虽然都关于"谁",但读写频率与
失效方式完全不同 —— 用户表在登录路径上被读(每次请求一次),而可见范围只在
"列清单 / 检索 / 改授权"时读。合成一个端口的结果是:登录这条最热的路径被迫认识
三张与它无关的表,而验收要造一个假用户表时也得顺手造一堆组。

**三张表的分工**(实现见 ``adapters/access_mysql.py``):

* ``acl_groups``          一个组一行(名字唯一);
* ``acl_group_members``   组 ↔ 用户(多对多,没有"主组"这一说 —— 有主组就有优先级,
  而"可不可以看"这件事不该有优先级:它只该是并集);
* ``acl_documents``       一份文档 ↔ 一个授权对象(= 某一行的 ``subject_kind`` + ``subject_id``)。

**为什么"没有规则"用"查不到行"表达,再立一个布尔列**:``restricted`` 这种东西一旦落库,
就会出现"布尔说限制了、授权表里一行都没有"的状态,而那时谁也说不清该信哪个
(见 ``domain/access.py`` 的 ``DocumentAcl``)。空 = 没规则,只有一种表示。

一句话限定:**这个端口只管"存了什么",不管"谁能看"** —— 判定在
``domain/access.py`` 的 ``allows`` / ``application/access.py`` 的 ``denied_documents``,
那是能在一台没有 MySQL 的机器上单测的纯函数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from project_rag.domain.access import DocumentAcl, Group


class AccessStore(Protocol):
    """组、组员、文档授权名单的读写。"""

    def init_schema(self) -> None:
        """输入:无。行为:建三张表(**幂等**)。输出:无。"""
        ...

    # ── 组 ────────────────────────────────────────────────────────────────
    def create_group(self, name: str) -> Group:
        """输入:组名。行为:插入一行(重名抛错,不覆盖)。输出:新建的组。

        重名抛错而不是覆盖:覆盖会让"给销售部加个人"顺手把销售部改名 —— 而名字是
        管理员唯一认得的标识,悄悄变了没人能发现(授权还在,只是没人知道那是哪个组)。
        """
        ...

    def list_groups(self) -> Sequence[Group]:
        """输入:无。行为:列出全部组(按名字升序)。输出:组序列。"""
        ...

    def delete_group(self, group_id: str) -> int:
        """输入:组 ID。行为:删掉这个组**以及它的组员与授权行**。输出:删掉的组行数(0 = 本来没有)。

        连带清干净是刻意的:留着孤儿组员行,下次建一个**同名新组**时会凭空继承旧成员 ——
        那是最难查的一种"权限串了"。
        """
        ...

    # ── 组员 ──────────────────────────────────────────────────────────────
    def set_members(self, group_id: str, user_ids: Sequence[str]) -> None:
        """输入:组 ID、成员用户 ID(**整份名单**)。行为:把成员设成这一份(多退少补)。输出:无。

        **声明式而不是增删两个动作**:前端弹窗里勾选完点保存,语义就是"这些人"。
        拆成 add/remove 的话,一次保存要发一堆请求,中途失败就留下半份名单 ——
        而权限的半份名单是"看起来设过了"。
        """
        ...

    def members_of(self, group_id: str) -> Sequence[str]:
        """输入:组 ID。行为:它的成员用户 ID(升序)。输出:ID 序列。"""
        ...

    def groups_by_user(self) -> Mapping[str, tuple[str, ...]]:
        """输入:无。行为:**一次**给出每个用户属于哪些组。输出:``user_id -> 组 ID 元组``。

        为什么要批量:列清单 / 每次检索都要问"我属于哪些组",逐组去查是 N 次查询;
        而这张表小得很(人 × 组),一次全读回来最省。**用户不在表里 = 不属于任何组**
        (空元组的意思,不是"数据缺了")。
        """
        ...

    # ── 文档授权 ──────────────────────────────────────────────────────────
    def set_document_subjects(
        self, document_id: str, *, user_ids: Sequence[str], group_ids: Sequence[str]
    ) -> None:
        """输入:文档 ID、两份**整份**名单。行为:覆盖这份文档的授权对象。输出:无。

        两份名单都空 = **删掉这份文档的规则**(回到"人人可见"),不是"谁都看不到" ——
        见模块 docstring 与 ``domain/access.py``。
        """
        ...

    def subjects_of(self, document_id: str) -> DocumentAcl:
        """输入:文档 ID。行为:取它的授权对象。输出:``DocumentAcl``;没有规则给空的那一份。"""
        ...

    def rules(self) -> Mapping[str, DocumentAcl]:
        """输入:无。行为:一次取出**全部**有规则的文档。输出:``document_id -> DocumentAcl``。

        没规则的文档**不会出现在返回值里**(空表 = 一份都没限制)。这个"缺席"是有含义的:
        调用方要靠它算"哪些文档对我来说被收窄了"(见 ``application/access.py``)。
        """
        ...

    def forget_document(self, document_id: str) -> int:
        """输入:文档 ID。行为:删掉它的授权行(文档本身被删时调用)。输出:删掉的行数。

        为什么必须显式调:授权表按 ``document_id`` 存,而那一列**没有外键**
        (向量库与登记表都不是 MySQL 的表,建不了外键)。不调的话,同名文档将来重新上传
        会**继承上一份的授权** —— 旧文档曾经只给三个人看,新传的同名文件也就只给那三个人看,
        而管理员以为自己传了一份公开文档。
        """
        ...

    def forget_user(self, user_id: str) -> int:
        """输入:用户 ID。行为:把他从所有组和所有文档的授权名单里摘掉(删号时调用)。输出:删掉的行数。

        与 ``forget_document`` 同一个道理(没有外键),但后果更隐蔽:留着已删用户的 ID,
        界面上会出现一个**点不开也删不掉的"授权对象"**(在用户列表里查不到他),
        而管理员没法判断那份文档到底还挡着谁。
        """
        ...

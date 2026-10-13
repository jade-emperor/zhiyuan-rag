#!/usr/bin/env python
"""P0-6 契约:第三个角色(运维)、账号管理、分组(一类员工)、文档可见范围。

用法::

    uv run python scripts/acceptance/check_p0_6_access.py

**为什么要有这一份**:这一批改动里"错了最贵"的不是功能缺失,而是**权限放宽** ——
一个看不见的 `or` 就能让受限文档重新出现在所有人的检索结果里,而且不报错。
所以口径、守卫、四个落地位置(检索 / 清单 / 预览 / WS)各钉几条。

本脚本固化的口径(改它们 = 改契约):

* **角色是闭集** `("employee", "ops", "admin")`;能力判断只在 `domain/auth.py` 的三张表里,
  调用点不比较角色字符串;
* **运维(ops)**:能看报表(延迟 + 消耗),**不能**用知识库、不能管账号;
* **可见范围只用来收窄** —— 没配规则的文档人人可见;配了规则的,名单里的人 +
  名单里的组(并集)+ 管理员可见;**空规则 = 不禁**(不是"全禁");
* **删号两条守卫**:不能删自己、不能删最后一个管理员;
* **看不见的文档回 404**(不是 403):"存在但不给你看"本身也是信息;
* **读不到规则时 503 不放行**:数据库抖动不许变成"全网可见"。

全部用内存假件(不需要容器 / 模型 / 网络);真 MySQL 的往返由
``check_m5_history.py`` 那一路负责。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from _harness import check, report


# ── 假件:认证存储(含删号)与可见范围存储 ────────────────────────────────────
class _FakeAuthStore:
    """``AuthStore`` 端口的内存实现 —— 比别处那份多一个 ``delete_user``。

    删号必须**连带删令牌**:真实适配器是这么做的,而这正是"删完立刻失效"那条检查
    要验的东西(见 ``ports/auth.py`` 的 ``delete_user``)。
    """

    def __init__(self, *, fail_user_for_token: bool = False) -> None:
        from project_rag.domain.auth import User

        self.users: dict[str, User] = {}
        self.passwords: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.owners: dict[str, str] = {}
        # 让"读认证状态时炸"这件事可控:用例层要验的是"失败方向是不是关闭的"
        self.fail_user_for_token = fail_user_for_token

    def init_schema(self) -> None:
        return None

    def create_user(self, username, password_hash, role):
        from project_rag.domain.auth import User

        user_id = f"u-{len(self.users) + 1}"
        user = User(user_id=user_id, username=username, role=role)
        self.users[user_id] = user
        self.passwords[user_id] = password_hash
        return user

    def find_by_username(self, username):
        for user_id, user in self.users.items():
            if user.username == username:
                return user, self.passwords[user_id]
        return None

    def set_password(self, user_id, password_hash):
        self.passwords[user_id] = password_hash

    def list_users(self):
        return sorted(self.users.values(), key=lambda user: user.username)

    def delete_user(self, user_id):
        if self.users.pop(user_id, None) is None:
            return 0
        self.passwords.pop(user_id, None)
        for token_hash in [key for key, owner in self.tokens.items() if owner == user_id]:
            self.tokens.pop(token_hash)
        return 1

    def issue_token(self, user, token_hash, ttl_seconds):
        self.tokens[token_hash] = user.user_id

    def user_for_token(self, token_hash):
        if self.fail_user_for_token:
            raise RuntimeError("MySQL 挂了(假件)")
        owner = self.tokens.get(token_hash)
        return None if owner is None else self.users.get(owner)

    def revoke_token(self, token_hash):
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id):
        self.owners.setdefault(session_id, user_id)

    def owner_of(self, session_id):
        return self.owners.get(session_id)


class _FakeLedger:
    """空账本:**报表要读得到东西**才能验"谁能看" —— 没接账本时接口回 503,
    那测出来的是装配缺失,不是权限。
    """

    def __init__(self, rows: Sequence[object] = ()) -> None:
        self.rows = list(rows)

    def init_schema(self) -> None:
        return None

    def record(self, row) -> None:
        self.rows.append(dict(row))

    def load_since(self, since):
        return list(self.rows)


class _FakeAccess:
    """``AccessStore`` 端口的内存实现(组 / 组员 / 文档授权)。

    构造参数 ``fail`` 让每个方法都抛 —— 用例要验"读不到规则时是 503 而不是放行"。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.groups: dict[str, str] = {}  # group_id -> name
        self.members: dict[str, list[str]] = {}  # group_id -> [user_id]
        # document_id -> (用户名单, 组名单)
        self.subjects: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self.fail = fail

    def _guard(self) -> None:
        if self.fail:
            raise RuntimeError("MySQL 挂了(假件)")

    def init_schema(self) -> None:
        return None

    def create_group(self, name):
        from project_rag.domain.access import Group

        if any(existing == name for existing in self.groups.values()):
            raise ValueError(f"组名 {name!r} 已存在")
        group_id = f"g-{len(self.groups) + 1}"
        self.groups[group_id] = name
        self.members[group_id] = []
        return Group(group_id=group_id, name=name)

    def list_groups(self):
        from project_rag.domain.access import Group

        return [Group(group_id=gid, name=name) for gid, name in sorted(self.groups.items())]

    def delete_group(self, group_id):
        self._guard()
        if self.groups.pop(group_id, None) is None:
            return 0
        self.members.pop(group_id, None)
        for document_id in list(self.subjects):
            users, groups = self.subjects[document_id]
            kept = tuple(gid for gid in groups if gid != group_id)
            if kept or users:
                self.subjects[document_id] = (users, kept)
            else:
                self.subjects.pop(document_id)
        return 1

    def set_members(self, group_id, user_ids):
        self._guard()
        self.members[group_id] = list(dict.fromkeys(user_ids))

    def members_of(self, group_id):
        self._guard()
        return tuple(sorted(self.members.get(group_id, [])))

    def groups_by_user(self):
        self._guard()
        grouped: dict[str, list[str]] = {}
        for group_id, user_ids in self.members.items():
            for user_id in user_ids:
                grouped.setdefault(user_id, []).append(group_id)
        return {user_id: tuple(sorted(gids)) for user_id, gids in grouped.items()}

    def set_document_subjects(self, document_id, *, user_ids, group_ids):
        self._guard()
        users, groups = tuple(dict.fromkeys(user_ids)), tuple(dict.fromkeys(group_ids))
        if users or groups:
            self.subjects[document_id] = (users, groups)
        else:
            self.subjects.pop(document_id, None)

    def subjects_of(self, document_id):
        self._guard()
        return _acl(document_id, self.subjects.get(document_id, ((), ())))

    def rules(self):
        self._guard()
        return {
            document_id: _acl(document_id, pair) for document_id, pair in self.subjects.items()
        }

    def forget_document(self, document_id):
        self._guard()
        return 1 if self.subjects.pop(document_id, None) is not None else 0

    def forget_user(self, user_id):
        self._guard()
        removed = 0
        for group_id in list(self.members):
            if user_id in self.members[group_id]:
                self.members[group_id] = [u for u in self.members[group_id] if u != user_id]
                removed += 1
        for document_id in list(self.subjects):
            users, groups = self.subjects[document_id]
            kept = tuple(u for u in users if u != user_id)
            if len(kept) != len(users):
                removed += 1
            if kept or groups:
                self.subjects[document_id] = (kept, groups)
            else:
                self.subjects.pop(document_id)
        return removed


def _acl(document_id: str, pair):
    """输入:文档 ID + ``(用户名单, 组名单)``。行为:转成 ``DocumentAcl``。输出:它。"""
    from project_rag.domain.access import DocumentAcl

    users, groups = pair
    return DocumentAcl(document_id=document_id, user_ids=tuple(users), group_ids=tuple(groups))


# ── 装配:注入式客户端(不连任何东西) ────────────────────────────────────────
def _client(*, auth=True, access=True, fail_access=False):
    """输入:要不要接认证 / 可见范围 / 让可见范围读失败。输出:``(client, auth_store, access_store)``。"""
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    from _fakes import (
        FakeConversationStore,
        FakeFaqStore,
        FakeMatcher,
        FakeVectorStore,
    )

    auth_store = _FakeAuthStore()
    access_store = _FakeAccess(fail=fail_access)
    ledger = _FakeLedger()
    fields: dict[str, Any] = {
        "ledger_store_factory": lambda: ledger,
        # 下面这几个是"最小可用":让清单 / 来源 / 会话 / 问候这几条路能真的跑通。
        # 不接它们的话,那些接口回 503 —— 测出来的是装配缺失,不是权限。
        "faq_store_factory": lambda: FakeFaqStore([]),
        "matcher_factory": lambda: FakeMatcher(),
        "conversation_store_factory": lambda: FakeConversationStore(),
        "vector_store_factory": lambda: FakeVectorStore([]),
        "document_store_factory": _EmptyDocs,
        "disk_files_factory": lambda: (),
    }
    if auth:
        fields["auth_store_factory"] = lambda: auth_store
    if access:
        fields["access_store_factory"] = lambda: access_store
    return TestClient(create_app(Deps(**fields))), auth_store, access_store


class _EmptyDocs:
    """空登记表(清单那条路只要求"读得到",不要求有行)。"""

    def init_schema(self) -> None: ...
    def add(self, record) -> None: ...
    def upsert(self, record) -> None: ...
    def list_all(self): return []
    def get(self, document_id): return None
    def set_status(self, *args, **kwargs) -> None: ...
    def remove(self, document_id): return 0


def _seed(auth_store, access_store=None):
    """输入:假认证存储(可给假可见范围)。行为:建 ``admin`` / ``ops`` / ``emp`` / ``emp2`` 四个账号。输出:``{名字: 用户}``。"""
    from project_rag.application.security import hash_password

    made = {
        name: auth_store.create_user(name, hash_password("pw-123456"), role)
        for name, role in (
            ("admin", "admin"),
            ("ops", "ops"),
            ("emp", "employee"),
            ("emp2", "employee"),
        )
    }
    if access_store is not None:
        group = access_store.create_group("销售")
        access_store.set_members(group.group_id, [made["emp"].user_id])
    return made


def _headers(client, username: str, password: str = "pw-123456") -> dict[str, str]:
    """输入:客户端 + 账号。行为:登录。输出:``Authorization`` 头;失败直接断言失败。"""
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise AssertionError(f"{username} 登录该 200,实际 {response.status_code}:{response.text[:200]}")
    return {"Authorization": f"Bearer {response.json()['token']}"}


# ══ 1. 角色与能力表 ══════════════════════════════════════════════════════════
@check("角色闭集：三个角色、三张能力表，且 employee 不再等于『唯一非管理员』")
def _roles_closed_set() -> str:
    from project_rag.domain.auth import (
        KNOWLEDGE_ROLES,
        MANAGE_ROLES,
        REPORT_ROLES,
        USER_ROLES,
    )

    if USER_ROLES != ("employee", "ops", "admin"):
        raise AssertionError(f"角色闭集变了:{USER_ROLES}")
    if REPORT_ROLES != ("ops", "admin"):
        raise AssertionError(f"能看报表的该是运维 + 管理员:{REPORT_ROLES}")
    if KNOWLEDGE_ROLES != ("employee", "admin"):
        raise AssertionError(f"能用知识库的该是员工 + 管理员:{KNOWLEDGE_ROLES}")
    if MANAGE_ROLES != ("admin",):
        raise AssertionError(f"能管理的只有管理员:{MANAGE_ROLES}")
    for table in (REPORT_ROLES, KNOWLEDGE_ROLES, MANAGE_ROLES):
        stray = [role for role in table if role not in USER_ROLES]
        if stray:
            raise AssertionError(f"能力表里出现了闭集外的角色:{stray}")
    return "三角色 + 三张能力表,没有闭集外的角色"


@check("角色能力：三个角色 × 四个谓词的真值表（权限判断只有 domain/auth.py 那一处）")
def _role_truth_table() -> str:
    from project_rag.domain.auth import User

    expected = {
        # 角色: (能看报表, 能用知识库, 能管理, 不受可见范围限制)
        "employee": (False, True, False, False),
        "ops": (True, False, False, False),
        "admin": (True, True, True, True),
    }
    got = {}
    for role in ("employee", "ops", "admin"):
        user = User(user_id="u", username="u", role=role)  # type: ignore[arg-type]
        got[role] = (
            user.can_view_reports,
            user.can_use_knowledge,
            user.can_manage,
            user.sees_all_documents,
        )
    if got != expected:
        raise AssertionError(f"能力真值表不符:\n  期望 {expected}\n  实际 {got}")
    if not User(user_id="u", username="u", role="ops").is_ops:
        raise AssertionError("is_ops 该为真")
    return "employee(问答) / ops(只看报表) / admin(全能) —— 12 个格子逐一对过"


# ══ 2. 可见范围的口径(纯函数) ═══════════════════════════════════════════════
@check("口径 没规则=人人可见：规则表为空、或规则里两个名单都空，都不该挡人（空 ≠ 全禁）")
def _acl_empty_means_open() -> str:
    """两档都要验,因为它们是**两条不同的路径**:

    1. 规则表里**没有这一份**(最常见:从没配过限制);
    2. 规则表里**有这一份、但两个名单都是空的**(手工改过库、或迁移留下的空行)。

    第 2 档是反向测试 RT-1 逼出来的:只验第 1 档时,把 ``allows`` 里的
    "空即放行"删掉照样全绿 —— 因为那条路上压根没有 ``DocumentAcl`` 对象参与判断。
    """
    from project_rag.application.access import context_of, denied_documents
    from project_rag.domain.access import DocumentAcl

    if denied_documents({}, user_id="u-1") != frozenset():
        raise AssertionError("没有任何规则时禁止名单必须是空的(空 = 不禁,不是全禁)")
    context = context_of({}, user_id="u-1")
    if not context.allows("任意文档.md"):
        raise AssertionError("没有规则时任何文档都该可见")
    if context.sees_all is not False or context.restricted is not False:
        raise AssertionError("普通员工在没有规则时不该被标成『不受限』或『被收窄』")

    blank = DocumentAcl(document_id="空规则.md")
    if blank.restricted:
        raise AssertionError("两个名单都空的规则不该算『被收窄』")
    if not blank.allows(user_id="u-1", group_ids=()):
        raise AssertionError("两个名单都空的规则 = 没有规则 = 人人可见(不是谁都看不到)")
    if denied_documents({"空规则.md": blank}, user_id="u-1"):
        raise AssertionError("空规则不该出现在禁止名单里")
    return "空规则表 + 空规则对象两条路径都放行"


@check("口径 有规则就收窄：名单里的人可见、名单外的人被禁（管理员永远可见）")
def _acl_narrows() -> str:
    from project_rag.application.access import denied_documents
    from project_rag.domain.access import DocumentAcl

    rules = {"受限.md": DocumentAcl(document_id="受限.md", user_ids=("u-1",))}
    if denied_documents(rules, user_id="u-1"):
        raise AssertionError("名单里的人该看得见")
    if denied_documents(rules, user_id="u-2") != frozenset({"受限.md"}):
        raise AssertionError("名单外的人该被挡住")
    if denied_documents(rules, user_id="u-2", sees_all=True):
        raise AssertionError("管理员(sees_all)永远可见 —— 否则改错一次授权就再也改不回来")
    return "名单内可见 / 名单外被禁 / 管理员不受限"


@check("口径 组命中是并集：属于任一被授权的组即可见，多组不会互相抵消")
def _acl_group_union() -> str:
    from project_rag.application.access import denied_documents
    from project_rag.domain.access import DocumentAcl

    rules = {
        "销售手册.md": DocumentAcl(document_id="销售手册.md", group_ids=("g-销售",)),
        "技术白皮书.md": DocumentAcl(document_id="技术白皮书.md", group_ids=("g-技术",)),
    }
    both = denied_documents(rules, user_id="u-9", group_ids=("g-销售", "g-技术"))
    if both:
        raise AssertionError(f"两个组都在名单里该两份都可见,实际被禁 {both}")
    only_one = denied_documents(rules, user_id="u-9", group_ids=("g-销售",))
    if only_one != frozenset({"技术白皮书.md"}):
        raise AssertionError(f"只在一个组里时,另一个组的那份该被禁,实际 {only_one}")
    none = denied_documents(rules, user_id="u-9")
    if none != frozenset({"销售手册.md", "技术白皮书.md"}):
        raise AssertionError(f"一个组都不属于时两份都该被禁,实际 {none}")

    # **这一档专门区分并集与交集**:一份文档授权给两个组,而人只属于其中一个 ——
    # 并集可见、交集不可见。少了它,"改成交集"这种坏法会一路绿灯
    # (反向测试 RT-2 就是这么发现这条检查不够严的)。
    pair = {"双组文档.md": DocumentAcl(document_id="双组文档.md", group_ids=("g-销售", "g-技术"))}
    half = denied_documents(pair, user_id="u-9", group_ids=("g-销售",))
    if half:
        raise AssertionError(f"授权了两个组、人在其中一个里 → 该可见(并集),实际被禁 {half}")
    return "并集:两组命中→全可见 / 一组→禁另一份 / 无组→全禁 / 双组授权命中一个也可见"


@check("口径 AccessContext：allows 与 denied 必须一致（两处口径不许各说一套）")
def _acl_context_agrees() -> str:
    from project_rag.application.access import context_of
    from project_rag.domain.access import DocumentAcl

    rules = {"a.md": DocumentAcl(document_id="a.md", user_ids=("u-1",))}
    context = context_of(rules, user_id="u-2", group_ids=("g-1",))
    if context.denied != frozenset({"a.md"}) or context.allows("a.md"):
        raise AssertionError(f"denied 与 allows 不一致:{context}")
    if not context.allows("b.md"):
        raise AssertionError("不在规则表里的文档必须可见(没有规则 = 人人可见)")
    if context.restricted is not True:
        raise AssertionError("有东西被挡住时 restricted 该为真")
    return "denied={a.md} 且 allows(a.md)=False、allows(b.md)=True"


# ══ 3. 账号守卫(纯函数) ═════════════════════════════════════════════════════
@check("守卫 建号：登录名长度/空格、密码长度、角色闭集、重名（不区分大小写）")
def _guard_new_account() -> str:
    from project_rag.application.accounts import check_new_account
    from project_rag.domain.auth import User

    existing = [User(user_id="u-1", username="Boss", role="admin")]
    cases = (
        ("太短的名字", {"username": "x", "password": "pw-123456", "role": "employee"}),
        ("名字里有空格", {"username": "a b", "password": "pw-123456", "role": "employee"}),
        ("太长的名字", {"username": "长" * 40, "password": "pw-123456", "role": "employee"}),
        ("密码太短", {"username": "newbie", "password": "short", "role": "employee"}),
        ("角色不在闭集", {"username": "newbie", "password": "pw-123456", "role": "root"}),
        ("重名(大小写不同)", {"username": "boss", "password": "pw-123456", "role": "employee"}),
    )
    for title, kwargs in cases:
        try:
            check_new_account(existing, **kwargs)  # type: ignore[arg-type]
        except ValueError as exc:
            if not str(exc):
                raise AssertionError(f"{title}:抛了空消息,管理员看不懂为什么被拒") from exc
        else:
            raise AssertionError(f"{title}:该被拒,实际放行")
    name = check_new_account(
        existing, username="  新同事  ", password="pw-123456", role="ops"
    )
    if name != "新同事":
        raise AssertionError(f"合法的名字该去掉首尾空白后返回,实际 {name!r}")
    return "6 种非法输入全被拒(消息非空);合法输入返回规范化名字"


@check("守卫 删号：不能删自己、不能删最后一个管理员，目标必须存在")
def _guard_delete_account() -> str:
    from project_rag.application.accounts import check_delete_account
    from project_rag.domain.auth import User

    boss = User(user_id="u-1", username="boss", role="admin")
    other_admin = User(user_id="u-2", username="boss2", role="admin")
    worker = User(user_id="u-3", username="emp", role="employee")

    for title, users, kwargs in (
        ("删自己", [boss, worker], {"user_id": "u-1", "actor_id": "u-1"}),
        ("删最后一个管理员", [boss, worker], {"user_id": "u-1", "actor_id": "u-3"}),
        ("删不存在的人", [boss, worker], {"user_id": "u-404", "actor_id": "u-1"}),
    ):
        try:
            check_delete_account(users, **kwargs)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"{title}:该被拒,实际放行")

    # **有两个管理员时删自己也必须被拒**:少了这一档,"不能删自己"那条守卫即使被删掉,
    # 也会被"最后一个管理员"那条掩盖住(反向测试 RT-12 抓出来的)。
    try:
        check_delete_account([boss, other_admin], user_id="u-1", actor_id="u-1")
    except ValueError:
        pass
    else:
        raise AssertionError("即使还有别的管理员,也不能删自己")

    # 有两个管理员时可以删掉其中一个 —— 否则这条守卫会变成"管理员只能有一个"
    target = check_delete_account([boss, other_admin, worker], user_id="u-2", actor_id="u-1")
    if target.user_id != "u-2":
        raise AssertionError("有两个管理员时该允许删掉其中一个")
    return "三条拒绝路径 + 两个管理员时可删"


@check("守卫 改密码：长度与建号同一套（网页端不该比命令行宽松）")
def _guard_password() -> str:
    from project_rag.application.accounts import MIN_PASSWORD_CHARS, check_new_password

    try:
        check_new_password("1234567")
    except ValueError as exc:
        if str(MIN_PASSWORD_CHARS) not in str(exc):
            raise AssertionError(f"报错该说清最少几位:{exc}") from exc
    else:
        raise AssertionError("7 位密码该被拒")
    check_new_password("12345678")
    return f"少于 {MIN_PASSWORD_CHARS} 位被拒,刚好够长度放行"


# ══ 4. HTTP:账号管理 ════════════════════════════════════════════════════════
@check("接口 账号管理只有管理员：员工 403、运维 403、无令牌 401")
def _accounts_admin_only() -> str:
    client, store, _ = _client()
    _seed(store)
    admin, ops, emp = (_headers(client, name) for name in ("admin", "ops", "emp"))
    if client.get("/api/v1/admin/users", headers=admin).status_code != 200:
        raise AssertionError("管理员该能列账号")
    for who, headers in (("员工", emp), ("运维", ops)):
        code = client.get("/api/v1/admin/users", headers=headers).status_code
        if code != 403:
            raise AssertionError(f"{who}列账号该 403,实际 {code}")
    if client.get("/api/v1/admin/users").status_code != 401:
        raise AssertionError("不带令牌该 401")
    return "管理员 200 / 员工 403 / 运维 403 / 无令牌 401"


@check("接口 建号：201 + 不回显密码 + 新账号能登录；角色 ops 真的生效")
def _create_account_via_http() -> str:
    client, store, _ = _client()
    _seed(store)
    admin = _headers(client, "admin")
    response = client.post(
        "/api/v1/admin/users",
        headers=admin,
        json={"username": "newops", "password": "pw-abcdef", "role": "ops"},
    )
    if response.status_code != 201:
        raise AssertionError(f"建号该 201,实际 {response.status_code}:{response.text[:200]}")
    body = response.json()
    if "password" in body or "password_hash" in body:
        raise AssertionError(f"建号响应不许出现密码字段:{body}")
    if body["role"] != "ops":
        raise AssertionError(f"角色该照请求写:{body}")
    me = client.get("/api/v1/auth/me", headers=_headers(client, "newops", "pw-abcdef")).json()
    if (me["role"], me["can_view_reports"], me["can_use_knowledge"]) != ("ops", True, False):
        raise AssertionError(f"新建的运维能力不对:{me}")
    users = client.get("/api/v1/admin/users", headers=admin).json()["users"]
    if sum(1 for user in users if user["username"] == "newops") != 1:
        raise AssertionError(f"新账号该出现在列表里:{users}")
    return "201 + 无密码字段 + 新运维能登录且能力正确"


@check("接口 建号的失败路径是 400 + 人话（不是 500，也不是英文 SQL 错）")
def _create_account_errors() -> str:
    client, store, _ = _client()
    _seed(store)
    admin = _headers(client, "admin")
    for title, payload in (
        ("重名", {"username": "emp", "password": "pw-abcdef", "role": "employee"}),
        ("密码太短", {"username": "someone", "password": "123", "role": "employee"}),
        ("角色非法", {"username": "someone", "password": "pw-abcdef", "role": "root"}),
    ):
        response = client.post("/api/v1/admin/users", headers=admin, json=payload)
        if response.status_code != 400:
            raise AssertionError(f"{title}:该 400,实际 {response.status_code}:{response.text[:200]}")
        if not response.json().get("detail"):
            raise AssertionError(f"{title}:400 里没有 detail,前端只能显示『请求失败』")
    return "重名 / 短密码 / 非法角色 → 400 + 中文 detail"


@check("接口 删号：目标令牌立刻失效，且他从组与授权名单里被摘掉")
def _delete_account_takes_effect() -> str:
    client, store, access = _client()
    made = _seed(store, access)
    admin = _headers(client, "admin")
    victim = _headers(client, "emp")
    # 授权名单里挂上他:删号之后这些行必须消失,否则界面上会出现一个查不到的授权对象
    access.set_document_subjects(
        "手册.md", user_ids=[made["emp"].user_id], group_ids=[]
    )
    response = client.delete(f"/api/v1/admin/users/{made['emp'].user_id}", headers=admin)
    if response.status_code != 200 or response.json()["deleted"] is not True:
        raise AssertionError(f"删号该 200 + deleted=true,实际 {response.status_code}:{response.text[:200]}")
    if client.get("/api/v1/auth/me", headers=victim).status_code != 401:
        raise AssertionError("被删账号的令牌该立刻失效(401),而不是等到 TTL 结束")
    if made["emp"].user_id in access.groups_by_user():
        raise AssertionError("删号该把他从所有组里摘掉")
    if access.rules():
        raise AssertionError(f"删号该清掉以他为对象的授权行,实际还剩 {access.rules()}")
    return "令牌 401 + 组员行清零 + 授权行清零"


@check("接口 删号守卫：删自己、删最后一个管理员都是 400（界面能看懂为什么）")
def _delete_account_guards_http() -> str:
    client, store, _ = _client()
    made = _seed(store)
    admin = _headers(client, "admin")
    for title, target in (("删自己", made["admin"].user_id), ("删最后一个管理员(换个人动手)", made["admin"].user_id)):
        response = client.delete(f"/api/v1/admin/users/{target}", headers=admin)
        if response.status_code != 400:
            raise AssertionError(f"{title}:该 400,实际 {response.status_code}:{response.text[:200]}")
    # 再建一个管理员,这时删第一个就合法了
    client.post(
        "/api/v1/admin/users",
        headers=admin,
        json={"username": "admin2", "password": "pw-abcdef", "role": "admin"},
    )
    again = client.delete(
        f"/api/v1/admin/users/{made['admin'].user_id}",
        headers=_headers(client, "admin2", "pw-abcdef"),
    )
    if again.status_code != 200:
        raise AssertionError(f"有两个管理员时该能删掉一个,实际 {again.status_code}:{again.text[:200]}")
    return "删自己 400 / 最后一个管理员 400 / 有备份管理员时 200"


@check("接口 改密码：新密码能登录、旧密码不行（改的是哈希，不是明文）")
def _reset_password() -> str:
    client, store, _ = _client()
    made = _seed(store)
    admin = _headers(client, "admin")
    response = client.post(
        f"/api/v1/admin/users/{made['emp'].user_id}/password",
        headers=admin,
        json={"password": "pw-newpass"},
    )
    if response.status_code != 200:
        raise AssertionError(f"改密码该 200,实际 {response.status_code}:{response.text[:200]}")
    if "password" in response.json():
        raise AssertionError("改密码响应不许回显密码")
    _headers(client, "emp", "pw-newpass")
    stale = client.post(
        "/api/v1/auth/login", json={"username": "emp", "password": "pw-123456"}
    )
    if stale.status_code != 401:
        raise AssertionError("旧密码该失效")
    return "新密码可登录 / 旧密码 401 / 响应无密码字段"


# ══ 5. HTTP:分组与可见范围 ══════════════════════════════════════════════════
@check("接口 分组：建组 / 列组 / 设成员 / 重名报 400 / 不存在的人报 400")
def _groups_via_http() -> str:
    client, store, access = _client()
    made = _seed(store, access)
    admin = _headers(client, "admin")
    created = client.post("/api/v1/admin/groups", headers=admin, json={"name": "售后"})
    if created.status_code != 201:
        raise AssertionError(f"建组该 201,实际 {created.status_code}:{created.text[:200]}")
    group_id = created.json()["group_id"]
    duplicate = client.post("/api/v1/admin/groups", headers=admin, json={"name": "售后"})
    if duplicate.status_code != 400:
        raise AssertionError(f"重名建组该 400,实际 {duplicate.status_code}")
    member = client.put(
        f"/api/v1/admin/groups/{group_id}/members",
        headers=admin,
        json={"user_ids": [made["emp"].user_id, made["emp2"].user_id]},
    )
    if member.status_code != 200 or len(member.json()["member_ids"]) != 2:
        raise AssertionError(f"设成员该 200 且两人都在,实际 {member.status_code}:{member.text[:200]}")
    ghost = client.put(
        f"/api/v1/admin/groups/{group_id}/members",
        headers=admin,
        json={"user_ids": ["u-不存在"]},
    )
    if ghost.status_code != 400:
        raise AssertionError(f"不存在的人该 400,实际 {ghost.status_code}")
    groups = client.get("/api/v1/admin/groups", headers=admin).json()["groups"]
    if len(groups) != 2:  # 种子里那个 + 新建的
        raise AssertionError(f"列组该给出全部组:{groups}")
    return "建组 201 / 重名 400 / 设成员 200 / 幽灵成员 400 / 列组含种子组"


@check("接口 删组：连带清掉『授权给这个组』的规则（否则会串到同名的下一个组）")
def _delete_group_clears_rules() -> str:
    client, store, access = _client()
    _seed(store, access)
    admin = _headers(client, "admin")
    group_id = next(iter(access.groups))
    access.set_document_subjects("内部资料.md", user_ids=[], group_ids=[group_id])
    if not client.get("/api/v1/admin/documents/内部资料.md/acl", headers=admin).json()["restricted"]:
        raise AssertionError("前置条件不成立:规则没设上")
    client.delete(f"/api/v1/admin/groups/{group_id}", headers=admin)
    acl = client.get("/api/v1/admin/documents/内部资料.md/acl", headers=admin).json()
    if acl["restricted"] or acl["group_ids"]:
        raise AssertionError(f"删组该把授权给它的规则一起清掉,实际 {acl}")
    return "删组后那份文档回到『人人可见』"


@check("接口 文档可见范围：PUT/GET 往返；两个名单都空 = 取消限制（不是谁都看不到）")
def _document_acl_http() -> str:
    client, store, access = _client()
    made = _seed(store, access)
    admin = _headers(client, "admin")
    group_id = next(iter(access.groups))
    put = client.put(
        "/api/v1/admin/documents/手册.md/acl",
        headers=admin,
        json={"user_ids": [made["emp2"].user_id], "group_ids": [group_id]},
    )
    if put.status_code != 200:
        raise AssertionError(f"设范围该 200,实际 {put.status_code}:{put.text[:200]}")
    body = put.json()
    if (body["restricted"], body["user_ids"], body["group_ids"]) != (
        True,
        [made["emp2"].user_id],
        [group_id],
    ):
        raise AssertionError(f"范围该原样返回:{body}")
    if client.get(
        f"/api/v1/admin/documents/手册.md/acl", headers=admin
    ).json() != body:
        raise AssertionError("GET 该回同一份范围(两次读必须一致)")
    cleared = client.put(
        "/api/v1/admin/documents/手册.md/acl",
        headers=admin,
        json={"user_ids": [], "group_ids": []},
    ).json()
    if cleared["restricted"] or cleared["user_ids"] or cleared["group_ids"]:
        raise AssertionError(f"空名单 = 取消限制,实际 {cleared}")
    return "PUT/GET 一致;清空 → restricted=false"


@check("接口 可见范围只认存在的对象：幽灵用户 / 幽灵组都是 400")
def _document_acl_validation() -> str:
    client, store, access = _client()
    _seed(store, access)
    admin = _headers(client, "admin")
    for title, payload in (
        ("幽灵用户", {"user_ids": ["u-幽灵"], "group_ids": []}),
        ("幽灵组", {"user_ids": [], "group_ids": ["g-幽灵"]}),
    ):
        response = client.put("/api/v1/admin/documents/x.md/acl", headers=admin, json=payload)
        if response.status_code != 400:
            raise AssertionError(f"{title}:该 400,实际 {response.status_code}:{response.text[:200]}")
    return "两种幽灵对象都被 400 挡下"


@check("接口 可见范围也要求管理员：员工与运维都是 403")
def _document_acl_admin_only() -> str:
    client, store, access = _client()
    _seed(store, access)
    for who in ("emp", "ops"):
        headers = _headers(client, who)
        if client.get("/api/v1/admin/documents/x.md/acl", headers=headers).status_code != 403:
            raise AssertionError(f"{who} 读可见范围该 403")
        if client.put(
            "/api/v1/admin/documents/x.md/acl", headers=headers, json={"user_ids": [], "group_ids": []}
        ).status_code != 403:
            raise AssertionError(f"{who} 改可见范围该 403")
    return "员工 / 运维的读与写都是 403"


# ══ 6. 落地:检索 / 清单 / 预览 / WS ═════════════════════════════════════════
@check("落地 检索：被禁文档的块不许出现在引用里（两路都过滤）")
def _retrieval_respects_denied() -> str:
    from _fakes import FakeEmbedder, FakeVectorStore
    from project_rag.application.answer_rag import retrieve
    from project_rag.domain import Chunk, RetrievalOptions, RetrievedChunk

    def hit(doc_id: str, text: str):
        """一条命中:假向量库收的是 ``RetrievedChunk``(检索那一层只认它)。"""
        return RetrievedChunk(
            chunk=Chunk(chunk_id=f"{doc_id}::{text}", text=text, source="uploads", doc_id=doc_id),
            score=0.9,
        )

    hits = [hit("机密.md", "机密内容"), hit("公开.md", "公开内容")]
    store = FakeVectorStore(hits)
    options = RetrievalOptions(k=5, max_context=5, denied_docs=("机密.md",))
    citations = retrieve(
        "内容",
        embedder=FakeEmbedder(),
        store=store,  # type: ignore[arg-type] - 假件只实现端口里用到的那几个方法
        keyword_index=None,
        options=options,
    )
    leaked = [c.chunk.doc_id for c in citations if c.chunk.doc_id == "机密.md"]
    if leaked:
        raise AssertionError(f"被禁文档的块漏进了引用:{leaked}")
    if not citations:
        raise AssertionError("公开文档该照常被检索到(过滤不能把整条路关掉)")

    # 空禁止名单 = 不限制:同样的两路检索该拿到两条
    everything = retrieve(
        "内容",
        embedder=FakeEmbedder(),
        store=store,  # type: ignore[arg-type]
        keyword_index=None,
        options=RetrievalOptions(k=5, max_context=5),
    )
    if len({c.chunk.doc_id for c in everything}) != 2:
        raise AssertionError(f"没有禁止名单时两份文档都该能检索到:{everything}")
    return "禁止名单生效(机密不出现) / 空名单不限制(两份都在)"


@check("落地 检索：过滤在截断之前（多要几条，不能因为被禁就少给）")
def _retrieval_overfetches() -> str:
    from _fakes import FakeEmbedder, FakeVectorStore
    from project_rag.application.answer_rag import retrieve
    from project_rag.domain import Chunk, RetrievalOptions, RetrievedChunk

    # 前 4 条全是机密,后面才是公开的:先截断到 k=2 再过滤会拿到 0 条
    def _hit(chunk_id: str, doc_id: str):
        return RetrievedChunk(
            chunk=Chunk(chunk_id=chunk_id, text="内容", source="uploads", doc_id=doc_id), score=0.9
        )

    hits = [_hit(f"c{i}", "机密.md") for i in range(4)]
    hits += [_hit(f"p{i}", "公开.md") for i in range(3)]
    citations = retrieve(
        "内容",
        embedder=FakeEmbedder(),
        store=FakeVectorStore(hits),  # type: ignore[arg-type]
        keyword_index=None,
        options=RetrievalOptions(k=2, max_context=5, denied_docs=("机密.md",)),
    )
    docs = {c.chunk.doc_id for c in citations}
    if docs != {"公开.md"} or len(citations) != 2:
        raise AssertionError(f"该多要几条再过滤,拿到 2 条公开文档;实际 {docs}({len(citations)} 条)")
    return f"k=2 时仍拿满 2 条公开文档({len(citations)} 条,全是公开.md)"


@check("落地 清单：员工看不到受限文档、管理员看得到且标着 restricted")
def _documents_list_respects_acl() -> str:
    client, store, access = _client()
    made = _seed(store, access)
    from project_rag.cli.deps import Deps

    # 直接换一个带文档的客户端:清单要登记表 + 磁盘 + 向量库三处都有东西才算 ok,
    # 这里只关心"哪几行出现",所以三处都喂上同一份。
    from project_rag.adapters.memory_vector_store import InMemoryVectorStore
    from project_rag.domain import Chunk, Embedding
    from project_rag.domain.documents import DocRecord

    runtime = InMemoryVectorStore()
    runtime.upsert(
        [Chunk(chunk_id="a", text="x", source="uploads", doc_id="公开.md"),
         Chunk(chunk_id="b", text="y", source="uploads", doc_id="机密.md")],
        [Embedding(dense=(1.0, 0.0)), Embedding(dense=(0.0, 1.0))],
    )

    class _Docs:
        def init_schema(self): ...
        def add(self, record): ...
        def upsert(self, record): ...
        def list_all(self):
            return [
                DocRecord(document_id=doc, source="uploads", filename=doc, status="processed",
                          chunks=1, size_bytes=1, created_at="2025-01-01T00:00:00")
                for doc in ("公开.md", "机密.md")
            ]
        def get(self, document_id): return None
        def set_status(self, *a, **k): ...
        def remove(self, document_id): return 0

    def _files():
        from pathlib import Path

        from project_rag.adapters.document_loader import DiskFile

        return [
            DiskFile(path=Path("data/uploads") / name, size_bytes=1, source="uploads", doc_ids=(name,))
            for name in ("公开.md", "机密.md")
        ]

    fields = {
        "auth_store_factory": lambda: store,
        "access_store_factory": lambda: access,
        "document_store_factory": _Docs,
        "vector_store_factory": lambda: runtime,
        "disk_files_factory": _files,
    }
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app

    client = TestClient(create_app(Deps(**fields)))
    finance = access.create_group("财务")  # 员工不在这个组里 → 机密.md 对他不可见
    access.set_document_subjects("机密.md", user_ids=[], group_ids=[finance.group_id])
    emp = client.get("/api/v1/documents", headers=_headers(client, "emp")).json()
    names = [row["document_id"] for row in emp["documents"]]
    if names != ["公开.md"]:
        raise AssertionError(f"员工该只看到公开那份,实际 {names}")
    if emp["summary"]["total"] != 1:
        raise AssertionError(f"汇总该按过滤后的行数算,实际 {emp['summary']}")
    admin = client.get("/api/v1/documents", headers=_headers(client, "admin")).json()
    if [row["document_id"] for row in admin["documents"]] != ["公开.md", "机密.md"]:
        raise AssertionError("管理员该看到两份")
    restricted = {row["document_id"]: row["restricted"] for row in admin["documents"]}
    if restricted != {"公开.md": False, "机密.md": True}:
        raise AssertionError(f"restricted 标注不对:{restricted}")
    return "员工 1 行(汇总也是 1)/ 管理员 2 行且 restricted 标注正确"


@check("落地 预览：看不见的文档回 404（不是 403 —— 存在本身也是信息）")
def _preview_hidden_is_404() -> str:
    client, store, access = _client()
    _seed(store, access)
    from fastapi.testclient import TestClient

    from _fakes import FakeVectorStore
    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps
    from project_rag.domain import Chunk, RetrievedChunk

    class _Docs:
        def init_schema(self): ...
        def add(self, record): ...
        def upsert(self, record): ...
        def list_all(self): return []
        def get(self, document_id):
            from project_rag.domain.documents import DocRecord

            # **只知道这一份**:另一份真的"根本不存在",两条 404 才有可比性
            if document_id != "机密.md":
                return None
            return DocRecord(document_id=document_id, source="uploads", filename=document_id,
                             status="processed", chunks=1, size_bytes=1, created_at="t")
        def set_status(self, *a, **k): ...
        def remove(self, document_id): return 0

    vector = FakeVectorStore(
        [
            RetrievedChunk(
                chunk=Chunk(chunk_id="a", text="机密", source="uploads", doc_id="机密.md"),
                score=0.9,
            )
        ]
    )
    client = TestClient(
        create_app(
            Deps(
                auth_store_factory=lambda: store,
                access_store_factory=lambda: access,
                document_store_factory=_Docs,
                vector_store_factory=lambda: vector,
            )
        )
    )
    finance = access.create_group("财务")
    access.set_document_subjects("机密.md", user_ids=[], group_ids=[finance.group_id])
    for who, expected in (("emp", 404), ("admin", 200)):
        code = client.get("/api/v1/documents/机密.md/chunks", headers=_headers(client, who)).status_code
        if code != expected:
            raise AssertionError(f"{who} 预览该 {expected},实际 {code}")
    # "看不见"与"不存在"的响应必须**只在请求的那个 ID 上不同**(那是调用方自己给的名字,
    # 不是泄露):同一句话、同一个状态码。差别一旦出现,消息本身就成了一条存在性探针。
    emp = _headers(client, "emp")
    missing = client.get("/api/v1/documents/根本不存在.md/chunks", headers=emp)
    hidden = client.get("/api/v1/documents/机密.md/chunks", headers=emp)
    if missing.status_code != hidden.status_code:
        raise AssertionError(f"状态码不同:{missing.status_code} / {hidden.status_code}")
    shape_of = lambda body, name: body["detail"].replace(name, "<ID>")  # noqa: E731
    if shape_of(missing.json(), "根本不存在.md") != shape_of(hidden.json(), "机密.md"):
        raise AssertionError(
            f"『看不见』与『不存在』的话术必须一致(否则消息就是存在性探针):"
            f"{missing.json()} / {hidden.json()}"
        )
    return "员工 404 / 管理员 200;两条 404 只差调用方给的那个 ID"


@check("落地 运维：知识库各接口全 403，报表明细照常")
def _ops_scope() -> str:
    client, store, _ = _client()
    _seed(store)
    ops = _headers(client, "ops")
    blocked = {
        "sources": client.get("/api/v1/sources", headers=ops),
        "documents": client.get("/api/v1/documents", headers=ops),
        "preview": client.get("/api/v1/documents/x.md/chunks", headers=ops),
        "sessions": client.get("/api/v1/sessions", headers=ops),
        "history": client.get("/api/v1/history/任意", headers=ops),
        "create_session": client.post("/api/v1/create_session", headers=ops),
    }
    wrong = {name: response.status_code for name, response in blocked.items() if response.status_code != 403}
    if wrong:
        raise AssertionError(f"运维在知识库接口上该全 403,实际 {wrong}")
    query = client.post("/api/v1/query", headers=ops, json={"query": "装饰器"})
    if query.status_code != 403:
        raise AssertionError(f"运维不该能提问,实际 {query.status_code}")
    for path in ("/api/v1/reports/metrics?window=24h", "/api/v1/reports/cost?window=24h"):
        code = client.get(path, headers=ops).status_code
        if code != 200:
            raise AssertionError(f"运维该能看 {path},实际 {code}")
    if client.get("/api/v1/reports/cost?window=24h", headers=_headers(client, "emp")).status_code != 403:
        raise AssertionError("员工不该能看消耗报表")
    return "6 个知识库接口 + 提问全 403;两个报表放行(员工看报表仍 403)"


@check("落地 运维：WebSocket 连接被拒（令牌有效但角色不对，不给他反复撞墙）")
def _ops_websocket_blocked() -> str:
    from starlette.websockets import WebSocketDisconnect

    client, store, _ = _client()
    _seed(store)
    token = client.post(
        "/api/v1/auth/login", json={"username": "ops", "password": "pw-123456"}
    ).json()["token"]
    try:
        with client.websocket_connect(f"/api/v1/stream?token={token}") as socket:
            frame = socket.receive_json()
            if frame.get("type") != "error" or "运维" not in frame.get("detail", ""):
                raise AssertionError(f"该先回一帧说明原因的 error,实际 {frame}")
            socket.receive_json()  # 服务端接着 close(1008)
    except WebSocketDisconnect as exc:
        if exc.code != 1008:
            raise AssertionError(f"该用 1008(策略拒绝)关掉,实际 {exc.code}")
        return "error 帧说明角色不符 + 1008 关闭"
    raise AssertionError("运维的 WS 连接不该被接受")


@check("落地 无认证模式：不注入 auth/access 时一切照常（老契约仍有效的原因）")
def _no_auth_still_open() -> str:
    """无认证模式(只可能来自显式注入)下**不许**多出一道 403。

    这一条是给"老契约仍然有效"作保的:``check_m5_api`` 那 26 条注入的就是无认证的 Deps,
    要是这里加了角色门,它们会莫名其妙地全红。
    """
    client, _, _ = _client(auth=False, access=False)
    for path in ("/api/v1/documents", "/api/v1/sources", "/api/v1/sessions"):
        code = client.get(path).status_code
        if code != 200:
            raise AssertionError(f"无认证模式下 {path} 该放行,实际 {code}")
    if client.post("/api/v1/create_session").status_code != 200:
        raise AssertionError("无认证模式下建会话该放行")
    greeting = client.post("/api/v1/query", json={"query": "你好"})
    if greeting.status_code != 200 or greeting.json()["channel"] != "greeting":
        raise AssertionError(f"无认证模式下提问该放行,实际 {greeting.status_code}:{greeting.text[:200]}")
    if client.get("/api/v1/auth/me").status_code != 503:
        raise AssertionError("无认证模式下 /me 该 503(而不是假装登录成功)")
    return "清单 / 来源 / 会话 / 问候都放行;/me 明确 503"


@check("落地 读不到规则时 503：数据库抖动不许变成『全网可见』")
def _access_read_failure_is_503() -> str:
    client, store, _ = _client(fail_access=True)
    _seed(store)
    # 用**管理员**读:员工在报表上本来就 403,那会掩盖"读不到规则"这个真正的失败原因
    headers = _headers(client, "admin")
    for path in ("/api/v1/documents", "/api/v1/documents/任意.md/chunks"):
        code = client.get(path, headers=headers).status_code
        if code != 503:
            raise AssertionError(f"读不到可见范围时 {path} 该 503(不放行),实际 {code}")
    body = client.post("/api/v1/query", headers=headers, json={"query": "装饰器"}).json()
    if body.get("detail") and "可见范围" not in body["detail"]:
        raise AssertionError(f"503 的 detail 该说清是可见范围读不到:{body}")
    return "清单 / 预览 / 提问都是 503(方向朝关闭)"


@check("落地 删文档：连带清掉它的可见范围规则（否则同名重传会继承旧授权）")
def _delete_document_clears_acl() -> str:
    from fastapi.testclient import TestClient

    from _fakes import FakeVectorStore
    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps
    from project_rag.domain import Chunk

    client, store, access = _client()
    _seed(store, access)
    removed: list[str] = []

    class _Docs:
        def init_schema(self): ...
        def add(self, record): ...
        def upsert(self, record): ...
        def list_all(self): return []
        def get(self, document_id):
            from project_rag.domain.documents import DocRecord

            return DocRecord(document_id=document_id, source="uploads", filename=document_id,
                             status="processed", chunks=1, size_bytes=1, created_at="t")
        def set_status(self, *a, **k): ...
        def remove(self, document_id):
            removed.append(document_id)
            return 1

    vector = FakeVectorStore([Chunk(chunk_id="a", text="x", source="uploads", doc_id="机密.md")])
    client = TestClient(
        create_app(
            Deps(
                auth_store_factory=lambda: store,
                access_store_factory=lambda: access,
                document_store_factory=_Docs,
                vector_store_factory=lambda: vector,
                disk_files_factory=lambda: (),
            )
        )
    )
    finance = access.create_group("财务")
    access.set_document_subjects("机密.md", user_ids=[], group_ids=[finance.group_id])
    response = client.delete("/api/v1/documents/机密.md", headers=_headers(client, "admin"))
    if response.status_code != 200:
        raise AssertionError(f"删文档该 200,实际 {response.status_code}:{response.text[:200]}")
    if access.rules():
        raise AssertionError(f"删文档该清掉它的规则,实际还剩 {access.rules()}")
    if removed != ["机密.md"]:
        raise AssertionError(f"前置条件不成立:登记行没被删({removed})")
    return "删文档后规则表里不再有它"


def main() -> int:
    return report(
        "P0-6 契约：运维角色 / 账号管理 / 分组 / 文档可见范围",
        notes=(
            "说明：全部内存假件（不需要容器 / 模型 / 网络）；真 MySQL 往返见 check_p0_7_cost.py。",
            "口径：没配规则的文档人人可见；配了规则的只有名单里的人 / 组 + 管理员可见。",
        ),
        hint="提示：建号 `POST /api/v1/admin/users`；分组 `/api/v1/admin/groups`；"
        "文档范围 `PUT /api/v1/admin/documents/{id}/acl`。",
    )


if __name__ == "__main__":
    sys.exit(main())

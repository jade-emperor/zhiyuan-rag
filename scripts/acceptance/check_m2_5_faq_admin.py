#!/usr/bin/env python
"""契约:FAQ 管理台 —— 问答对的增 / 改 / 删(接口 + 页面)。

用法：

    uv run python scripts/acceptance/check_m2_5_faq_admin.py

**这一批补的是什么**:FAQ 表原来只能由(不随仓库发布的)装载脚本灌一次,
之后要改一条就得连库手写 SQL。这一批给它一个管理台:

    GET    /api/v1/admin/faq            -> {entries, total}      （管理员）
    POST   /api/v1/admin/faq            -> FaqOut · 201          （管理员）
    PATCH  /api/v1/admin/faq/{id}       -> FaqOut                （管理员）
    DELETE /api/v1/admin/faq/{id}       -> {entry_id, deleted}   （管理员）

本脚本固化的口径(改它们 = 改契约):

1. **读端口与写端口分开**:答题路径只认识 ``FaqStore.load_all``;``add/update/remove``
   在 ``FaqEditor`` 上。给只读存储装上"管理"接口,是把一次缺装配变成一堆 500;
2. **查重是用例层的事**:压空白 + 忽略大小写后完全相同才算重复(409,回执点名已有 ID),
   "两个问法很像"不拦 —— 那是匹配器打分的活;
3. **上限只有一处**:问题 512 字、答案 8000 字(与老表 ``VARCHAR(512)`` / ``TEXT`` 对齐);
4. **删除幂等**:删不存在的给 ``deleted=0``,不报 404 —— 重复点删除不该报错;
5. **改完下一次提问就生效**:web 每个请求都读库,没有旁路缓存(CLI 的 ``faq`` 命令除外,
   它吃 ``cache.ttl_s``);
6. **只有管理员能改**:员工 403、无令牌 401。

前半段用内存假件;MySQL 那两条用真库(连不上就 SKIP,并点名 owner);前端是静态契约(读源码)。
"""

from __future__ import annotations

import inspect
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from _fakes import FakeConversationStore, FakeFaqStore
from _harness import SkipCheck, check, report

if TYPE_CHECKING:  # 只为注解;运行时的导入留在函数里(import 期不该连/读任何东西)
    from project_rag.domain.auth import UserRole

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"

MYSQL_ENV = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "10093")),
    "user": os.environ.get("MYSQL_USER", "rag"),
    "password": os.environ.get("MYSQL_PASSWORD", "1234"),
    "database": os.environ.get("MYSQL_DATABASE", "rag"),
    "charset": "utf8mb4",
}


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _FakeAuthStore:
    """最小认证存储:登录 + 令牌 + 归属。角色只有 admin / employee 两种。"""

    def __init__(self) -> None:
        self.users: dict[str, tuple[Any, str]] = {}
        self.tokens: dict[str, Any] = {}
        self.owners: dict[str, str] = {}
        self.schemas = 0

    def init_schema(self) -> None:
        self.schemas += 1

    def create_user(self, username: str, password_hash: str, role: UserRole):
        from project_rag.domain.auth import User

        user = User(user_id=f"u{len(self.users) + 1}", username=username, role=role)
        self.users[username] = (user, password_hash)
        return user

    def find_by_username(self, username: str):
        return self.users.get(username)

    def set_password(self, user_id: str, password_hash: str) -> None:
        for name, (user, _) in list(self.users.items()):
            if user.user_id == user_id:
                self.users[name] = (user, password_hash)

    def list_users(self):
        return [user for user, _ in self.users.values()]

    def delete_user(self, user_id: str) -> int:
        return 0

    def issue_token(self, user, token_hash: str, ttl_seconds: int) -> None:
        self.tokens[token_hash] = user

    def user_for_token(self, token_hash: str):
        return self.tokens.get(token_hash)

    def revoke_token(self, token_hash: str) -> None:
        self.tokens.pop(token_hash, None)

    def revoke_all_tokens(self, user_id: str) -> int:
        return 0

    def bind_session(self, session_id: str, user_id: str) -> None:
        self.owners.setdefault(session_id, user_id)

    def owner_of(self, session_id: str):
        return self.owners.get(session_id)


class _ReadOnlyStore:
    """只有 ``load_all`` 的存储:管理接口该回 503,而不是 500。"""

    def __init__(self, entries=()) -> None:
        self.entries = list(entries)

    def load_all(self):
        return list(self.entries)


class _BrokenStore:
    """读写都在,但写操作抛错:接口把异常翻成人话(500 + 点名类型),不让它裸奔。"""

    def load_all(self):
        return []

    def add(self, question, answer):
        raise RuntimeError("库挂了")

    def update(self, entry_id, *, question=None, answer=None):
        raise RuntimeError("库挂了")

    def remove(self, entry_id):
        raise RuntimeError("库挂了")


# ── 夹具 ─────────────────────────────────────────────────────────────────────
def _client(store, *, auth: _FakeAuthStore | None = None):
    """输入:FAQ 存储、认证存储(可省)。行为:建注入了依赖的 TestClient。输出:(client, auth)。"""
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps

    auth = auth if auth is not None else _FakeAuthStore()
    if not auth.users:
        auth.create_user("boss", hash_password("pw-123456"), "admin")
        auth.create_user("worker", hash_password("pw-123456"), "employee")
    deps = Deps(
        faq_store_factory=lambda: store,
        auth_store_factory=lambda: auth,
        conversation_store_factory=lambda: FakeConversationStore([]),
    )
    return TestClient(create_app(deps)), auth


def _headers(client, username: str = "boss") -> dict[str, str]:
    """输入:客户端、用户名。行为:登录并取令牌。输出:鉴权头。"""
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-123456"}
    )
    if resp.status_code != 200:
        raise AssertionError(f"{username} 登录该 200,实际 {resp.status_code}:{resp.text[:200]}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _entries(count: int = 3):
    from project_rag.domain import FaqEntry

    return [
        FaqEntry(id=index + 1, question=f"问题 {index + 1}", answer=f"答案 {index + 1}")
        for index in range(count)
    ]


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _web(relative: str) -> str:
    """输入:相对 web/src 的路径。行为:读它。输出:正文(不在就报错)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— FAQ 管理页还没落地?")
    return path.read_text(encoding="utf-8")


def _mysql_connect():
    """输入:无。行为:连真 MySQL。输出:连接。连不上抛 SkipCheck(点名 owner)。"""
    import pymysql

    try:
        return pymysql.connect(
            host=MYSQL_ENV["host"],
            port=MYSQL_ENV["port"],
            user=MYSQL_ENV["user"],
            password=MYSQL_ENV["password"],
            database=MYSQL_ENV["database"],
            charset=MYSQL_ENV["charset"],
            autocommit=True,
            connect_timeout=3,
        )
    except Exception as exc:  # noqa: BLE001 - 环境不在就是 SKIP,不是 FAIL
        raise SkipCheck(
            "MySQL",
            f"连不上 {MYSQL_ENV['host']}:{MYSQL_ENV['port']}({type(exc).__name__});"
            "真库那两条留给 services 档,owner = 部署环境",
        ) from exc


def _entry(entry_id: int, question: str, answer: str):
    """输入:ID、问题、答案。行为:造一条领域问答对。输出:FaqEntry。"""
    from project_rag.domain import FaqEntry

    return FaqEntry(id=entry_id, question=question, answer=answer)


def _extra():
    """输入:无。行为:造一条"只在答案里出现关键字"的条目。输出:FaqEntry。"""
    return _entry(9, "公司配什么键盘", "Cherry 红轴")


# ── 端口与适配器 ─────────────────────────────────────────────────────────────
@check("端口：FaqStore 只有 load_all（答题路径不该有写能力）；FaqEditor / FaqAdminStore 是 Protocol")
def _ports() -> str:
    from project_rag.ports import faq as module

    for name in ("FaqStore", "FaqEditor", "FaqAdminStore"):
        port = getattr(module, name)
        if not getattr(port, "_is_protocol", False):
            raise AssertionError(f"{name} 不是 typing.Protocol")
    read_methods = {
        name
        for name, value in vars(module.FaqStore).items()
        if inspect.isfunction(value) and not name.startswith("__")
    }
    if read_methods != {"load_all"}:
        raise AssertionError(f"FaqStore 只该有 load_all,实际 {sorted(read_methods)}")
    specs = {
        "add": ("question", "answer"),
        "update": ("entry_id", "question", "answer"),
        "remove": ("entry_id",),
    }
    for method_name, want in specs.items():
        fn = getattr(module.FaqEditor, method_name, None)
        if fn is None:
            raise AssertionError(f"FaqEditor 缺 {method_name}")
        got = tuple(
            p
            for p in inspect.signature(fn).parameters
            if p != "self"
        )
        if got != want:
            raise AssertionError(f"FaqEditor.{method_name} 参数应为 {want},实际 {got}")
    bases = set(module.FaqAdminStore.__bases__)
    if not {module.FaqStore, module.FaqEditor}.issubset(bases):
        raise AssertionError(f"FaqAdminStore 该同时继承读与写两个端口,实际 {bases}")
    return "FaqStore 只读;FaqEditor 三个写方法;FaqAdminStore 两半合一;三者都是 Protocol"


@check("适配器：MySQLFaqStore 两半都实现；建表与老表同形（VARCHAR(512)/TEXT + utf8mb4 口径）")
def _adapter() -> str:
    from project_rag.adapters.faq_mysql import MySQLFaqStore

    for name in ("load_all", "add", "update", "remove", "ensure_table"):
        if not callable(getattr(MySQLFaqStore, name, None)):
            raise AssertionError(f"MySQLFaqStore 缺 {name}")
    source = (ROOT / "src/project_rag/adapters/faq_mysql.py").read_text(encoding="utf-8")
    if "question VARCHAR(512)" not in source or "answer TEXT" not in source:
        raise AssertionError("建表语句与老表不同形(问题 512 / 答案 TEXT)")
    if "TABLE_OPTIONS" not in source:
        raise AssertionError("建表尾巴没走 mysql_schema.TABLE_OPTIONS(漏 COLLATE 会掉进 1267)")
    if "information_schema.TABLES" not in source:
        raise AssertionError("老表的排序规则没扳回来(CREATE IF NOT EXISTS 不会改已存在的表)")
    return "五个方法齐;DDL 与老表同形;建表尾巴走 TABLE_OPTIONS;老表排序规则自愈"


# ── 用例层 ───────────────────────────────────────────────────────────────────
@check("用例 新建：清洗首尾空白；空/超长 → ValueError；同问（压空白+忽略大小写）→ FaqExists 并点名 ID")
def _create_rules() -> str:
    from project_rag.application.faq_admin import (
        MAX_ANSWER_CHARS,
        MAX_QUESTION_CHARS,
        FaqExists,
        create_entry,
    )
    from project_rag.domain import FaqEntry

    store = FakeFaqStore([FaqEntry(id=7, question="String 和 StringBuilder 的区别?", answer="旧答案")])
    created = create_entry(store, question="  新问题  ", answer="  新答案  ")
    if created.question != "新问题" or created.answer != "新答案":
        raise AssertionError(f"首尾空白没清: {created!r}")
    if created.id == 0:
        raise AssertionError("回执没有新 ID(前端拿它做后续编辑)")
    for bad_question, bad_answer in (("   ", "答案"), ("问题", "  ")):
        try:
            create_entry(store, question=bad_question, answer=bad_answer)
        except ValueError:
            continue
        raise AssertionError(f"空值该 400: {bad_question!r}/{bad_answer!r}")
    try:
        create_entry(store, question="问" * (MAX_QUESTION_CHARS + 1), answer="答案")
        raise AssertionError("超长问题该 400")
    except ValueError:
        pass
    try:
        create_entry(store, question="问题", answer="答" * (MAX_ANSWER_CHARS + 1))
        raise AssertionError("超长答案该 400")
    except ValueError:
        pass
    try:
        create_entry(store, question="  string   和 stringbuilder 的区别? ", answer="重复")
        raise AssertionError("压空白+忽略大小写后相同的问题该 409")
    except FaqExists as exc:
        if "7" not in str(exc):
            raise AssertionError(f"重复回执该点名已有 ID,实际 {exc}")
    if ("add", " 新问题  ", " 新答案  ") in store.writes:
        raise AssertionError("用例层把没清洗的值传给了存储")
    return f"空白已清;空值与 {MAX_QUESTION_CHARS}/{MAX_ANSWER_CHARS} 上限驳回;重复指名 ID 7"


@check("用例 修改：可只给一个字段；改成自己的问法不算重复；ID 不存在 → LookupError；空补丁 → ValueError")
def _update_rules() -> str:
    from project_rag.application.faq_admin import FaqExists, update_entry
    from project_rag.domain import FaqEntry

    store = FakeFaqStore(
        [
            FaqEntry(id=1, question="怎么退款", answer="找客服"),
            FaqEntry(id=2, question="怎么开发票", answer="找财务"),
        ]
    )
    partial = update_entry(store, 1, answer="提交工单")
    if partial.question != "怎么退款" or partial.answer != "提交工单":
        raise AssertionError(f"只改答案时问句被动了: {partial!r}")
    same = update_entry(store, 2, question=" 怎么开发票 ")
    if same.id != 2:
        raise AssertionError("改成自己的问法(只差空白)不该算重复")
    try:
        update_entry(store, 1, question="怎么开发票")
        raise AssertionError("改成别人的问法该 409")
    except FaqExists:
        pass
    try:
        update_entry(store, 99, answer="x")
        raise AssertionError("ID 不存在该 LookupError(接口翻 404)")
    except LookupError:
        pass
    try:
        update_entry(store, 1)
        raise AssertionError("一个字段都不给该 ValueError")
    except ValueError:
        pass
    return "单字段修改保留另一个字段;自成一体不算重复;404/400 语义各就位"


@check("用例 删除：幂等（不存在给 0）；ID 非正数 → ValueError；过滤同时搜问题与答案")
def _delete_and_filter() -> str:
    from project_rag.application.faq_admin import delete_entry, filter_entries
    from project_rag.domain import FaqEntry

    store = FakeFaqStore(
        [
            FaqEntry(id=1, question="HashSet 怎么去重", answer="用 HashSet 的 add 返回值"),
            FaqEntry(id=2, question="StringBuilder 线程安全吗", answer="不安全,用 StringBuffer"),
        ]
    )
    entries = store.load_all()
    if len(filter_entries(entries, "")) != len(entries):
        raise AssertionError("空搜索该给全部")
    if [e.id for e in filter_entries(entries, "stringbuilder")] != [2]:
        raise AssertionError("问题里的词没搜到(且该忽略大小写)")
    if [e.id for e in filter_entries(entries, "add 返回值")] != [1]:
        raise AssertionError("答案正文里的词没搜到 —— 维护时'我记得答案里提过 X'是常见线索")
    if delete_entry(store, 1) != 1:
        raise AssertionError("删存在的该回 1")
    if delete_entry(store, 1) != 0:
        raise AssertionError("删不存在的该回 0(幂等),不是 404")
    try:
        delete_entry(store, -1)
        raise AssertionError("ID 非正数该 ValueError")
    except ValueError:
        pass
    return "过滤覆盖问题与答案、忽略大小写;删除幂等;ID 非正数 → 400 语义"


# ── HTTP ────────────────────────────────────────────────────────────────────
@check("接口 清单：管理员 200（entries+total）；员工 403；无令牌 401")
def _api_list() -> str:
    store = FakeFaqStore([*_entries(3), _extra()])
    client, _ = _client(store)
    boss = _headers(client)
    resp = client.get("/api/v1/admin/faq", headers=boss)
    if resp.status_code != 200:
        raise AssertionError(f"管理员该 200,实际 {resp.status_code}:{resp.text[:200]}")
    body = resp.json()
    if body["total"] != 4 or len(body["entries"]) != 4:
        raise AssertionError(f"清单该 4 条,实际 {body}")
    if sorted(body["entries"][0]) != ["answer", "id", "question"]:
        raise AssertionError(f"出参字段该 id/question/answer,实际 {sorted(body['entries'][0])}")
    filtered = client.get("/api/v1/admin/faq?query=键盘", headers=boss).json()
    if filtered["total"] != 4 or len(filtered["entries"]) != 1:
        raise AssertionError(f"过滤后 total 仍是总数、entries 只剩命中,实际 {filtered}")
    if client.get("/api/v1/admin/faq", headers=_headers(client, "worker")).status_code != 403:
        raise AssertionError("员工看 FAQ 管理该 403")
    if client.get("/api/v1/admin/faq").status_code != 401:
        raise AssertionError("无令牌该 401")
    return "4 条;query 命中答案正文;total 是过滤前总数;员工 403 / 无令牌 401"


@check("接口 新建：201 回执带新 ID；同问 409 且点名已有 ID；空问题 400；员工 403")
def _api_create() -> str:
    store = FakeFaqStore([])
    client, _ = _client(store)
    boss = _headers(client)
    first = client.post(
        "/api/v1/admin/faq", headers=boss, json={"question": "怎么报销", "answer": "贴发票"}
    )
    if first.status_code != 201:
        raise AssertionError(f"新建该 201,实际 {first.status_code}:{first.text[:200]}")
    created = first.json()
    if created["question"] != "怎么报销" or created["id"] <= 0:
        raise AssertionError(f"回执不对: {created}")
    dup = client.post(
        "/api/v1/admin/faq", headers=boss, json={"question": "  怎么报销 ", "answer": "别的"}
    )
    if dup.status_code != 409:
        raise AssertionError(f"同问该 409,实际 {dup.status_code}:{dup.text[:200]}")
    if str(created["id"]) not in dup.json()["detail"]:
        raise AssertionError(f"409 该点名已有 ID,实际 {dup.json()['detail']!r}")
    empty = client.post("/api/v1/admin/faq", headers=boss, json={"question": " ", "answer": "x"})
    if empty.status_code != 400:
        raise AssertionError(f"空问题该 400,实际 {empty.status_code}:{empty.text[:200]}")
    worker = client.post(
        "/api/v1/admin/faq", headers=_headers(client, "worker"), json={"question": "q", "answer": "a"}
    )
    if worker.status_code != 403:
        raise AssertionError(f"员工新建该 403,实际 {worker.status_code}")
    return f"201 id={created['id']};重复 409 点名;空 400;员工 403"


@check("接口 修改：200 改一条；不存在 404；改成别人的问法 409；空补丁 400")
def _api_update() -> str:
    store = FakeFaqStore(
        [
            _entry(1, "年假几天", "5 天"),
            _entry(2, "病假怎么请", "提交病假单"),
        ]
    )
    client, _ = _client(store)
    boss = _headers(client)
    ok = client.patch("/api/v1/admin/faq/1", headers=boss, json={"answer": "10 天"})
    if ok.status_code != 200 or ok.json()["answer"] != "10 天":
        raise AssertionError(f"改答案该 200 且回新值,实际 {ok.status_code}:{ok.text[:200]}")
    missing = client.patch("/api/v1/admin/faq/99", headers=boss, json={"answer": "x"})
    if missing.status_code != 404:
        raise AssertionError(f"ID 不存在该 404,实际 {missing.status_code}:{missing.text[:200]}")
    dup = client.patch("/api/v1/admin/faq/1", headers=boss, json={"question": "病假怎么请"})
    if dup.status_code != 409:
        raise AssertionError(f"改成别人的问法该 409,实际 {dup.status_code}:{dup.text[:200]}")
    empty = client.patch("/api/v1/admin/faq/1", headers=boss, json={})
    if empty.status_code != 400:
        raise AssertionError(f"空补丁该 400,实际 {empty.status_code}:{empty.text[:200]}")
    return "改答案 200;99 → 404;撞别人的问法 → 409;空补丁 → 400"


@check("接口 删除：200 deleted=1；重复删除 deleted=0（幂等）；员工 403")
def _api_delete() -> str:
    store = FakeFaqStore([_entry(1, "年会几号", "12 月 30 日")])
    client, _ = _client(store)
    boss = _headers(client)
    first = client.delete("/api/v1/admin/faq/1", headers=boss)
    if first.status_code != 200 or first.json() != {"entry_id": 1, "deleted": 1}:
        raise AssertionError(f"删除该 200 + deleted=1,实际 {first.status_code}:{first.text[:200]}")
    again = client.delete("/api/v1/admin/faq/1", headers=boss)
    if again.status_code != 200 or again.json()["deleted"] != 0:
        raise AssertionError(f"重复删除该 200 + deleted=0(幂等),实际 {again.status_code}")
    worker = client.delete(
        "/api/v1/admin/faq/1", headers=_headers(client, "worker")
    )
    if worker.status_code != 403:
        raise AssertionError(f"员工删除该 403,实际 {worker.status_code}")
    return "200/1;再删 200/0;员工 403"


@check("接口 降级：只读存储 → 503 并点名缺的方法；写操作抛错 → 500 带类型（不裸奔）")
def _api_degrade() -> str:
    read_only, _ = _client(_ReadOnlyStore())
    resp = read_only.get("/api/v1/admin/faq", headers=_headers(read_only))
    if resp.status_code != 503:
        raise AssertionError(f"只读存储该 503,实际 {resp.status_code}:{resp.text[:200]}")
    if "add" not in resp.json()["detail"]:
        raise AssertionError(f"503 该点名缺的方法,实际 {resp.json()['detail']!r}")
    broken, _ = _client(_BrokenStore())
    failed = broken.post(
        "/api/v1/admin/faq", headers=_headers(broken), json={"question": "q", "answer": "a"}
    )
    if failed.status_code != 500 or "RuntimeError" not in failed.json()["detail"]:
        raise AssertionError(f"写失败该 500 并带类型,实际 {failed.status_code}:{failed.text[:200]}")
    return "只读 → 503 点名;写失败 → 500 RuntimeError"


@check("路由：四条都在 /api/v1/admin/faq 下（业务路由的命名空间契约）")
def _routes() -> str:
    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    paths = {
        str(path)
        for route in create_app(Deps()).routes
        if (path := getattr(route, "path", "")).startswith("/api/v1/admin/faq")
    }
    want = {
        "/api/v1/admin/faq",
        "/api/v1/admin/faq/{entry_id}",
    }
    if not want.issubset(paths):
        raise AssertionError(f"缺路由: {sorted(want - paths)}")
    return "清单/新建 与 改/删 两组路径都在 /api/v1/admin/faq"


# ── 真 MySQL ────────────────────────────────────────────────────────────────
@check("真库：建表幂等 + 增改删往返；临时行清干净")
def _real_mysql() -> str:
    from project_rag.adapters.faq_mysql import MySQLFaqStore
    from project_rag.config import FaqConfig, MySQLConfig
    from pydantic import SecretStr

    conn = _mysql_connect()
    conn.close()
    store = MySQLFaqStore(
        MySQLConfig(
            host=MYSQL_ENV["host"],
            port=MYSQL_ENV["port"],
            user=MYSQL_ENV["user"],
            password=SecretStr(str(MYSQL_ENV["password"])),
            database=MYSQL_ENV["database"],
        ),
        FaqConfig(table="faq"),
    )
    store.ensure_table()
    before = len(store.load_all())
    created = store.add("验收临时问题-请忽略", "临时答案")
    try:
        if store.update(created.id, answer="改过的答案") is None:
            raise AssertionError("真库改一行回 None")
        if store.update(10**9, answer="x") is not None:
            raise AssertionError("不存在的 ID 该回 None")
        if len(store.load_all()) != before + 1:
            raise AssertionError("真库行数没 +1")
    finally:
        store.remove(created.id)
    if len(store.load_all()) != before:
        raise AssertionError("临时行没清干净")
    return f"round-trip 成功(原 {before} 行,临时行已删)"


# ── 前端 ────────────────────────────────────────────────────────────────────
@check("前端：/faq 路由要 manage 能力；导航只给管理员；页面调四个接口；弹层上限与后端一致")
def _frontend() -> str:
    router = _web("router.ts")
    if "path: '/faq'" not in router or "name: 'faq'" not in router:
        raise AssertionError("路由表里没有 /faq")
    if "needs: 'manage'" not in router.split("path: '/faq'", 1)[1].split("}", 1)[0]:
        raise AssertionError("/faq 没挂 manage 能力(员工不该看到入口)")
    header = _web("components/AppHeader.vue")
    nav = header.split('<nav', 1)[-1].split("</nav>", 1)[0]
    faq_link = [line for line in nav.splitlines() if "name: 'faq'" in line]
    if len(faq_link) != 1 or "isAdmin" not in faq_link[0]:
        raise AssertionError("导航里的 FAQ 入口不是管理员专属")
    view = _web("views/FaqView.vue")
    for call in ("refreshFaq", "deleteFaq", "FaqDialog"):
        if call not in view:
            raise AssertionError(f"FAQ 管理页没用到 {call}")
    dialog = _web("components/FaqDialog.vue")
    for limit in ('maxlength="512"', 'maxlength="8000"'):
        if limit not in dialog:
            raise AssertionError(f"弹层缺 {limit}(与后端上限同源)")
    endpoints = _web("api/endpoints.ts")
    for fn in ("faqEntries", "createFaq", "updateFaq", "deleteFaq"):
        if f"export function {fn}" not in endpoints:
            raise AssertionError(f"endpoints.ts 缺 {fn}")
    if endpoints.count("${API_PREFIX}/admin/faq") < 2:
        raise AssertionError("四条 FAQ 路径没走 API_PREFIX 命名空间")
    types = _web("api/types.ts")
    wanted = {
        "FaqEntry": ("id", "question", "answer"),
        "FaqListOut": ("entries", "total"),
    }
    for name, fields in wanted.items():
        block = types.split(f"export interface {name}", 1)
        if len(block) != 2:
            raise AssertionError(f"前端类型缺 {name}")
        body = block[1].split("}", 1)[0]
        for field in fields:
            if field not in body:
                raise AssertionError(f"{name} 缺 {field}(与后端出参同名才不容易漂)")
    sources = _web("views/FaqView.vue") + _web("components/FaqDialog.vue")
    for phrase in ("新建问答对", "编辑问答对", "删除后这个问题会交给知识库或模型兜底"):
        if phrase not in sources:
            raise AssertionError(f"界面缺一句该说清的话:{phrase}")
    return "路由 manage;导航管理员;四个接口;512/8000 上限;两条删除提示"


def main() -> int:
    return report("FAQ 管理台（增 / 改 / 删）")


if __name__ == "__main__":
    sys.exit(main())

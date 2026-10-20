#!/usr/bin/env python
"""契约:会话历史 —— 目录跨账号可列(管理员)、正文按归属读。

用法：

    uv run python scripts/acceptance/check_m5_10_history.py

**这一批补的是什么**:会话目录原来只按"我"过滤,管理员没有任何入口看"用户到底问了什么、
答得怎么样"—— 而答案质量、拒答、改写效果全藏在真实问答里。改成:

    GET /api/v1/sessions?scope=mine|all
        mine(默认) = 我名下的;all = 跨账号,每行带 owner(登录名)与 mine(是不是我的)
    GET /api/v1/history/{session_id}
        本人或**管理员**能读;**没有归属记录的会话谁都不放行**(内容可能不属于任何人)

本脚本固化的口径(改它们 = 改契约):

1. **`scope=all` 要管理员**:员工 403 —— 跨账号的目录本身就是一份"谁问过什么"的清单;
2. **正文那道门是 ``owner_violation``**:HTTP 与 WebSocket 共用一份,两边都测;
3. **没归属的会话不放行给任何人**(包括管理员):`owner_of` 返回 None 有两种可能 ——
   "加认证前的老会话"与"瞎编的 ID",两者都不该读到内容;
4. **只列有轮次的会话**:刚建没问过的不占地方(与侧栏同一条口径);
5. **账号被删,会话不消失**:适配器用 LEFT JOIN users,`owner` 给空串 ——
   用 INNER JOIN 会让"删一个人"顺手删掉他的历史可见性,而数据还在库里。

前半段用内存假件;适配器那两条读源码 + 真 MySQL(连不上就 SKIP);前端是静态契约。
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from _fakes import FakeConversationStore
from _harness import SkipCheck, check, report

if TYPE_CHECKING:  # 只为注解;运行时的导入留在函数里
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
    """最小认证存储:登录、令牌、会话归属。"""

    def __init__(self) -> None:
        self.users: dict[str, tuple[Any, str]] = {}
        self.tokens: dict[str, Any] = {}
        self.owners: dict[str, str] = {}

    def init_schema(self) -> None:
        return None

    def create_user(self, username: str, password_hash: str, role: UserRole):
        from project_rag.domain.auth import User

        user = User(user_id=f"u{len(self.users) + 1}", username=username, role=role)
        self.users[username] = (user, password_hash)
        return user

    def find_by_username(self, username: str):
        return self.users.get(username)

    def set_password(self, user_id: str, password_hash: str) -> None:
        return None

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


class _Catalog:
    """假会话目录:``list_for`` 只给自己的,``list_all`` 给全部并带上归属名。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows: list[dict[str, Any]] = [
            {
                "session_id": "s-worker",
                "title": "怎么报销",
                "rounds": 2,
                "created_at": "2026-10-19T10:00:00",
                "updated_at": "2026-10-19T10:05:00",
                "owner_id": "u2",
                "owner": "worker",
            },
            {
                "session_id": "s-boss",
                "title": "报表怎么导",
                "rounds": 1,
                "created_at": "2026-10-19T09:00:00",
                "updated_at": "2026-10-19T09:01:00",
                "owner_id": "u1",
                "owner": "boss",
            },
            {
                "session_id": "s-gone",
                "title": "前同事问的",
                "rounds": 3,
                "created_at": "2026-10-18T09:00:00",
                "updated_at": "2026-10-18T09:30:00",
                "owner_id": "u9",
                "owner": "",
            },
        ]

    def list_for(self, user_id: str, *, limit: int):
        from project_rag.domain.sessions import SessionSummary

        self.calls.append(("for", user_id))
        return tuple(
            SessionSummary(
                session_id=row["session_id"],
                title=row["title"],
                rounds=row["rounds"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in self.rows
            if row["owner_id"] == user_id
        )[:limit]

    def list_all(self, *, limit: int):
        from project_rag.domain.sessions import SessionSummary

        self.calls.append(("all", limit))
        return tuple(
            SessionSummary(
                session_id=row["session_id"],
                title=row["title"],
                rounds=row["rounds"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                owner=row["owner"],
            )
            for row in self.rows
        )[:limit]


# ── 夹具 ─────────────────────────────────────────────────────────────────────
def _client(catalog: _Catalog | None = None):
    """输入:假目录(可省)。行为:建注入了依赖的 TestClient。输出:(client, catalog, auth)。"""
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps

    auth = _FakeAuthStore()
    auth.create_user("boss", hash_password("pw-123456"), "admin")
    auth.create_user("worker", hash_password("pw-123456"), "employee")
    # 归属绑定(生产上由 create_session 做):s-worker 是 worker 的,s-boss 是 boss 的,
    # s-gone 的账号已删除 —— 它没有归属行,正好验"没归属谁都不放行"。
    auth.owners["s-worker"] = "u2"
    auth.owners["s-boss"] = "u1"
    catalog = _Catalog() if catalog is None else catalog
    turns = {
        "s-worker": [
            _turn("怎么报销", "贴发票走财务。", "财务手册.md"),
            _turn("多久到账", "3 个工作日。", "财务手册.md"),
        ],
        "s-boss": [_turn("报表怎么导", "点右上角导出。", "报表说明.md")],
    }
    deps = Deps(
        auth_store_factory=lambda: auth,
        session_catalog_factory=lambda: catalog,
        conversation_store_factory=lambda: FakeConversationStore(turns),
    )
    return TestClient(create_app(deps)), catalog, auth


def _turn(question: str, answer: str, source: str):
    from project_rag.domain import CitationSnapshot, ConversationTurn

    return ConversationTurn(
        question=question,
        answer=answer,
        citations=(CitationSnapshot(index=1, source=source, doc_id=source, preview="…", score=0.9),),
    )


def _headers(client, username: str = "boss") -> dict[str, str]:
    """输入:客户端、用户名。行为:登录。输出:鉴权头。"""
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-123456"}
    )
    if resp.status_code != 200:
        raise AssertionError(f"{username} 登录该 200,实际 {resp.status_code}:{resp.text[:200]}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _web(relative: str) -> str:
    """输入:相对 web/src 的路径。行为:读它。输出:正文(不在就报错)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— 会话历史页还没落地?")
    return path.read_text(encoding="utf-8")


def _mysql_connect():
    """输入:无。行为:连真 MySQL。输出:连接。连不上抛 SkipCheck。"""
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
    except Exception as exc:  # noqa: BLE001 - 环境不在是 SKIP
        raise SkipCheck(
            "MySQL",
            f"连不上 {MYSQL_ENV['host']}:{MYSQL_ENV['port']}({type(exc).__name__});"
            "真库那条留给 services 档,owner = 部署环境",
        ) from exc


# ── 端口与领域 ───────────────────────────────────────────────────────────────
@check("端口：SessionCatalog 有 list_for + list_all；SessionSummary 的 owner 默认空串（老调用不受影响）")
def _ports() -> str:
    from project_rag.domain.sessions import SessionSummary
    from project_rag.ports import SessionCatalog

    if not getattr(SessionCatalog, "_is_protocol", False):
        raise AssertionError("SessionCatalog 不是 Protocol")
    signatures = {
        "list_for": ("user_id", "limit"),
        "list_all": ("limit",),
    }
    for name, want in signatures.items():
        fn = getattr(SessionCatalog, name, None)
        if fn is None:
            raise AssertionError(f"SessionCatalog 缺 {name}")
        got = tuple(p for p in inspect.signature(fn).parameters if p != "self")
        if got != want:
            raise AssertionError(f"{name} 参数应为 {want},实际 {got}")
    summary = SessionSummary(
        session_id="s", title="t", rounds=1, created_at="a", updated_at="b"
    )
    if summary.owner != "":
        raise AssertionError(f"owner 默认值该是空串(没查就是没查),实际 {summary.owner!r}")
    return "list_for(user_id, limit) / list_all(limit);owner 默认空串"


@check("适配器：list_all 用 LEFT JOIN users（删号不吞历史），且仍只列有轮次的会话")
def _adapter() -> str:
    source = (ROOT / "src/project_rag/adapters/sessions_mysql.py").read_text(encoding="utf-8")
    block = source.split("def list_all", 1)[1].split("def _titles", 1)[0]
    if 'f" LEFT JOIN {self._users} u' not in block or "u.username" not in block:
        raise AssertionError("list_all 没 LEFT JOIN 账号表/没取登录名")
    if "o JOIN {self._table} c" not in block:
        raise AssertionError("list_all 没 JOIN 会话表 —— 那会把 0 轮的会话也列出来")
    if "GROUP BY o.session_id, o.created_at, u.username" not in block:
        raise AssertionError("list_all 的 GROUP BY 与 SELECT 对不上(MySQL only_full_group_by 会炸)")
    if "LIMIT %s" not in block:
        raise AssertionError("list_all 没有 LIMIT(会话表会无限长)")
    return "LEFT JOIN users + JOIN conversations + 分组齐全 + LIMIT"


# ── 接口 ────────────────────────────────────────────────────────────────────
@check("接口 目录：默认 mine 只列自己的；scope=all 员工 403、管理员 200（带 owner 与 mine）")
def _api_sessions() -> str:
    client, catalog, _ = _client()
    worker = _headers(client, "worker")
    mine = client.get("/api/v1/sessions", headers=worker)
    if mine.status_code != 200:
        raise AssertionError(f"员工列自己的会话该 200,实际 {mine.status_code}:{mine.text[:200]}")
    body = mine.json()
    if body["scope"] != "mine" or [row["session_id"] for row in body["sessions"]] != ["s-worker"]:
        raise AssertionError(f"默认该只给自己的会话,实际 {body}")
    if body["sessions"][0]["mine"] is not True:
        raise AssertionError("自己的会话 mine 该为 true")
    if client.get("/api/v1/sessions?scope=all", headers=worker).status_code != 403:
        raise AssertionError("员工要 all 该 403")
    boss = _headers(client)
    everything = client.get("/api/v1/sessions?scope=all", headers=boss)
    if everything.status_code != 200:
        raise AssertionError(f"管理员要 all 该 200,实际 {everything.status_code}:{everything.text[:200]}")
    if everything.json()["scope"] != "all":
        raise AssertionError(f"scope=all 该原样回显,实际 {everything.json()['scope']!r}")
    rows = {row["session_id"]: row for row in everything.json()["sessions"]}
    if set(rows) != {"s-worker", "s-boss", "s-gone"}:
        raise AssertionError(f"管理员该看到全部三段的目录,实际 {sorted(rows)}")
    if rows["s-worker"]["owner"] != "worker" or rows["s-worker"]["mine"] is not False:
        raise AssertionError(f"别人的会话该带 owner 且 mine=false,实际 {rows['s-worker']}")
    if rows["s-boss"]["mine"] is not True:
        raise AssertionError(f"自己的会话 mine 该为 true,实际 {rows['s-boss']}")
    if rows["s-gone"]["owner"] != "":
        raise AssertionError("账号已删除的会话 owner 该是空串(不编占位名)")
    return "mine 默认只给自己;all 员工 403 / 管理员三段带 owner;删号不留空白占位名"


@check("接口 正文：管理员能读别人的会话（引用一起回来）；员工读别人的 403")
def _api_history() -> str:
    client, _, _ = _client()
    worker_token = _headers(client, "worker")["Authorization"].split(" ", 1)[1]
    boss = _headers(client)
    own = client.get("/api/v1/history/s-worker", headers=_headers(client, "worker"))
    if own.status_code != 200 or len(own.json()["rounds"]) != 2:
        raise AssertionError(f"自己的两轮该读得回来,实际 {own.status_code}:{own.text[:200]}")
    if own.json()["rounds"][0]["citations"] == []:
        raise AssertionError("历史里该带当轮的引用快照")
    admin = client.get("/api/v1/history/s-worker", headers=boss)
    if admin.status_code != 200:
        raise AssertionError(f"管理员读别人的会话该 200,实际 {admin.status_code}:{admin.text[:200]}")
    if admin.json()["session_id"] != "s-worker":
        raise AssertionError(f"读错了会话:{admin.json()}")
    forbidden = client.get("/api/v1/history/s-boss", headers={"Authorization": f"Bearer {worker_token}"})
    if forbidden.status_code != 403:
        raise AssertionError(f"员工读别人的会话该 403,实际 {forbidden.status_code}")
    orphan = client.get("/api/v1/history/从没绑过的ID", headers=boss)
    if orphan.status_code != 403:
        raise AssertionError(f"没归属的会话管理员也不该读,实际 {orphan.status_code}")
    return "本人两轮带引用;管理员读别人的 200;员工 403;没归属的 403"


@check("接口 降级：没接会话目录时 503 并点名，不是空表")
def _api_missing_catalog() -> str:
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps

    auth = _FakeAuthStore()
    auth.create_user("boss", hash_password("pw-123456"), "admin")
    bare = TestClient(
        create_app(
            Deps(
                auth_store_factory=lambda: auth,
                conversation_store_factory=lambda: FakeConversationStore([]),
            )
        )
    )
    boss = _headers(bare)
    for path in ("/api/v1/sessions", "/api/v1/sessions?scope=all"):
        resp = bare.get(path, headers=boss)
        if resp.status_code != 503:
            raise AssertionError(f"{path} 该 503,实际 {resp.status_code}:{resp.text[:200]}")
        if "session_catalog_factory" not in resp.json()["detail"]:
            raise AssertionError(f"503 该点名缺的东西,实际 {resp.json()['detail']!r}")
    return "两条都 503,且点名 session_catalog_factory"


@check("前端：/history 要 knowledge 能力；管理员能切 mine/all；正文按轮次渲染并带出处")
def _frontend() -> str:
    router = _web("router.ts")
    block = router.split("path: '/history", 1)
    if len(block) != 2 or "needs: 'knowledge'" not in block[1].split("}", 1)[0]:
        raise AssertionError("/history 路由没挂 knowledge 能力(员工本来就能读自己的历史)")
    header = _web("components/AppHeader.vue")
    nav = header.split('<nav', 1)[-1].split("</nav>", 1)[0]
    if "name: 'history'" not in nav:
        raise AssertionError("导航里没有「历史」入口")
    view = _web("views/HistoryView.vue")
    for call in ("loadSessions", "loadDetail", "clearSession", "openSession"):
        if call not in view:
            raise AssertionError(f"历史页没用到 {call}")
    for phrase in ("全部", "我的", "出处", "不可恢复"):
        if phrase not in view:
            raise AssertionError(f"历史页缺一句该说清的话:{phrase}")
    store = _web("stores/history.ts")
    if "api.sessions(100, scope)" not in store:
        raise AssertionError("切 mine/all 时没有按 scope 重新要目录")
    endpoints = _web("api/endpoints.ts")
    if "scope=${scope}" not in endpoints:
        raise AssertionError("endpoints 里 sessions() 没把 scope 传下去")
    types = _web("api/types.ts")
    for field in ("owner: string", "mine: boolean"):
        if field not in types:
            raise AssertionError(f"前端类型缺 {field}(与后端出参同名才不容易漂)")
    return "路由 knowledge;导航有入口;页面四个动作;mine/all 切换;出处与删除提示都在"


# ── 真 MySQL ────────────────────────────────────────────────────────────────
@check("真库：list_all 能读出归属名，且只含有轮次的会话")
def _real_mysql() -> str:
    from project_rag.adapters.sessions_mysql import MySQLSessionCatalog
    from project_rag.config import ConversationConfig, MySQLConfig
    from pydantic import SecretStr

    conn = _mysql_connect()
    conn.close()
    catalog = MySQLSessionCatalog(
        MySQLConfig(
            host=MYSQL_ENV["host"],
            port=MYSQL_ENV["port"],
            user=MYSQL_ENV["user"],
            password=SecretStr(str(MYSQL_ENV["password"])),
            database=MYSQL_ENV["database"],
        ),
        ConversationConfig(),
    )
    rows = catalog.list_all(limit=20)
    if not rows:
        raise SkipCheck("真实语料", "会话表里没有带轮次的数据 —— owner = 数据准备")
    if any(row.rounds <= 0 for row in rows):
        raise AssertionError("list_all 列出了一段 0 轮的会话")
    named = [row for row in rows if row.owner]
    if not named:
        raise AssertionError("list_all 一条归属名都没读出来(LEFT JOIN 的账号表接错?)")
    if named[0].updated_at < rows[-1].updated_at:
        raise AssertionError("顺序该是最近活动在前")
    return f"{len(rows)} 段(其中 {len(named)} 段有归属名),全部有轮次,最近在前"


def main() -> int:
    return report("会话历史（跨账号目录 + 归属读取）")


if __name__ == "__main__":
    sys.exit(main())

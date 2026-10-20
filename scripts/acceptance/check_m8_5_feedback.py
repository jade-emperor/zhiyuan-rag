#!/usr/bin/env python
"""契约:答案反馈 —— 「有帮助 / 没帮助」的留痕与看板。

用法：

    uv run python scripts/acceptance/check_m8_5_feedback.py

**这一批补的是什么**:回答下方的两个按钮此前只改本机状态(`setFeedback` 写在 store 里),
刷新即丢、管理员也看不到。现在:

    POST /api/v1/feedback            {session_id, question, answer, rating, channel?, comment?}
        rating = up / down / none(none = 撤销自己那一条)
    GET  /api/v1/reports/feedback?limit=&rating=&days=
        -> {summary:{up,down,total,negative_rate}, entries:[…], days}

本脚本固化的口径(改它们 = 改契约):

1. **快照随反馈一起存**:运维台看到的是用户点按钮那一刻的问题/答案/通道,会话后来被清空
   也不影响复盘;只存计数的话,"被踩的那条答了什么"永远查不回来;
2. **``turn_key`` 唯一**:同一个人对同一轮重复点(乐观 UI 会重发)是一次**覆盖写**,
   计数不被重复点击放大;键由 (会话, 问题, 答案) 算,只算一处;
3. **撤销只删自己的**:``remove`` 带 ``user_id``,别人的反馈删不掉;
4. **看板的比率带分子分母**:``negative_rate`` 在一条都没有时是 ``None``(不是 0);
   ``days=0`` = 全部历史(不拿 0 当"今天");
5. **权限**:提交 = 能问答的人 + 会话归属;看板 = 运维/管理员(与两份报表同一条线)。

前半段用内存假件;适配器读源码 + 真 MySQL(连不上就 SKIP);前端是静态契约。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
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


class _FakeFeedbackStore:
    """内存反馈表：按 ``turn_key`` 覆盖写，``remove`` 只删自己的。"""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.last_since: datetime | None = None
        self._next = 1

    def upsert(self, *, turn_key, session_id, user_id, rating, question, answer, channel, comment):
        from project_rag.domain import FeedbackRecord

        self.calls.append(("upsert", turn_key, user_id, rating))
        stamp = "2026-10-19T10:00:00"
        record = FeedbackRecord(
            id=self.rows[turn_key].id if turn_key in self.rows else self._next,
            session_id=session_id,
            user_id=user_id,
            username="" if user_id == "" else f"user-{user_id}",
            rating=rating,
            question=question,
            answer=answer,
            channel=channel,
            comment=comment,
            created_at=stamp,
            updated_at=stamp,
        )
        if turn_key not in self.rows:
            self._next += 1
        self.rows[turn_key] = record
        return record

    def remove(self, turn_key: str, user_id: str) -> int:
        self.calls.append(("remove", turn_key, user_id))
        record = self.rows.get(turn_key)
        if record is None or record.user_id != user_id:
            return 0
        del self.rows[turn_key]
        return 1

    def list_recent(self, *, limit, rating=None, since=None):
        self.calls.append(("list", limit, rating, since))
        self.last_since = since
        rows = [row for row in self.rows.values() if rating is None or row.rating == rating]
        return tuple(sorted(rows, key=lambda row: row.id, reverse=True))[:limit]

    def counts(self, *, since=None):
        self.calls.append(("counts", since))
        self.last_since = since
        up = sum(1 for row in self.rows.values() if row.rating == "up")
        down = sum(1 for row in self.rows.values() if row.rating == "down")
        return up, down


# ── 夹具 ─────────────────────────────────────────────────────────────────────
def _client(store: _FakeFeedbackStore | None = None):
    """输入:假反馈存储(可省)。输出:(client, store, auth)。"""
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps

    auth = _FakeAuthStore()
    auth.create_user("boss", hash_password("pw-123456"), "admin")
    auth.create_user("worker", hash_password("pw-123456"), "employee")
    auth.create_user("other", hash_password("pw-123456"), "employee")
    auth.create_user("watcher", hash_password("pw-123456"), "ops")
    auth.owners["s-1"] = "u2"  # worker 的会话
    store = _FakeFeedbackStore() if store is None else store
    deps = Deps(
        auth_store_factory=lambda: auth,
        conversation_store_factory=lambda: FakeConversationStore([]),
        feedback_store_factory=lambda: store,
    )
    return TestClient(create_app(deps)), store, auth


def _client_without_store():
    """输入:无。行为:装配一个**没有**反馈存储的应用。输出:client。

    与 ``_client`` 分开写:用一个 ``False`` 哨兵会让整份脚本的 ``store`` 类型变成三选一,
    pyright 一路报"calls 不在 Literal[False] 上"—— 检查脚本自己先要读得过去。
    """
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps

    auth = _FakeAuthStore()
    auth.create_user("worker", hash_password("pw-123456"), "employee")
    auth.owners["s-1"] = "u1"
    deps = Deps(
        auth_store_factory=lambda: auth,
        conversation_store_factory=lambda: FakeConversationStore([]),
    )
    return TestClient(create_app(deps))


def _headers(client, username: str = "worker") -> dict[str, str]:
    """输入:客户端、用户名。行为:登录。输出:鉴权头。"""
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "pw-123456"}
    )
    if resp.status_code != 200:
        raise AssertionError(f"{username} 登录该 200,实际 {resp.status_code}:{resp.text[:200]}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _payload(**overrides: Any) -> dict[str, Any]:
    """输入:要覆盖的字段。行为:造一份合法的提交体。输出:字典。"""
    body: dict[str, Any] = {
        "session_id": "s-1",
        "question": "怎么报销",
        "answer": "贴发票走财务。",
        "rating": "up",
        "channel": "rag",
        "comment": "",
    }
    body.update(overrides)
    return body


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _web(relative: str) -> str:
    """输入:相对 web/src 的路径。行为:读它。输出:正文(不在就报错)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— 反馈那条链路还没接上?")
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


# ── 领域与用例 ───────────────────────────────────────────────────────────────
@check("领域：评级闭集；记录校验；负反馈率在没样本时是 None（不是 0）")
def _domain() -> str:
    from project_rag.domain import FEEDBACK_RATINGS, FeedbackCounts, FeedbackRecord

    if FEEDBACK_RATINGS != ("up", "down"):
        raise AssertionError(f"评级闭集该是 up/down,实际 {FEEDBACK_RATINGS}")
    for bad in ("", "UP", "helpful"):
        try:
            FeedbackRecord(
                id=1,
                session_id="s-1",
                user_id="u1",
                username="worker",
                rating=bad,
                question="q",
                answer="a",
                channel="",
                comment="",
                created_at="t",
                updated_at="t",
            )
            raise AssertionError(f"评级 {bad!r} 该在构造时被拒")
        except ValueError:
            pass
    empty = FeedbackCounts()
    if empty.negative_rate is not None or empty.total != 0:
        raise AssertionError(f"一条都没有时比率该是 None,实际 {empty.negative_rate!r}")
    if abs((FeedbackCounts(up=1, down=3).negative_rate or 0) - 0.75) > 1e-9:
        raise AssertionError("1/4 的负反馈率该是 0.75")
    return "up/down 闭集;坏评级构造即拒;空样本 None;1/4 = 75%"


@check("用例：turn_key 稳定且只算一处；submit 清洗快照；非法输入 → FeedbackError；撤销只删自己的")
def _application() -> str:
    from project_rag.application.feedback import (
        MAX_COMMENT_CHARS,
        FeedbackError,
        submit,
        turn_key,
        withdraw,
    )

    store = _FakeFeedbackStore()
    key = turn_key("s-1", " 怎么报销 ", " 贴发票走财务。 ")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise AssertionError(f"turn_key 该是 64 位十六进制,实际 {key!r}")
    if key != turn_key("s-1", "怎么报销", "贴发票走财务。"):
        raise AssertionError("首尾空白不该改变 turn_key(否则同一轮会存两条)")
    if key == turn_key("s-1", "怎么报销?", "贴发票走财务。"):
        raise AssertionError("问题不同该是不同的键")

    record = submit(
        store,
        session_id="s-1",
        user_id="u2",
        rating="up",
        question="  怎么报销  ",
        answer="  贴发票走财务。  ",
        channel="rag",
        comment="  谢谢  ",
    )
    if record.question != "怎么报销" or record.answer != "贴发票走财务。" or record.comment != "谢谢":
        raise AssertionError(f"快照没清洗:{record!r}")
    again = submit(
        store,
        session_id="s-1",
        user_id="u2",
        rating="down",
        question="怎么报销",
        answer="贴发票走财务。",
    )
    if again.id != record.id or len(store.rows) != 1:
        raise AssertionError("同一轮重复提交该是覆盖写(1 行、同 id),不是新增")
    if again.rating != "down":
        raise AssertionError("覆盖写该改掉评级")

    for bad in (
        {"rating": "meh"},
        {"comment": "字" * (MAX_COMMENT_CHARS + 1)},
        {"question": "   "},
        {"answer": ""},
    ):
        try:
            submit(
                store,
                session_id="s-1",
                user_id="u2",
                rating=bad.get("rating", "up"),
                question=bad.get("question", "怎么报销"),
                answer=bad.get("answer", "贴发票走财务。"),
                comment=bad.get("comment", ""),
            )
            raise AssertionError(f"{bad!r} 该被拒")
        except FeedbackError:
            pass

    if withdraw(store, session_id="s-1", user_id="u3", question="怎么报销", answer="贴发票走财务。") != 0:
        raise AssertionError("别人删得掉这条反馈 —— remove 没带 user_id")
    if withdraw(store, session_id="s-1", user_id="u2", question="怎么报销", answer="贴发票走财务。") != 1:
        raise AssertionError("本人撤销该删掉 1 条")
    return "64 位键、空白无关;覆盖写 1 行;四类非法输入都拒;撤销只删自己的"


@check("适配器：turn_key 唯一 + 快照列 + ON DUPLICATE KEY + remove 带 user_id + LEFT JOIN 账号")
def _adapter() -> str:
    source = (ROOT / "src/project_rag/adapters/feedback_mysql.py").read_text(encoding="utf-8")
    for needle, why in (
        ("turn_key CHAR(64)", "没有 turn_key 列"),
        ("UNIQUE KEY uk_turn_key (turn_key)", "没有唯一键 —— 重复点会多出几行"),
        ("question TEXT NOT NULL", "没有存问题快照"),
        ("answer MEDIUMTEXT NOT NULL", "答案快照列不对(20000 字放不进 TEXT)"),
        ("ON DUPLICATE KEY UPDATE rating = VALUES(rating)", "不是覆盖写"),
        ("WHERE turn_key = %s AND user_id = %s", "撤销没带 user_id"),
        ("LEFT JOIN users u ON u.user_id = f.user_id", "账号被删后反馈会整条消失"),
        ("TABLE_OPTIONS", "建表尾巴没走 mysql_schema(漏 COLLATE 会掉进 1267)"),
    ):
        if needle not in source:
            raise AssertionError(f"{why}(缺 {needle!r})")
    return "唯一键 + 两列快照 + 覆盖写 + 按人删 + LEFT JOIN + utf8mb4 口径"


# ── HTTP ────────────────────────────────────────────────────────────────────
@check("接口 提交：up 200；同一轮再点不新增；改 down 覆盖；none 撤销；非自己会话 403")
def _api_submit() -> str:
    client, store, _ = _client()
    headers = _headers(client)
    first = client.post("/api/v1/feedback", headers=headers, json=_payload())
    if first.status_code != 200 or first.json()["rating"] != "up":
        raise AssertionError(f"提交该 200 + rating=up,实际 {first.status_code}:{first.text[:200]}")
    second = client.post("/api/v1/feedback", headers=headers, json=_payload())
    if second.status_code != 200 or second.json()["id"] != first.json()["id"]:
        raise AssertionError(f"同一轮重复提交该覆盖(同 id),实际 {second.json()}")
    if len(store.rows) != 1:
        raise AssertionError(f"重复提交该只有 1 行,实际 {len(store.rows)}")
    down = client.post("/api/v1/feedback", headers=headers, json=_payload(rating="down"))
    if down.status_code != 200 or len(store.rows) != 1:
        raise AssertionError("改成 down 该覆盖同一行")
    out = client.post(
        "/api/v1/feedback", headers=headers, json=_payload(rating="none")
    )
    if out.status_code != 200 or out.json()["deleted"] != 1:
        raise AssertionError(f"撤销该回 deleted=1,实际 {out.status_code}:{out.text[:200]}")
    if store.rows:
        raise AssertionError("撤销之后库里不该还有这一条")
    other = client.post(
        "/api/v1/feedback", headers=_headers(client, "other"), json=_payload()
    )
    if other.status_code != 403:
        raise AssertionError(f"给别人的会话打分该 403,实际 {other.status_code}")
    if client.post("/api/v1/feedback", json=_payload()).status_code != 401:
        raise AssertionError("无令牌该 401")
    return "up 200;重复同 id 且 1 行;改 down 覆盖;none 删 1 条;别人的会话 403;无令牌 401"


@check("接口 提交：非法评级 / 超长备注 / 空快照 → 400；没装配反馈存储 → 503 点名")
def _api_validation() -> str:
    client, _, _ = _client()
    headers = _headers(client)
    for body, why in (
        (_payload(rating="meh"), "非法评级"),
        (_payload(comment="字" * 201), "超长备注"),
        (_payload(question="  "), "空问题"),
    ):
        resp = client.post("/api/v1/feedback", headers=headers, json=body)
        if resp.status_code not in (400, 422):
            raise AssertionError(f"{why} 该 400/422,实际 {resp.status_code}:{resp.text[:200]}")
        if resp.status_code == 400 and not resp.json()["detail"]:
            raise AssertionError(f"{why} 的 400 没给原因")
    bare = _client_without_store()
    missing = bare.post("/api/v1/feedback", headers=_headers(bare, "worker"), json=_payload())
    if missing.status_code != 503 or "feedback_store_factory" not in missing.json()["detail"]:
        raise AssertionError(f"没装配存储该 503 点名,实际 {missing.status_code}:{missing.text[:200]}")
    return "三类非法输入 400/422;没装配 → 503 点名"


@check("接口 看板：员工 403、运维/管理员 200；summary 带分子分母；rating=down 只回没帮助；days=0 不设窗口")
def _api_report() -> str:
    client, store, _ = _client()
    from project_rag.application.feedback import submit

    submit(store, session_id="s-1", user_id="u2", rating="down", question="q1", answer="a1")
    submit(store, session_id="s-1", user_id="u2", rating="down", question="q2", answer="a2")
    submit(store, session_id="s-2", user_id="u3", rating="up", question="q3", answer="a3")

    if client.get("/api/v1/reports/feedback", headers=_headers(client, "worker")).status_code != 403:
        raise AssertionError("员工看反馈看板该 403")
    for who in ("watcher", "boss"):
        body = client.get("/api/v1/reports/feedback", headers=_headers(client, who))
        if body.status_code != 200:
            raise AssertionError(f"{who} 该能看反馈看板,实际 {body.status_code}:{body.text[:200]}")
        summary = body.json()["summary"]
        if summary["up"] != 1 or summary["down"] != 2 or summary["total"] != 3:
            raise AssertionError(f"{who} 的计数不对:{summary}")
        if abs((summary["negative_rate"] or 0) - 2 / 3) > 1e-6:
            raise AssertionError(f"负反馈率该是 2/3,实际 {summary['negative_rate']}")
    only_down = client.get(
        "/api/v1/reports/feedback?rating=down", headers=_headers(client, "boss")
    ).json()
    if [entry["rating"] for entry in only_down["entries"]] != ["down", "down"]:
        raise AssertionError(f"rating=down 该只回没帮助的,实际 {only_down['entries']}")
    store.calls.clear()
    client.get("/api/v1/reports/feedback?days=30", headers=_headers(client, "boss"))
    if not any(call[0] == "counts" and call[1] is not None for call in store.calls):
        raise AssertionError("days=30 没有把窗口起点传下去(counts 收到的是 None)")
    store.calls.clear()
    client.get("/api/v1/reports/feedback?days=0", headers=_headers(client, "boss"))
    if not any(call[0] == "counts" and call[1] is None for call in store.calls):
        raise AssertionError("days=0 该表示全部历史(不设窗口)")
    bad = client.get("/api/v1/reports/feedback?rating=meh", headers=_headers(client, "boss"))
    if bad.status_code != 400:
        raise AssertionError(f"非法 rating 该 400,实际 {bad.status_code}:{bad.text[:200]}")
    # 一条反馈都没有时,比率该是 null(不是 0):没有样本的比率不是"0%",是"没法算"
    empty, _, _ = _client()
    blank = empty.get("/api/v1/reports/feedback", headers=_headers(empty, "boss")).json()
    if blank["summary"]["total"] != 0 or blank["summary"]["negative_rate"] is not None:
        raise AssertionError(f"空看板该 total=0 + rate=null,实际 {blank['summary']}")
    return "员工 403 / 运维与管理员 200;1/3 与 2/3 计数对;只看没帮助;窗口 0 = 全部;空看板 rate=null"


# ── 前端 ────────────────────────────────────────────────────────────────────
@check("前端：两个按钮落到后端（带问答快照）；/feedback 要 reports 能力；看板显示快照与备注")
def _frontend() -> str:
    actions = _web("components/MessageActions.vue")
    if "feedbackBusy" not in actions:
        raise AssertionError("反馈按钮没有置灰态(连点会来回覆盖)")
    store = _web("stores/chat.ts")
    for needle, why in (
        ("api.submitFeedback", "按钮没有落到后端(还是本地状态)"),
        ("question: question.text", "提交里没有带用户当时问的那句"),
        ("answer: message.text", "提交里没有带这一轮的答案"),
        ("rating: next ?? 'none'", "再点同一个按钮没有走撤销"),
    ):
        if needle not in store:
            raise AssertionError(why)
    router = _web("router.ts")
    block = router.split("path: '/feedback'", 1)
    if len(block) != 2 or "needs: 'reports'" not in block[1].split("}", 1)[0]:
        raise AssertionError("/feedback 路由没挂 reports 能力(看板是运维与管理员那一档)")
    header = _web("components/AppHeader.vue")
    nav = header.split('<nav', 1)[-1].split("</nav>", 1)[0]
    feedback_link = [line for line in nav.splitlines() if "name: 'feedback'" in line]
    if len(feedback_link) != 1 or "canViewReports" not in feedback_link[0]:
        raise AssertionError("导航里的「反馈」入口不是报表能力那一档")
    view = _web("views/FeedbackView.vue")
    for phrase in ("负反馈率", "仅没帮助", "看这段会话", "账号已删除"):
        if phrase not in view:
            raise AssertionError(f"看板缺一句该说清的话:{phrase}")
    types = _web("api/types.ts")
    block = types.split("export interface FeedbackEntry", 1)
    if len(block) != 2:
        raise AssertionError("前端类型缺 FeedbackEntry")
    for field in ("question", "answer", "comment", "username", "session_id"):
        if field not in block[1].split("}", 1)[0]:
            raise AssertionError(f"FeedbackEntry 缺 {field}")
    return "按钮带置灰态;提交带问答快照与撤销;路由/导航按报表能力;看板有比率/筛选/跳会话"


# ── 真 MySQL ────────────────────────────────────────────────────────────────
@check("真库：建表幂等 + 覆盖写只有一行 + 只删自己 + 计数 + 清理")
def _real_mysql() -> str:
    from pydantic import SecretStr

    from project_rag.adapters.feedback_mysql import MySQLFeedbackStore
    from project_rag.config import MySQLConfig

    conn = _mysql_connect()
    conn.close()
    store = MySQLFeedbackStore(
        MySQLConfig(
            host=MYSQL_ENV["host"],
            port=MYSQL_ENV["port"],
            user=MYSQL_ENV["user"],
            password=SecretStr(str(MYSQL_ENV["password"])),
            database=MYSQL_ENV["database"],
        )
    )
    store.ensure_table()
    key = f"check-feedback-{os.getpid()}"
    try:
        first = store.upsert(
            turn_key=key,
            session_id="s-check",
            user_id="u-check",
            rating="up",
            question="验收临时问题",
            answer="验收临时答案",
            channel="rag",
            comment="",
        )
        again = store.upsert(
            turn_key=key,
            session_id="s-check",
            user_id="u-check",
            rating="down",
            question="验收临时问题",
            answer="验收临时答案",
            channel="rag",
            comment="改一下",
        )
        if again.id != first.id:
            raise AssertionError("真库重复写该覆盖同一行(自增 ID 变了说明插了两行)")
        if store.remove(key, "someone-else") != 0:
            raise AssertionError("删除没带 user_id —— 别人删得掉")
        rows = [row for row in store.list_recent(limit=10) if row.session_id == "s-check"]
        if len(rows) != 1 or rows[0].rating != "down" or rows[0].comment != "改一下":
            raise AssertionError(f"真库读回来不对:{rows}")
        up, down = store.counts()
        if up < 0 or down < 1:
            raise AssertionError(f"计数不对:up={up} down={down}")
    finally:
        store.remove(key, "u-check")
    if any(row.session_id == "s-check" for row in store.list_recent(limit=50)):
        raise AssertionError("临时反馈没清干净")
    return "覆盖写同 id;别人删不掉;读回快照与备注;计数正常;临时行已清"


def main() -> int:
    return report("答案反馈（留痕 + 看板）")


if __name__ == "__main__":
    sys.exit(main())

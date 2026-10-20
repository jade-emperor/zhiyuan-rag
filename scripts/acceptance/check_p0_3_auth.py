#!/usr/bin/env python
"""契约：认证、角色、会话归属。

用法：

    uv run python scripts/acceptance/check_p0_3_auth.py

**这一份在堵什么洞**：在这之前**会话 ID 就是凭证** —— 猜到/看到别人的 ID 就能读到他的
全部对话（``GET /api/v1/history/{id}``），也能拿他的历史继续提问。现在:登录换令牌、
会话在创建那一刻绑定归属、读写都先查归属。

**口径写死在这里**：

1. **密码哈希自带参数**(``算法$迭代$盐$哈希``):换迭代次数时老密码仍能验证,
   而且**同一密码两次的哈希必须不同**(盐随机)—— 相同就说明盐是固定的;
2. **解析不了的哈希串一律返回 False**,不抛异常(一个坏行不该让整个登录接口 500);
3. **令牌只认 ``Bearer``**:裸令牌、query 参数都不收(URL 会进日志与浏览器历史)。
   WebSocket 是唯一例外(浏览器不能给 WS 设头),那条路单独在 API 层处理;
4. **失败一律同一句话**:不区分"没这个人"和"密码不对"(否则等于送一个用户名枚举接口);
5. **会话没主人 = 拒绝**:``owner_of`` 返回 None 既可能是"加认证前的老会话",
   也可能是"瞎编的 ID",两者都不该让人读到内容;
6. **``auth_store=None`` = 无认证模式**,只可能来自显式注入(验收 / 本机),
   ``build_real_deps`` 永远接上 —— 这条保证 ``check_m5_api`` 那 26 条老契约仍然有效。

除最后一条（真 MySQL 往返）外全部内存假件:**不需要容器/模型/网络**。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path  # 假件签名里要用(函数内的 _Path 只是为了不和它重名)

from _fakes import NoFolders
from _harness import SkipCheck, check, report
# domain 只用标准库,顶层 import 不会拖起 torch/pymilvus,而假件要在运行期造 User
from project_rag.domain.auth import User, UserRole


class _FakeAuthStore:
    """最小可判定的认证存储:`AuthStore` 端口的内存实现(哈希与令牌由用例层算好传进来)。"""

    def __init__(self) -> None:
        self.users: dict[str, tuple[User, str]] = {}  # username -> (User, hash)
        self.tokens: dict[str, tuple[str, float]] = {}  # token_hash -> (user_id, 过期时刻)
        self.owners: dict[str, str] = {}
        self.schemas = 0

    # —— 端口 ——
    def init_schema(self) -> None:
        self.schemas += 1

    def create_user(self, username: str, password_hash: str, role: UserRole) -> User:
        if username in self.users:
            raise ValueError(f"用户名 {username!r} 已存在")
        user = User(user_id=f"u{len(self.users)}", username=username, role=role)
        self.users[username] = (user, password_hash)
        return user

    def find_by_username(self, username: str) -> tuple[User, str] | None:
        return self.users.get(username)

    def set_password(self, user_id: str, password_hash: str) -> None:
        for name, (user, _) in self.users.items():
            if user.user_id == user_id:
                self.users[name] = (user, password_hash)

    def delete_user(self, user_id: str) -> int:
        """起端口多了这一条 —— 假件也要跟得上(不然 pyright 说它不满足 AuthStore)。"""
        return 1 if self.users.pop(user_id, None) is not None else 0

    def list_users(self) -> Sequence[User]:
        return [user for user, _ in self.users.values()]

    def issue_token(self, user: User, token_hash: str, ttl_seconds: int) -> None:
        import time

        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash: str) -> User | None:
        import time

        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next((u for u, _ in self.users.values() if u.user_id == item[0]), None)

    def revoke_token(self, token_hash: str) -> None:
        self.tokens.pop(token_hash, None)

    def revoke_all_tokens(self, user_id: str) -> int:
        doomed = [h for h, (owner, _) in self.tokens.items() if owner == user_id]
        for token_hash in doomed:
            self.tokens.pop(token_hash, None)
        return len(doomed)

    def bind_session(self, session_id: str, user_id: str) -> None:
        self.owners.setdefault(session_id, user_id)

    def owner_of(self, session_id: str) -> str | None:
        return self.owners.get(session_id)


class _FakeLedger:
    """最小账本存储:报表接口只需要 load_since。"""

    def __init__(self) -> None:
        self.schemas = 0

    def init_schema(self) -> None:
        self.schemas += 1

    def record(self, row) -> None:
        pass

    def load_since(self, since):
        return []


def _client(store=None, *, enable_auth=True):
    """输入:认证存储(可省)、是否开认证。

    行为:建一个注入了依赖的 TestClient —— **会话与账本存储也必须注入**,
          否则那些接口会 503("没装配"),而那不是这条检查要测的东西。
          第一版就是漏了这两个,三条检查全红在夹具上。
    输出:``(client, store)``。
    """
    from fastapi.testclient import TestClient

    from _fakes import FakeConversationStore
    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    store = store if store is not None else _FakeAuthStore()
    deps = Deps(
        auth_store_factory=(lambda: store) if enable_auth else None,
        conversation_store_factory=lambda: FakeConversationStore([]),
        ledger_store_factory=lambda: _FakeLedger(),
    )
    return TestClient(create_app(deps)), store


def _login(client, username: str = "boss", password: str = "pw-123456") -> dict[str, str]:
    """输入:客户端。行为:登录并返回带令牌的头。输出:headers。"""
    from project_rag.application.security import hash_password

    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise AssertionError(f"登录该 200,实际 {resp.status_code}:{resp.text[:200]}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _seeded_client():
    """输入:无。行为:造一个已有管理员 + 员工 + **运维**的客户端。输出:``(client, store)``。

    运维在这一份里只是"第三个账号":报表那条检查要它,而它必须能登录、能看报表、
    别处一律 403 —— 这三件事各断言一次。
    """
    from project_rag.application.security import hash_password

    store = _FakeAuthStore()
    store.create_user("boss", hash_password("pw-123456"), "admin")
    store.create_user("worker", hash_password("pw-123456"), "employee")
    store.create_user("watcher", hash_password("pw-123456"), "ops")
    client, store = _client(store)
    return client, store


@check("密码哈希：自带参数、盐随机（同密码两次不同）、坏串返回 False 而不是抛")
def _password_hashing() -> str:
    from project_rag.application.security import hash_password, verify_password

    first, second = hash_password("correct horse"), hash_password("correct horse")
    if first == second:
        raise AssertionError("同一密码两次的哈希相同 —— 盐不是随机的")
    if not first.startswith("pbkdf2_sha256$") or len(first.split("$")) != 4:
        raise AssertionError(f"哈希串格式应为 算法$迭代$盐$哈希,实际 {first[:40]!r}")
    if not verify_password("correct horse", first):
        raise AssertionError("正确密码验证失败")
    if verify_password("wrong horse", first):
        raise AssertionError("错误密码竟然通过")
    if verify_password("x", "这不是一个哈希串"):
        raise AssertionError("解析不了的串该返回 False,而不是当成通过")
    low = hash_password("weak-params", iterations=1000)
    if not verify_password("weak-params", low):
        raise AssertionError("迭代次数写在串里 —— 老参数生成的哈希必须仍能验证(否则改参数=全员重设)")
    return f"格式 {first.split('$')[0]}$…；两次哈希不同；坏串 False；低迭代串仍可验证"


@check("令牌只认 Bearer：裸令牌/空头一律 None（URL 里带令牌是 WebSocket 的例外）")
def _bearer_only() -> str:
    from project_rag.application.security import bearer_token, new_token, token_fingerprint

    if bearer_token("Bearer abc.def") != "abc.def":
        raise AssertionError("标准的 Bearer 头没解析出来")
    if bearer_token("bearer abc") != "abc":
        raise AssertionError("scheme 该大小写不敏感")
    for bad in ("abc", "", None, "Basic abc", "Bearer "):
        if bearer_token(bad) is not None:
            raise AssertionError(f"{bad!r} 不该被当成令牌")
    token = new_token()
    digest = token_fingerprint(token)
    if len(token) < 40 or len(digest) != 64 or token in digest:
        raise AssertionError(f"令牌该够长且进库的是 sha256 摘要:token={len(token)} digest={len(digest)}")
    return f"Bearer 解析正确;令牌 {len(token)} 字符 → 进库 {len(digest)} 字符摘要"


@check("登录：密码错与用户不存在都给 401 且**同一句话**（不给用户名枚举）")
def _login_no_enumeration() -> str:
    client, _ = _seeded_client()
    wrong = client.post("/api/v1/auth/login", json={"username": "boss", "password": "bad"})
    missing = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "bad"})
    if (wrong.status_code, missing.status_code) != (401, 401):
        raise AssertionError(f"两种失败都该 401,实际 {wrong.status_code}/{missing.status_code}")
    if wrong.json()["detail"] != missing.json()["detail"]:
        raise AssertionError(f"失败话术必须一致,实际 {wrong.json()!r} vs {missing.json()!r}")
    ok = client.post("/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"})
    if ok.status_code != 200 or sorted(ok.json()) != ["expires_in", "role", "token", "username"]:
        raise AssertionError(f"登录成功该给 token/username/role/expires_in,实际 {ok.status_code} {ok.json()}")
    return f"401 话术一致(«{wrong.json()['detail']}»);成功给 {sorted(ok.json())}"


@check("会话归属：员工读别人的 403、管理员能读、没主人的谁都不放行、自己的 200")
def _session_ownership() -> str:
    client, store = _seeded_client()
    boss, worker, watcher = (
        _login(client, "boss"),
        _login(client, "worker"),
        _login(client, "watcher"),
    )
    sid = client.post("/api/v1/create_session", headers=worker).json()["session_id"]

    own = client.get(f"/api/v1/history/{sid}", headers=worker)
    if own.status_code != 200:
        raise AssertionError(f"自己的会话该 200,实际 {own.status_code}:{own.text[:200]}")
    other = client.get(f"/api/v1/history/{sid}", headers=watcher)
    if other.status_code != 403:
        raise AssertionError(
            f"**运维读别人的会话该 403**(越权不是权限不够),实际 {other.status_code}"
        )
    admin = client.get(f"/api/v1/history/{sid}", headers=boss)
    if admin.status_code != 200:
        raise AssertionError(
            f"**管理员读别人的会话该 200**(运维台要看真实问答才能迭代),实际 {admin.status_code}"
        )
    if admin.json()["rounds"] != []:
        raise AssertionError(f"空会话该给空轮次,实际 {admin.json()}")
    for who, headers in (("员工", worker), ("管理员", boss)):
        orphan = client.get("/api/v1/history/从没绑过的ID", headers=headers)
        if orphan.status_code != 403:
            raise AssertionError(f"{who}读没主人的会话该 403(不能放行),实际 {orphan.status_code}")
    if client.get(f"/api/v1/history/{sid}").status_code != 401:
        raise AssertionError("不带令牌读会话该 401")
    if store.owners.get(sid) != store.users["worker"][0].user_id:
        raise AssertionError("建会话时没绑定归属 —— 那中间有一段谁都能读的窗口")
    return "自己 200 / 运维读别人 403 / 管理员读别人 200 / 没主人的两人都 403 / 无令牌 401"


@check("角色：报表是运维与管理员的（员工 403、无令牌 401），/me 如实报能力")
def _admin_only() -> str:
    """报表从 ``/admin/metrics`` 挪到 ``/reports/metrics`` —— 运维不是管理员,
    让他去打一个 ``/admin/`` 开头的接口,读代码的人会以为权限配错了。

    三个角色各断言一次:管理员 200、**运维 200**、员工 403;无令牌 401(先认证再授权)。
    ``/me`` 报三个能力布尔值(前端按"能不能"渲染页签,不比较角色字符串)。
    """
    client, _ = _seeded_client()
    boss = _login(client, "boss")
    worker = _login(client, "worker")
    watcher = _login(client, "watcher")
    for who, headers in (("管理员", boss), ("运维", watcher)):
        code = client.get("/api/v1/reports/metrics", headers=headers).status_code
        if code != 200:
            raise AssertionError(f"{who}该能看报表,实际 {code}")
    if client.get("/api/v1/reports/metrics", headers=worker).status_code != 403:
        raise AssertionError("员工看报表该 403")
    if client.get("/api/v1/reports/metrics").status_code != 401:
        raise AssertionError("不带令牌该 401(先认证再授权)")
    me = client.get("/api/v1/auth/me", headers=worker).json()
    if me != {
        "username": "worker",
        "role": "employee",
        "is_admin": False,
        "can_view_reports": False,
        "can_use_knowledge": True,
        "can_manage": False,
    }:
        raise AssertionError(f"/me 该如实报角色与三个能力,实际 {me}")
    watcher_me = client.get("/api/v1/auth/me", headers=watcher).json()
    if (watcher_me["can_view_reports"], watcher_me["can_use_knowledge"]) != (True, True):
        raise AssertionError(f"运维的能力该是『能看报表 + 能问答』,实际 {watcher_me}")
    if watcher_me["can_manage"]:
        raise AssertionError(f"运维不该能管理(上传/删文档/建号/授权),实际 {watcher_me}")
    # 行为那一半(能不能提问 / 能不能管)在 check_p0_6_access 里验 —— 那个假世界装了
    # 知识库那半边;这里只有认证这一半,拿 /me 的能力布尔值当判据就够(前端也只读它)
    return "报表:管理员/运维 200、员工 403;/me 三个能力如实(运维=问答 + 报表,不能管理)"


@check("无认证模式：不注入 auth_store 时老接口照常（check_m5_api 那 25 条仍有效的原因）")
def _auth_disabled_mode() -> str:
    client, _ = _client(enable_auth=False)
    if client.get("/api/v1/auth/me").status_code != 503:
        raise AssertionError("无认证模式下 /me 该 503(而不是假装登录成功)")
    if client.post("/api/v1/create_session").status_code != 200:
        raise AssertionError("无认证模式下 create_session 该照常 200")
    if client.get("/api/v1/history/任意ID").status_code != 200:
        raise AssertionError("无认证模式下 history 该放行 —— 否则老契约全红")
    return "无认证模式:老接口放行、/me 明确 503(不假装已登录)"


@check("真 MySQL 往返：建号 / 登录 / 令牌 / 归属 / 改密码（需要容器）")
def _mysql_roundtrip() -> str:
    import uuid

    from project_rag.adapters.auth_mysql import MySQLAuthStore
    from project_rag.application.security import hash_password, token_fingerprint, verify_password
    from project_rag.config import get_settings

    try:
        store = MySQLAuthStore(get_settings().mysql)
        store.init_schema()
        store.init_schema()  # 幂等:第二次不该炸
    except Exception as exc:  # noqa: BLE001 - 容器没起:SKIP 并点名 owner
        raise SkipCheck(f"连不上 MySQL,这条往返没跑(owner:环境)。原始错误:{exc}") from exc

    name = f"chk_{uuid.uuid4().hex[:8]}"
    user = store.create_user(name, hash_password("pw-123456"), "employee")
    try:
        found = store.find_by_username(name)
        if found is None or not verify_password("pw-123456", found[1]):
            raise AssertionError("建完号读不回来 / 密码验证不过")
        token = f"tok-{uuid.uuid4().hex}"
        store.issue_token(user, token_fingerprint(token), 3600)
        back = store.user_for_token(token_fingerprint(token))
        if back is None or back.user_id != user.user_id or back.role != "employee":
            raise AssertionError(f"令牌换不回用户,实际 {back}")
        if store.user_for_token(token_fingerprint("别的令牌")) is not None:
            raise AssertionError("没签发的令牌竟然能换出用户")
        sid = f"s-{uuid.uuid4().hex[:8]}"
        store.bind_session(sid, user.user_id)
        store.bind_session(sid, "别人")  # 幂等且不该改归属
        if store.owner_of(sid) != user.user_id:
            raise AssertionError("归属被第二次绑定改掉了 —— 那等于'最后问的人拥有会话'")
        store.set_password(user.user_id, hash_password("pw-654321"))
        again = store.find_by_username(name)
        if again is None or not verify_password("pw-654321", again[1]):
            raise AssertionError("改密码没生效")
        store.revoke_token(token_fingerprint(token))
        if store.user_for_token(token_fingerprint(token)) is not None:
            raise AssertionError("登出后令牌还能用")
        return f"{name}:建号/令牌/归属(不可改)/改密码/登出 全部往返成功"
    finally:
        conn = store._connect(autocommit=True)  # noqa: SLF001 - 清理测试数据
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {store._users} WHERE username = %s", (name,))  # noqa: SLF001
        finally:
            conn.close()


@check("WebSocket：无令牌连不上（先回一帧 error 说明原因，再关连接）")
def _ws_requires_token() -> str:
    from starlette.websockets import WebSocketDisconnect

    client, _ = _seeded_client()
    with client.websocket_connect("/api/v1/stream") as ws:
        # **先发一句再收**:被反向测试改坏(不校验令牌)时,如果只等着收帧,
        # 服务端会一直等客户端说话 —— 检查就挂死而不是失败(挂死比失败坏得多:
        # 它会让整个反向测试超时,还会在被 kill 时把源码留在坏版本上)。
        ws.send_json({"query": "你好"})
        frame = ws.receive_json()
        if frame.get("type") != "error" or "认证" not in str(frame.get("detail")):
            raise AssertionError(f"该先回一帧「未认证」的 error 帧，实际 {frame}")
        try:
            ws.receive_json()
        except WebSocketDisconnect:
            return "无令牌：error 帧（说明原因）+ 关闭连接（1008）"
        raise AssertionError("回完 error 帧之后该关闭连接，而不是继续服务")


@check("WebSocket：运维用别人的会话 → error 帧且连接不断；管理员能继续问")
def _ws_ownership_keeps_connection() -> str:
    client, _ = _seeded_client()
    boss, worker, watcher = (
        _login(client, "boss"),
        _login(client, "worker"),
        _login(client, "watcher"),
    )
    sid = client.post("/api/v1/create_session", headers=worker).json()["session_id"]
    watcher_token = watcher["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/api/v1/stream?token={watcher_token}") as ws:
        ws.send_json({"query": "你好", "session_id": sid})
        frame = ws.receive_json()
        if frame.get("type") != "error" or "不属于" not in str(frame.get("detail")):
            raise AssertionError(f"越权该回 error 帧，实际 {frame}")
        ws.send_json({"query": "你好"})  # 不带 session_id → 放行
        frames = [ws.receive_json(), ws.receive_json()]
    if not any(f.get("type") == "done" for f in frames):
        raise AssertionError(f"越权之后连接该继续可用（回帧而不是断链），实际 {frames}")
    # 管理员:同一段会话能直接接着问(HTTP 与 WS 是同一道 ``owner_violation``)
    boss_token = boss["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/api/v1/stream?token={boss_token}") as ws:
        ws.send_json({"query": "你好", "session_id": sid})
        admin_frames = [ws.receive_json(), ws.receive_json()]
    if not any(f.get("type") == "done" for f in admin_frames):
        raise AssertionError(f"管理员该能接着别人的会话问,实际 {admin_frames}")
    return "运维:error 帧且连接不断;管理员:同一段会话正常走完"


@check("WebSocket：无认证模式下不带令牌也能连（check_m5_stream 那 37 条仍有效的原因）")
def _ws_open_without_auth() -> str:
    client, _ = _client(enable_auth=False)
    with client.websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": "你好"})
        frames = [ws.receive_json(), ws.receive_json()]
    if not any(f.get("type") == "done" for f in frames):
        raise AssertionError(f"无认证模式该照常回答，实际 {frames}")
    return "无认证模式：不带令牌也能连（老契约因此全绿）"


class _RecordingDocs(NoFolders):
    """假登记表:记下**调用顺序**(顺序本身就是契约) + 造一个待删的文档。"""

    def __init__(self, record=None, *, delete_raises: bool = False) -> None:
        self.calls: list[str] = []
        self.record = record
        self.delete_raises = delete_raises
        self.statuses: list[str] = []

    def init_schema(self) -> None:
        self.calls.append("init_schema")

    def add(self, record) -> None:
        self.record = record

    def list_all(self):
        return [] if self.record is None else [self.record]

    def get(self, document_id):
        return self.record if self.record and self.record.document_id == document_id else None

    def set_status(self, document_id, status, *, chunks=None, error=None):
        self.calls.append(f"set_status:{status}")
        self.statuses.append(status)

    def upsert(self, record) -> None:
        self.calls.append(f"upsert:{record.document_id}")
        self.record = record

    def remove(self, document_id):
        self.calls.append("remove")
        self.record = None
        return 1


class _RecordingVectors:
    """假向量库:转发到真内存实现(它是端口的参考实现),只把 **删除** 记下来并可选抛错。"""

    def __init__(self, *, raises: bool = False) -> None:
        from project_rag.adapters import InMemoryVectorStore

        self._inner = InMemoryVectorStore()
        self.calls: list[str] = []
        self.raises = raises

    def delete_by_doc_id(self, doc_id: str) -> int:
        self.calls.append(f"delete_by_doc_id:{doc_id}")
        if self.raises:
            raise RuntimeError("Milvus 连接断了(模拟)")
        return self._inner.delete_by_doc_id(doc_id)

    def upsert(self, chunks, embeddings, *, sparse=None):
        return self._inner.upsert(chunks, embeddings, sparse=sparse)

    def search(self, query_embedding, *, k, source=None):
        return self._inner.search(query_embedding, k=k, source=source)

    def search_sparse(self, query_sparse, *, k, source=None):
        return self._inner.search_sparse(query_sparse, k=k, source=source)

    def has_sparse_column(self) -> bool:
        return self._inner.has_sparse_column()

    def fingerprint(self):
        return self._inner.fingerprint()

    def load_all(self):
        return self._inner.load_all()

    def chunk_counts(self):
        return self._inner.chunk_counts()

    def delete_by_source(self, source: str) -> int:
        return self._inner.delete_by_source(source)

    def drop(self) -> None:
        self._inner.drop()


def _doc(tmp: Path, name: str = "手册.pdf"):
    """输入:上传目录 + 文件名。行为:登记一条 processed 文档并落一个真文件。输出:记录。"""
    from project_rag.application.documents import register_document

    (tmp / name).write_text("内容", encoding="utf-8")
    store = _RecordingDocs()
    record = register_document(store, source="demo", filename=name, size_bytes=6)
    store.record = record
    # **登记不属于删除序列**:``register_document`` 现在走 ``upsert``(重传同名即覆盖),
    # 会在这里留下一条 ``upsert:…``。断言的是"删除第一步是标记 deleting",所以把登记那一步清掉。
    store.calls.clear()
    return store, record


@check("文档删除：**先标记 deleting → 删向量 → 删文件 → 删行**，且幂等")
def _delete_order_and_idempotent() -> str:
    import tempfile
    from pathlib import Path as _Path

    from project_rag.application.documents import delete_document

    with tempfile.TemporaryDirectory() as raw:
        tmp = _Path(raw)
        store, record = _doc(tmp)
        vectors = _RecordingVectors()
        outcome = delete_document(store, vectors, record.document_id, uploads_dir=tmp)
        if store.calls[0] != "set_status:deleting":
            raise AssertionError(
                f"第一步必须是「标记 deleting」(先把它从可检索里摘出去),实际 {store.calls}"
            )
        if store.calls[-1] != "remove":
            raise AssertionError(f"最后一步才是删登记行,实际 {store.calls}")
        if not vectors.calls or not outcome.removed_file or not outcome.removed_record:
            raise AssertionError(f"三处都该清:向量 {vectors.calls} / 文件 {outcome.removed_file} / 行 {outcome.removed_record}")
        again = delete_document(store, vectors, record.document_id, uploads_dir=tmp)
        if again.removed_record or again.removed_file or again.deleted_chunks:
            raise AssertionError(f"重复删该什么都不做(幂等),实际 {again}")
        return f"顺序 {store.calls};再删一次 {again}"


@check("文档删除：删向量失败就抛，**不删登记行**（留下可查的痕迹）")
def _delete_raises_keeps_record() -> str:
    import tempfile
    from pathlib import Path as _Path

    from project_rag.application.documents import delete_document

    with tempfile.TemporaryDirectory() as raw:
        tmp = _Path(raw)
        store, record = _doc(tmp)
        vectors = _RecordingVectors(raises=True)
        try:
            delete_document(store, vectors, record.document_id, uploads_dir=tmp)
        except RuntimeError:
            pass
        else:
            raise AssertionError("向量没删掉就该抛出去 —— 静默成功会让表和库不一致")
        if "remove" in store.calls:
            raise AssertionError(
                "向量删除失败后**不许**删登记行:留着这行才查得出「库里还有孤儿块」"
            )
        if store.record is None:
            raise AssertionError("登记行不该消失")
        return f"抛错且登记行还在(状态 {store.statuses});文件也还在:{(tmp / record.filename).exists()}"


def _chunker():
    """给上传路径用的切分器(真实现,参数取小值)。"""
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker

    return ChineseRecursiveChunker(400, 200, 40)


def _loader():
    """给上传路径用的装载函数(真实现)。"""
    from project_rag.adapters.document_loader import load_documents

    return load_documents


@check("文档接口：清单要登录（员工可看）、删除要管理员（员工 403、无令牌 401）")
def _document_endpoints() -> str:
    from project_rag.domain.documents import DocRecord

    client, store = _seeded_client()
    boss, worker = _login(client, "boss"), _login(client, "worker")
    fake_docs = _RecordingDocs(DocRecord("d1", "demo", "手册.pdf", "processed", 15, 2048, "<ISO 时间>"))
    deps = None
    from project_rag.cli.deps import Deps
    from project_rag.api.app import create_app
    from fastapi.testclient import TestClient

    deps = Deps(
        auth_store_factory=lambda: store,
        conversation_store_factory=lambda: __import__("_fakes").FakeConversationStore([]),
        document_store_factory=lambda: fake_docs,
        # 删除要走"删向量"那一步 —— 不注入的话端点会 503(第一版就是漏了这个)
        vector_store_factory=_RecordingVectors,
        # 上传/能力接口要的三样(第一版漏了 → 端点 503,检查红在夹具上)
        suffixes_factory=lambda: (".md", ".txt"),
        chunker_factory=_chunker,
        loader_factory=_loader,
    )
    client = TestClient(create_app(deps))
    boss, worker = _login(client, "boss"), _login(client, "worker")

    listed = client.get("/api/v1/documents", headers=worker)
    if listed.status_code != 200 or listed.json()["documents"][0]["filename"] != "手册.pdf":
        raise AssertionError(f"员工该能看到清单(他要知道自己能检索什么),实际 {listed.status_code} {listed.text[:160]}")
    if client.get("/api/v1/documents").status_code != 401:
        raise AssertionError("不带令牌看清单该 401")
    if client.delete("/api/v1/documents/d1", headers=worker).status_code != 403:
        raise AssertionError("员工删文档该 403")
    if client.delete("/api/v1/documents/d1").status_code != 401:
        raise AssertionError("不带令牌删文档该 401")
    ok = client.delete("/api/v1/documents/d1", headers=boss)
    if ok.status_code != 200 or not ok.json()["removed_record"]:
        raise AssertionError(f"管理员该能删,实际 {ok.status_code} {ok.text[:160]}")
    return "清单:员工 200 / 无令牌 401;删除:员工 403 / 无令牌 401 / 管理员 200"


@check("上传：文件名只取 basename（`../x.md` 不许写到 uploads 目录外面）+ 后缀白名单")
def _upload_path_traversal() -> str:
    import tempfile
    from pathlib import Path as _Path

    from fastapi.testclient import TestClient

    from _fakes import FakeConversationStore
    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    with tempfile.TemporaryDirectory() as raw:
        tmp = _Path(raw)
        uploads = tmp / "uploads"
        store = _FakeAuthStore()
        from project_rag.application.security import hash_password

        store.create_user("boss", hash_password("pw-123456"), "admin")
        docs = _RecordingDocs()
        deps = Deps(
            auth_store_factory=lambda: store,
            conversation_store_factory=lambda: FakeConversationStore([]),
            document_store_factory=lambda: docs,
            suffixes_factory=lambda: (".md",),
            chunker_factory=_chunker,
            loader_factory=_loader,
            vector_store_factory=_RecordingVectors,
        )
        client = TestClient(create_app(deps))
        headers = _login(client, "boss")

        # 把上传目录指到临时目录:不能拿真上传目录做路径穿越实验
        import project_rag.api.app as app_module

        original = app_module._uploads_dir
        app_module._uploads_dir = lambda: uploads  # type: ignore[assignment]
        try:
            bad_suffix = client.post(
                "/api/v1/documents?filename=木马.exe", headers=headers, content=b"xx"
            )
            if bad_suffix.status_code != 400:
                raise AssertionError(f"不在白名单的后缀该 400,实际 {bad_suffix.status_code}")
            r = client.post(
                "/api/v1/documents?filename=../../逃逸.md", headers=headers, content=b"# hi"
            )
            if r.status_code != 202:
                raise AssertionError(f"穿越名该被规整成 basename 后接受,实际 {r.status_code}")
            if r.json()["filename"] != "逃逸.md":
                raise AssertionError(f"落盘名该只剩 basename,实际 {r.json()['filename']!r}")
            escaped = (tmp.parent / "逃逸.md").exists() or (tmp / ".." / ".." / "逃逸.md").exists()
            if escaped:
                raise AssertionError("文件被写到 uploads 目录外面了 —— 路径穿越")
            if not (uploads / "逃逸.md").exists():
                raise AssertionError("文件该落在 uploads 里")
        finally:
            app_module._uploads_dir = original  # type: ignore[assignment]
        return "后缀白名单 400;`../../逃逸.md` → 落盘为 uploads/逃逸.md(没跑出去)"


@check("文档 ID 口径：登记表的 document_id == load_documents 给的 doc_id（不一致就静默删不掉）")
def _document_id_matches_loader() -> str:
    import tempfile
    from pathlib import Path as _Path

    from project_rag.adapters.document_loader import load_documents
    from project_rag.application.documents import register_document

    with tempfile.TemporaryDirectory() as raw:
        tmp = _Path(raw)
        uploads = tmp / "uploads"
        uploads.mkdir()
        (uploads / "手册.md").write_text("# 标题\n正文", encoding="utf-8")

        store = _RecordingDocs()
        record = register_document(store, source=uploads.name, filename="手册.md", size_bytes=10)
        loaded = load_documents(uploads / "手册.md")
        doc = loaded.documents[0]
        if doc.doc_id != record.document_id:
            raise AssertionError(
                f"登记 ID {record.document_id!r} != 装载器 doc_id {doc.doc_id!r} —— "
                f"删除时 delete_by_doc_id(登记 ID) 会一块都删不掉(返回 0,静默)"
            )
        if doc.source != record.source:
            raise AssertionError(
                f"登记 source {record.source!r} != 装载器 source {doc.source!r} —— "
                f"/api/v1/sources 与登记表会说两个来源名"
            )
        return f"两者都是 {doc.doc_id!r} / source={doc.source!r}（删除才删得掉）"


@check("文档入库→删除：向量真的被删掉（用真装载器 + 内存向量库，不需要模型）")
def _ingest_then_delete_removes_vectors() -> str:
    import tempfile
    from pathlib import Path as _Path

    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.application.documents import (
        delete_document,
        ingest_uploaded,
        register_document,
    )

    with tempfile.TemporaryDirectory() as raw:
        tmp = _Path(raw)
        uploads = tmp / "uploads"
        uploads.mkdir()
        (uploads / "手册.md").write_text("# 退款\n退款在订单页点申请退款。" * 3, encoding="utf-8")

        store = _RecordingDocs()
        record = register_document(store, source=uploads.name, filename="手册.md", size_bytes=60)
        store.record = record
        vectors = _RecordingVectors()
        chunks = ingest_uploaded(
            store,
            record,
            uploads / "手册.md",
            chunker=ChineseRecursiveChunker(400, 200, 40),
            embedder=HashingEmbedder(),
            vector_store=vectors,
            batch_size=8,
            load=_loader(),
        )
        if chunks <= 0 or store.statuses[-1] != "processed":
            raise AssertionError(f"该入库成功并推到 processed,实际 chunks={chunks} 状态={store.statuses}")
        before = len(list(vectors.load_all()))
        outcome = delete_document(store, vectors, record.document_id, uploads_dir=uploads)
        after = len(list(vectors.load_all()))
        if before <= 0 or after != 0 or outcome.deleted_chunks != before:
            raise AssertionError(
                f"删除该把它的块全清掉:入库 {before} 块、删了 {outcome.deleted_chunks}、剩 {after}"
            )
        return f"入库 {chunks} 块 → 删除清掉 {outcome.deleted_chunks} 块、库里剩 {after} 块"


@check("对账：孤儿块与缺块都报得出来，且 pending/failed 不算缺块")
def _reconcile_reports() -> str:
    from project_rag.application.reconcile import reconcile
    from project_rag.domain.documents import DocRecord, DocStatus

    def rec(doc_id: str, status: DocStatus) -> DocRecord:
        return DocRecord(doc_id, "uploads", f"{doc_id}.md", status, 0, 10, "<ISO 时间>")

    # ① 干净:登记两份、库里两份都有块
    clean = reconcile(
        [rec("a.md", "processed"), rec("b.md", "processed")],
        {"a.md": 3, "b.md": 5},
    )
    if not clean.healthy or clean.ok != 2:
        raise AssertionError(f"两边一致时该 healthy,实际 {clean}")

    # ② 孤儿块:库里有 c.md,登记表没有
    orphan = reconcile([rec("a.md", "processed")], {"a.md": 3, "c.md": 9})
    if orphan.orphans != ("c.md",) or orphan.healthy:
        raise AssertionError(f"孤儿块该被报出来且不 healthy,实际 {orphan}")

    # ③ 缺块:登记 processed 但库里没有;**pending/failed 不算缺块**(它们本来就不该有)
    missing = reconcile(
        [rec("a.md", "processed"), rec("p.md", "pending"), rec("f.md", "failed")],
        {"a.md": 3},
    )
    if missing.missing:
        raise AssertionError(f"a.md 库里有块,不该报缺;实际 {missing.missing}")
    gone = reconcile([rec("a.md", "processed")], {})
    if gone.missing != ("a.md",):
        raise AssertionError(f"processed 但库内零块该报缺块,实际 {gone.missing}")
    if reconcile([rec("p.md", "pending")], {}).missing:
        raise AssertionError("pending 不该算缺块(还没入库呢)")
    return f"干净 {clean.ok} 份;孤儿 {orphan.orphans};缺块 {gone.missing};pending 不算缺"


@check("对账 回填：给老语料补的行是 processed + size=0（标成 pending 会自己造假问题）")
def _backfill_shape() -> str:
    from project_rag.application.reconcile import backfill_records

    rows = backfill_records(
        {"bagu_ai": ("bagu_ai", 677), "lecture/01.md": ("doc", 11)},
        created_at="<ISO 时间>",
    )
    if [r.document_id for r in rows] != ["bagu_ai", "lecture/01.md"]:
        raise AssertionError(f"该按 doc_id 升序造行,实际 {[r.document_id for r in rows]}")
    head = rows[0]
    if head.status != "processed" or head.chunks != 677 or head.size_bytes != 0:
        raise AssertionError(
            f"回填行该是 processed + 真实块数 + size 0(不编大小),实际 {head}"
        )
    if not head.searchable:
        raise AssertionError("回填的老语料本来就能检索,searchable 该为真")
    if backfill_records({}, created_at="x"):
        raise AssertionError("没有孤儿块时不该造任何行")
    return f"回填 {len(rows)} 行:{head.document_id} → {head.status} / {head.chunks} 块 / size 0"


@check("对账 清理入口：只删点名的那几个（孤儿块 / 缺块行），逐条回报，默认一个都不动")
def _cleanup_entry() -> str:
    """``doctor --prune-orphans`` / ``--forget-missing`` 背后那两个动作。

    **这一条盯的是「删得比该删的多」** —— 那才是这两个动作唯一可怕的失败方式：
    删少了看得见（对账还报），删多了（把别人还在用的块清掉）没人会立刻发现。
    所以断言分三层：① 开关默认关；② 只调用点名的那个 doc_id；③ 登记过的块必须原封不动。
    """
    import argparse

    from project_rag.adapters import HashingEmbedder
    from project_rag.application.reconcile import forget_missing, prune_orphans
    from project_rag.cli.commands import doctor as doctor_cmd
    from project_rag.domain import Chunk
    from project_rag.domain.documents import DocRecord

    # ① 默认只读：不给开关时两个动作都不做（它们删数据，不许有默认值）
    parser = argparse.ArgumentParser()
    doctor_cmd.add_parser(parser.add_subparsers())
    args = parser.parse_args([doctor_cmd.NAME])
    if args.prune_orphans or args.forget_missing:
        raise AssertionError("两个清理开关默认必须是关的 —— 默认开 = 自动删数据")

    # ② 孤儿块：只删点名的那份，登记过的块一块都不许碰
    vectors = _RecordingVectors()
    embedder = HashingEmbedder(dim=64)
    rows = [
        Chunk("x1", "登记过的块", "demo", "a.md"),
        Chunk("x2", "孤儿块", "old", "c.md"),
    ]
    vectors.upsert(rows, embedder.embed([row.text for row in rows]))
    outcome = prune_orphans(vectors, ["c.md"])
    if outcome != (("c.md", 1),):
        raise AssertionError(f"该逐条回报 (('c.md', 1),)，实际 {outcome}")
    if vectors.calls != ["delete_by_doc_id:c.md"]:
        raise AssertionError(f"只许删点名的那一份，实际调用 {vectors.calls}")
    left = {chunk.doc_id for chunk in vectors.load_all()}
    if left != {"a.md"}:
        raise AssertionError(f"登记过的块不许被碰，实际库里剩 {left}")

    # ③ 空清单 = 什么都不做（不是"那就全清一遍"）
    if prune_orphans(vectors, []) != () or vectors.calls != ["delete_by_doc_id:c.md"]:
        raise AssertionError("空清单该什么都不做 —— 删得比该删的多，是这个动作唯一可怕的失败方式")

    # ④ 缺块行：同样只删点名的
    row = DocRecord(
        document_id="gone.md", source="uploads", filename="gone.md", status="processed",
        chunks=0, size_bytes=0, created_at="<ISO 时间>",
    )
    docs = _RecordingDocs(record=row)
    rows_removed = forget_missing(docs, ["gone.md"])
    if rows_removed != (("gone.md", 1),) or "remove" not in docs.calls:
        raise AssertionError(f"该删掉那一条登记行并回报 1，实际 {rows_removed} / {docs.calls}")
    untouched = _RecordingDocs(record=row)
    if forget_missing(untouched, []) != () or untouched.calls:
        raise AssertionError("空清单不该动登记行")
    return (
        f"开关默认关；孤儿只删 c.md（1 块），登记过的 a.md 原封不动；缺块行删 1 条；空清单零动作"
    )


@check("对账 CLI：doctor 默认一个都不删；清完按**清理后**的状态给退出码")
def _doctor_cli() -> str:
    """命令层这一小段最容易被写歪：开关接上了、但没接到动作上（报告里一切正常，数据纹丝不动），
    或者清了半天退出码还按清理前的算（cron 里天天报警）。所以从 ``run()`` 那一层走一遍。
    """
    import argparse
    import contextlib
    import io

    from project_rag.adapters import HashingEmbedder, InMemoryVectorStore
    from project_rag.cli.commands import doctor as doctor_cmd
    from project_rag.cli.deps import Deps
    from project_rag.domain import Chunk
    from project_rag.domain.documents import DocRecord

    vectors = InMemoryVectorStore()
    rows = [
        Chunk("x1", "登记过的块", "demo", "a.md"),
        Chunk("x2", "孤儿块", "old", "c.md"),
    ]
    vectors.upsert(rows, HashingEmbedder(dim=64).embed([row.text for row in rows]))

    class _TwoRows(NoFolders):
        """两条登记行:**a.md(有块,健康)** 与 gone.md(缺块)。

        为什么不用单条的 ``_RecordingDocs``:那种假件里 a.md 没登记,于是它自己也是个孤儿 ——
        断言就变成了在验别的东西(这一条第一次跑就是这么红的,报「剩 set()」)。
        """

        def __init__(self, *records) -> None:
            self.records = list(records)
            self.removed: list[str] = []

        def init_schema(self) -> None: ...

        def add(self, record) -> None:
            self.records.append(record)

        def list_all(self):
            return list(self.records)

        def get(self, document_id):
            return next(
                (row for row in self.records if row.document_id == document_id), None
            )

        def set_status(self, document_id, status, *, chunks=None, error=None) -> None: ...

        def upsert(self, record) -> None:
            self.records = [row for row in self.records if row.document_id != record.document_id]
            self.records.append(record)

        def remove(self, document_id) -> int:
            self.removed.append(document_id)
            before = len(self.records)
            self.records = [row for row in self.records if row.document_id != document_id]
            return before - len(self.records)

    def _row(document_id: str, *, chunks: int) -> DocRecord:
        return DocRecord(
            document_id=document_id, source="uploads", filename=document_id,
            status="processed", chunks=chunks, size_bytes=0,
            created_at="<ISO 时间>",
        )

    docs = _TwoRows(_row("a.md", chunks=1), _row("gone.md", chunks=0))
    deps = Deps(document_store_factory=lambda: docs, vector_store_factory=lambda: vectors)

    def doctor(*flags: str) -> tuple[int, str]:
        args = argparse.Namespace(
            all=True, backfill=False, prune_orphans=False, forget_missing=False, deps=deps
        )
        for flag in flags:
            setattr(args, flag, True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = doctor_cmd.run(args)
        return code, buffer.getvalue()

    # ① 不给开关 = 只读:孤儿与缺块都报出来、退出码 1,但**一块数据都不许动**
    code, out = doctor()
    if code != 1 or "孤儿块" not in out or "缺块" not in out:
        raise AssertionError(f"默认该只读且报两类问题(退出码 1),实际 code={code}：{out[:200]}")
    if {chunk.doc_id for chunk in vectors.load_all()} != {"a.md", "c.md"} or docs.removed:
        raise AssertionError("没给开关却动了数据 —— 默认必须只读")

    # ② --prune-orphans:清掉孤儿块,登记过的 a.md 留着;缺块还在,所以退出码仍是 1
    code, out = doctor("prune_orphans")
    left = {chunk.doc_id for chunk in vectors.load_all()}
    if left != {"a.md"} or "c.md" not in out or code != 1:
        raise AssertionError(f"该只清掉 c.md 且仍报缺块(退出码 1),实际剩 {left} / code={code}：{out[:200]}")

    # ③ --forget-missing:删掉缺块行之后两边一致 → 退出码 0(不是按清理前的算)
    code, out = doctor("forget_missing")
    if code != 0 or "两边一致" not in out or docs.removed != ["gone.md"]:
        raise AssertionError(f"清完该报一致并返回 0,实际 code={code} / 删了 {docs.removed}：{out[:200]}")
    return "默认只读(退出码 1,零删除)；prune 只清 c.md；forget 只删 gone.md 且退出码 0"


def main() -> int:
    return report(
        "契约：认证 / 角色 / 会话归属 / 文档治理 / 上传入库 / 删除 / 对账清理",
        notes=("说明：除「真 MySQL 往返」外全部内存假件（不需要容器/模型/网络）。",),
        hint=(
            "提示：建号走 `project-rag user add -u 名字 --role admin`（没有注册接口）；"
            "登录 `POST /api/v1/auth/login` 换 Bearer 令牌。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

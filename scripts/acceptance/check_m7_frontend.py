#!/usr/bin/env python
"""M7.1 契约：前端聊天页（Vite+Vue3+TS）+ 它依赖的三个后端接口。

用法::

    uv run python scripts/acceptance/check_m7_frontend.py

**这一份在堵什么洞**：在这之前 `src/project_rag/api/static/index.html` 只有 101 行、
是文档里自己标注的「内置调试页」—— 没有会话列表、没有角色分流、没有参数面板；
而参数面板要的那些旋钮（融合方式 / 两路权重 / 重排开关 / 上下文条数）**在 HTTP 上根本不存在**
（`QueryRequest` 只有 query/session_id/source_filter/k）。M7.1 把这条链补成
「前端每个控件 → 一个接口字段 → 一次可断言的行为」。那份旧页面后来被删掉：
它守着的那两条性质搬到了新前端上 —— 「页面连的是服务端那条 WS」归**前端清单↔后端路由表**
那条，「令牌走 `?token=`」归 WebSocket 那条（见 `reverse_m7_frontend` 的 RT-11 / RT-12）。

公开契约::

    GET  /api/v1/capabilities
        -> {suffixes, max_upload_mb, models[], model_settings, retrieval{modes, fusions,
            defaults, limits, rerank_available}, roles[]}      # 前端**唯一**的能力来源
    POST /api/v1/query  (body: QueryRequest)
        -> 新增 mode / fusion / bm25_weight / vector_weight / rerank / max_context / rrf_k
    GET  /api/v1/sessions
        -> {sessions: [{session_id, title, rounds, created_at, updated_at}]}   # 按账号过滤
    GET  /          -> 307 -> /static/app/
    GET  /static/app/  -> 前端产物（index.html 引用 ./assets/…）
    GET /static/index.html -> **404**（那份 101 行的内置调试页 已删，界面只留一份）

**五条口径**（每条都对着一个静默坏法）::

    1. **capabilities 是前端唯一的能力来源**：前端源码里不许出现写死的后缀表 / 检索方式表 /
       模型名。写两份的后果是"界面允许、后端拒绝"（或反过来：后端新加一种融合方式，
       前端下拉框里没有）。
    2. **`mode` 与权重不能同时给**（422）：模式已经决定了跑哪几路，两个真值来源必然打架。
    3. **权重 ≤ 0 的那一路不跑**，而且是**连检索都不做**（用调用计数断言）——
       白跑一趟再丢掉，延迟账上的 `embed` / `vector` / `bm25` 三段就都在骗人。
    4. **`rerank=true` 而部署没装重排器 → 400 且话术可执行**（点名去掉哪个字段）：
       静默降级成"不重排"是看不见的谎。
    5. **无认证模式下列会话给空表**（不是"列出所有人的"）：没有账号时"这是谁的会话"
       没有答案，而"没有答案就都给"是最坏的一种实现。
    6. **存储层读不到时给 503 + 人话**：裸的 500 只会显示 "Internal Server Error" ——
       用户读到的结论是"会话都没了"，而原因（某种 SQL 错）一个字都没露。

前端部分是**静态契约**（读源码与构建产物），后端部分是**行为契约**（TestClient + 假件）。
不需要容器 / 模型 / 网络，也不跑 `npm` —— 只有"产物比源码新"那一条会比较 mtime。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
WEB_SRC = WEB / "src"
APP_DIR = ROOT / "src" / "project_rag" / "api" / "static" / "app"
STATIC = ROOT / "src" / "project_rag" / "api" / "static"


# ── 前端源码读取 ─────────────────────────────────────────────────────────────
def _sources() -> dict[str, str]:
    """输入:无。行为:读 web/src 下全部 .ts/.vue。输出:相对路径 -> 正文。"""
    files = sorted([*WEB_SRC.rglob("*.ts"), *WEB_SRC.rglob("*.vue")])
    if not files:
        raise AssertionError(f"前端源码不在 {WEB_SRC}（web/src 一个文件都没有）")
    return {str(path.relative_to(WEB)): path.read_text(encoding="utf-8") for path in files}


def _strip_comments(text: str) -> str:
    """输入:一份前端源码。行为:去掉块注释与**整行**注释。输出:去掉注释的文本。

    刻意只去整行 ``//`` 注释(不是行尾的):``http://…`` 里的 ``//`` 会被误伤,
    而后端地址那条检查正需要看到完整的 URL。
    """
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", without_block)


def _backend_module():
    from project_rag.api import app as app_module

    return app_module


def _schemas():
    from project_rag.api import schemas

    return schemas


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _Counter:
    """给工厂套一层调用计数：用来断言"没装重排器"是真的没构造，而不是构造了没用。"""

    def __init__(self, make):
        self._make = make
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._make()


class _CountingVector:
    def __init__(self, hits=()):
        from _fakes import FakeVectorStore

        self._inner = FakeVectorStore(list(hits))
        self.searches = 0

    def search(self, query_embedding, *, k, source=None):
        self.searches += 1
        return self._inner.search(query_embedding, k=k, source=source)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingKeyword:
    def __init__(self):
        from _fakes import FakeKeywordIndex

        self._inner = FakeKeywordIndex()
        self.searches = 0

    def search(self, query, *, k, source=None):
        self.searches += 1
        return self._inner.search(query, k=k, source=source)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# 口令只有这一处：`_FakeAuthStore` 建号与 `_World.token()` 登录都读它。
# （第一版两处各写一份,employee 的登录密码与库里存的对不上 —— 检查自己红了一次。）
_PASSWORDS = {"admin": "admin-pass", "employee": "emp-pass"}


class _FakeAuthStore:
    """认证存储的内存替身：只实现会话目录那几条要用到的动作。"""

    def __init__(self) -> None:
        from project_rag.application.security import hash_password
        from project_rag.domain.auth import User

        self.users = {
            "admin": (User("u-admin", "admin", "admin"), hash_password(_PASSWORDS["admin"])),
            "employee": (
                User("u-emp", "employee", "employee"),
                hash_password(_PASSWORDS["employee"]),
            ),
        }
        self.tokens: dict[str, tuple[str, float]] = {}
        self.owners: dict[str, str] = {}

    def init_schema(self) -> None: ...

    def create_user(self, username, password_hash, role):  # pragma: no cover - 本脚本不用
        raise NotImplementedError

    def set_password(self, user_id, password_hash) -> None: ...

    def delete_user(self, user_id) -> int:  # pragma: no cover - 本脚本用不到
        return 0

    def list_users(self):
        return [user for user, _ in self.users.values()]

    def find_by_username(self, username):
        return self.users.get(username)

    def issue_token(self, user, token_hash, ttl_seconds) -> None:
        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash):
        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next(user for user, _ in self.users.values() if user.user_id == item[0])

    def revoke_token(self, token_hash) -> None:
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id) -> None:
        self.owners.setdefault(session_id, user_id)

    def owner_of(self, session_id):
        return self.owners.get(session_id)


class _FakeCatalog:
    """会话目录的内存替身：**按 user_id 给不同的东西**，好断言"过滤"真的发生了。"""

    def __init__(self) -> None:
        from project_rag.domain.sessions import SessionSummary

        self.rows = {
            "u-admin": [
                SessionSummary("s-admin-1", "管理员的第一段", 3, "<ISO 时间>", "<ISO 时间>"),
                SessionSummary("s-admin-2", " 第二段\n带换行 ", 1, "<ISO 时间>", "<ISO 时间>"),
            ],
            "u-emp": [
                SessionSummary("s-emp-1", "员工的第一段", 7, "<ISO 时间>", "<ISO 时间>"),
            ],
        }
        self.calls: list[tuple[str, int]] = []

    def list_for(self, user_id: str, *, limit: int):
        self.calls.append((user_id, limit))
        return tuple(self.rows.get(user_id, ())[:limit])


class _World:
    """一套可注入的假依赖 + 计数。**每个检查自己建一个**，避免互相污染计数。"""

    def __init__(self, *, auth: bool = False, catalog: bool = True, reranker: bool = True) -> None:
        from _fakes import (
            FakeConversationStore,
            FakeEmbedder,
            FakeFaqStore,
            FakeLLM,
            FakeMatcher,
            FakeReranker,
        )
        from project_rag.cli import Deps
        from project_rag.domain import Chunk, RetrievedChunk

        self.hits = [
            RetrievedChunk(
                chunk=Chunk("c1", "子块", "bagu_ai", "handbook.pdf", "p1", "父块正文", page=7),
                score=0.91,
            ),
            RetrievedChunk(
                chunk=Chunk("c2", "子块", "bagu_java", "notes.md", "p2", "父块正文"),
                score=0.72,
            ),
        ]
        self.vector = _CountingVector(self.hits)
        self.keyword = _CountingKeyword()
        self.reranker = _Counter(lambda: FakeReranker())
        self.auth_store = _FakeAuthStore()
        self.catalog = _FakeCatalog()
        # Any 而不是 object:这个字典要装函数、标量、None 三种东西,
        # 写成 object 会让 pyright 对每个字段各报一次（29 条噪声把真问题埋了）
        fields: dict[str, Any] = {
            "llm_factory": lambda: FakeLLM(["答", "案"]),
            "faq_store_factory": lambda: FakeFaqStore([]),
            "matcher_factory": lambda: FakeMatcher(None),
            "cache_factory": None,
            "faq_threshold": 0.85,
            "embedder_factory": lambda: FakeEmbedder(),
            "vector_store_factory": lambda: self.vector,
            "keyword_index_factory": lambda: self.keyword,
            "conversation_store_factory": lambda: FakeConversationStore(),
            "suffixes_factory": lambda: (".pdf", ".md"),
        }
        if reranker:
            fields["reranker_factory"] = self.reranker
        if auth:
            fields["auth_store_factory"] = lambda: self.auth_store
        if catalog:
            fields["session_catalog_factory"] = lambda: self.catalog
        self.deps = Deps(**fields)

    def client(self):
        from fastapi.testclient import TestClient

        from project_rag.api import create_app

        return TestClient(create_app(self.deps))

    def token(self, username: str) -> str:
        """输入:用户名。行为:走真登录接口换令牌。输出:令牌。"""
        client = self.client()
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": _PASSWORDS[username]},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["token"])


def _options_for(**payload):
    """输入:QueryRequest 的字段。行为:走 `_options` 翻译一次。输出:RetrievalOptions。

    P0-6 起 `_options` 还要一个可见范围结论(``denied_docs``)。这里给**不受限**的那一份:
    这条检查测的是"检索旋钮怎么翻译",可见范围另有一套契约(``check_p0_6_access.py``)。
    """
    from project_rag.api.schemas import QueryRequest
    from project_rag.application.access import UNRESTRICTED

    request = QueryRequest(query="随便问问", **payload)
    return _backend_module()._options(_World().deps, request, UNRESTRICTED)


# ══ 一、前端工程与产物（静态契约） ═══════════════════════════════════════════
def _source_digest() -> tuple[str, int]:
    """输入:无。行为:按 ``web/scripts/source-digest.mjs`` 同一算法算摘要。输出:(摘要, 文件数)。

    **刻意重写一遍而不是跑 node**:回归不该依赖 node(CI / 只装了 Python 的机器也要能跑)。
    代价是同一套算法有两个实现 —— 所以这两个文件互相点名,改一个必须改另一个。
    摘要只认内容:``git checkout``、反向测试还原源码、换机器 clone 都不会让它假红(mtime 会)。
    """
    import hashlib

    files = [
        WEB / "package.json",
        WEB / "vite.config.ts",
        WEB / "index.html",
        *sorted(path for path in WEB_SRC.rglob("*") if path.is_file()),
    ]
    entries = sorted(
        (path.relative_to(WEB).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in files
        if path.is_file()
    )
    outer = hashlib.sha256()
    for rel, digest in entries:
        outer.update(f"{rel}\0{digest}\n".encode())
    return outer.hexdigest(), len(entries)


@check("前端产物与源码同源（改了前端忘了构建 → 这里红；摘要只认内容，不认 mtime）")
def _() -> str:
    index = APP_DIR / "index.html"
    if not index.exists():
        raise AssertionError(
            f"前端产物不在 {APP_DIR}：先跑 `cd web && npm install && npm run build`（产物要入库，"
            "Python 侧与镜像都不装 node）"
        )
    html = index.read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="\./(assets/[^"]+)"', html)
    if not assets:
        raise AssertionError("产物 index.html 里没有 ./assets/… 引用：base 不是相对的？")
    for asset in assets:
        if not (APP_DIR / asset).exists():
            raise AssertionError(f"index.html 引用了不存在的产物 {asset}")

    stamp = APP_DIR / "source-digest.txt"
    if not stamp.exists():
        raise AssertionError(
            "产物里没有 source-digest.txt：`npm run build` 末尾会写它（只跑 vite build 不算）"
        )
    built = stamp.read_text(encoding="utf-8").strip()
    current, count = _source_digest()
    if built != current:
        raise AssertionError(
            f"产物是**另一版源码**构建的（产物 {built[:12]}… vs 当前 {current[:12]}…）："
            "跑 `cd web && npm run build`；否则仓库里跑的界面和你改的源码不是一份东西"
        )
    return f"{len(assets)} 个资源；摘要 {current[:12]}…（{count} 个源文件）与源码一致"


@check("前端用 hash 路由 + 相对 base（后端不做 SPA fallback、桌面壳要能本地打开）")
def _() -> str:
    router_text = (WEB_SRC / "router.ts").read_text(encoding="utf-8")
    if "createWebHashHistory" not in router_text:
        raise AssertionError("router.ts 不是 hash 路由：StaticFiles 不做 fallback，刷新就会 404")
    if "createWebHistory" in router_text:
        raise AssertionError("router.ts 同时用了 history 模式：两种模式只能留一种")
    config = (WEB / "vite.config.ts").read_text(encoding="utf-8")
    if "base: './'" not in config:
        raise AssertionError("vite.config.ts 的 base 不是 './'：file:// 下会去找文件系统根目录")
    html = (APP_DIR / "index.html").read_text(encoding="utf-8")
    if 'src="/assets' in html or 'href="/assets' in html:
        raise AssertionError("产物里出现绝对 /assets 路径：base 没生效")
    return "hash 路由 + 相对 base + 产物引用 ./assets/"


@check("接口前缀只有一处：/api/v1 只写在 endpoints.ts 的 API_PREFIX（其余一律拼出来）")
def _() -> str:
    endpoints = (WEB_SRC / "api" / "endpoints.ts").read_text(encoding="utf-8")
    if "export const API_PREFIX = '/api/v1'" not in endpoints:
        raise AssertionError("endpoints.ts 里没有 `export const API_PREFIX = '/api/v1'`")
    # `endpoints.ts` **自己**也要扫,只放过定义前缀的那一行:
    # 否则 `export const STREAM_PATH = '/api/stream'` 这种"绕开 API_PREFIX 的老路径"
    # 正好落在这条检查的盲区里（补,见 reverse_m7_frontend 的 RT-11)。
    declaration = "export const API_PREFIX = '/api/v1'"
    stray = [
        name
        for name, text in _sources().items()
        if re.search(
            r"['\"`]/api/",
            _strip_comments(text).replace(declaration, "API_PREFIX_DECLARATION"),
        )
    ]
    if stray:
        raise AssertionError(
            f"这些文件里散落了 /api 路径字面量（应该从 API_PREFIX 拼）：{stray}"
        )
    return "前缀只在 endpoints.ts 的定义行；其余 0 处 /api 字面量"


# 前端清单里每一条 `${API_PREFIX}…` 都要在后端路由表里找得到。
# 抓的是模板字符串整段(含 `${encodeURIComponent(sessionId)}`),所以到 `?` 就停:
# query 是调用方拼的,不属于"路径"。
_FRONTEND_PATH = re.compile(r"\$\{API_PREFIX\}(/[^?`'\"\s]*)")


def _shape(path: str) -> str:
    """输入:一条路径（可含 `${…}` 或 `{param}`）。行为:把参数段抹成 `{}`。输出:可比较的形状。

    为什么要抹:前端写 `${encodeURIComponent(id)}`、FastAPI 写 `{document_id}` ——
    同一个位置两种写法,比字面量只会比出假红。抹掉之后比的才是**路由的形状**。

    先抹 `${…}` 再抹 `{…}`:反过来的话 `{[^}]*}` 会把 `{encodeURIComponent(id)}` 吃掉,
    留下一个孤零零的 `$`（第一版就这么写,`/history/${}` vs `/history/{}` —— 检查自己
    红了一次,报的是"服务端没注册 /history"）。
    """
    without_js = re.sub(r"\$\{[^}]*\}", "{}", path)
    return re.sub(r"\{[^}]*\}", "{}", without_js).rstrip("/")


@check("前端清单与后端路由表对得上（每条 ${API_PREFIX} 路径都注册了；WS 那条真的是 WS 路由）")
def _() -> str:
    """`endpoints.ts` 说"路径与 app.py 一一对应" —— 这条把那句注释变成可执行断言。

    它接的是**被删掉的内置调试页**留下的班：以前靠"页面源码里有 `/api/v1/stream`"来证明
    "页面连的是服务端那条 WS",判据其实是字符串搜索,而路径写错的典型形态
    (前缀对、尾巴错)照样能过。改成读前端的清单、去后端路由表里逐条对形状。
    """
    from fastapi.routing import APIWebSocketRoute

    from project_rag.api import create_app

    endpoints = (WEB_SRC / "api" / "endpoints.ts").read_text(encoding="utf-8")
    frontend = {_shape(hit) for hit in _FRONTEND_PATH.findall(endpoints)}
    if len(frontend) < 10:
        raise AssertionError(
            f"只从 endpoints.ts 里认出 {len(frontend)} 条路径:{sorted(frontend)} —— 解析塌了？"
        )

    # 直接建 app 而不是 `client().app`:后者在 pyright 眼里只是 ASGIApp,取不到 `.routes`
    app = create_app(_World().deps)
    backend: dict[str, type] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/v1"):
            backend[_shape(path.removeprefix("/api/v1"))] = type(route)

    missing = sorted(frontend - set(backend))
    if missing:
        raise AssertionError(
            f"前端在打这些路径,而服务端没注册:{missing}（运行时就是 404/连接被拒）"
        )

    # WS 那条单独钉:路径对了但注册成 HTTP 路由,页面照样连不上,而服务端一切正常
    stream = re.search(r"export const STREAM_PATH = `\$\{API_PREFIX\}([^`]*)`", endpoints)
    if stream is None:
        raise AssertionError("endpoints.ts 里没有 `export const STREAM_PATH = `${API_PREFIX}…``")
    stream_path = _shape(stream.group(1))
    route_type = backend.get(stream_path)
    if route_type is None or not issubclass(route_type, APIWebSocketRoute):
        kind = route_type.__name__ if route_type is not None else "没注册"
        raise AssertionError(
            f"STREAM_PATH 指向 {stream_path},而它在服务端是 {kind} —— 必须是 WebSocket 路由"
        )
    return f"{len(frontend)} 条路径全部对得上；{stream_path} 是 WS 路由"


@check("能力不写死在前端：后缀表 / 检索方式 / 模型名一律来自 capabilities")
def _() -> str:
    # 判据是**数组/集合字面量**:`['vector','keyword','hybrid']`、`['.pdf','.md']` 这种
    # "把后端的闭集抄一份"。类型里的联合类型(`type M = 'vector' | 'keyword'`)不算 ——
    # 那是编译期镜像,运行时的选项仍然来自 capabilities。
    literal = re.compile(
        r"\[\s*['\"](?:vector|keyword|hybrid|rrf|weighted|\.pdf|\.docx|\.md|\.txt)['\"]"
    )
    offenders: dict[str, list[str]] = {}
    for name, text in _sources().items():
        stripped = _strip_comments(text)
        found = []
        if literal.search(stripped):
            found.append("能力闭集的字面量数组")
        if re.search(r"deepseek", stripped, flags=re.I):
            found.append("写死的模型名")
        if re.search(r"accept=[\"'][^\"']*\.(pdf|docx|md|txt)", stripped):
            found.append("写死的 accept")
        if found:
            offenders[name] = found
    if offenders:
        raise AssertionError(
            f"这些地方把后端能力写死了：{offenders}；写死的后果是后端加了东西前端不知道"
            "（或反过来：界面允许、后端拒绝）"
        )
    return "0 处能力闭集字面量 / 0 处写死模型名 / 0 处写死 accept"


@check("角色分流：页签按能力渲染（对话/知识库/报表/账号），路由按能力拦")
def _() -> str:
    """P0-6:导航不再按"是不是管理员"分两档,而是按**能力**分四档 ——
    运维看到「对话 + 报表」(他也要能问答),员工看到「对话 + 知识库」,管理员全都看到。

    这几条断言盯的是"三件事必须同时成立",漏一件就会出现
    "藏了页签但路由放行"(点 URL 能进)或"拦了路由但页签还在"(点了必然 403):
    ① 页签用的那三个 computed 真的存在且来自 `/auth/me`;② 路由 meta 按能力标;
    ③ 守卫真的用 `NEEDS` 判定,而且进不去时有落地页。
    """
    header = (WEB_SRC / "components" / "AppHeader.vue").read_text(encoding="utf-8")
    # **逐个页签**核对它那一行上的守卫,不是"整个文件里出现过就算" ——
    # 后者会被"另一个页签也用了同一个能力"蒙混过去(反向测试 RT-8 抓出来的:
    # 把知识库页签的 v-if 去掉,文件里仍有 v-if="canUseKnowledge",检查照样绿)。
    wanted = {
        "chat": ("canUseKnowledge", "对话:员工与管理员"),
        "knowledge": ("isAdmin", "知识库:页面里有上传/删除/授权,所以只有管理员"),
        "reports": ("canViewReports", "报表:运维与管理员"),
        "accounts": ("isAdmin", "账号与权限:只有管理员"),
    }
    for name, (guard, what) in wanted.items():
        line = next(
            (item for item in header.splitlines() if f":to=\"{{ name: '{name}' }}\"" in item),
            None,
        )
        if line is None:
            raise AssertionError(f"顶部导航里没有指向 {name} 的页签")
        if f'v-if="{guard}"' not in line:
            raise AssertionError(
                f"{what}这个页签该用 v-if=\"{guard}\" 守住,实际这一行是:{line.strip()}"
            )
    router_text = (WEB_SRC / "router.ts").read_text(encoding="utf-8")
    for need in ("'knowledge'", "'reports'", "'manage'"):
        if need not in router_text:
            raise AssertionError(f"router.ts 里没有按能力标记路由(缺 needs: {need})")
    if "NEEDS[need]" not in router_text:
        raise AssertionError("router.ts 的守卫没有按能力判定（标了却不用 = 没拦）")
    if "landingRoute" not in router_text:
        raise AssertionError(
            "router.ts 没有落地页逻辑:运维打不开 /chat,不给去处会绕成空白页"
        )
    auth_text = (WEB_SRC / "stores" / "auth.ts").read_text(encoding="utf-8")
    for field in ("can_view_reports", "can_use_knowledge", "can_manage"):
        if field not in auth_text:
            raise AssertionError(f"stores/auth.ts 没有用后端报的能力字段 {field}（那是唯一真源）")
    return "导航 3 档能力页签 + 路由 needs 标记 + 按能力判定 + 落地页"


@check("WebSocket 带 ?token=（浏览器不能给 WS 设头，这是唯一一条令牌进 URL 的路）")
def _() -> str:
    stream = (WEB_SRC / "api" / "stream.ts").read_text(encoding="utf-8")
    if "wsUrl(" not in stream or "token()" not in stream:
        raise AssertionError("stream.ts 建连接时没把令牌交出去")
    client = (WEB_SRC / "api" / "client.ts").read_text(encoding="utf-8")
    if "?token=" not in client or "encodeURIComponent" not in client:
        raise AssertionError("client.ts 的 wsUrl 没拼 ?token=（或没转义）")
    if "1008" not in stream:
        raise AssertionError("stream.ts 没处理 close(1008)：令牌过期时用户只会看到「连接中断」")
    return "wsUrl 拼 ?token=；1008 单独处理"


@check("后端地址是这个 app 唯一的环境（源码里没有写死的地址）")
def _() -> str:
    client = (WEB_SRC / "api" / "client.ts").read_text(encoding="utf-8")
    for marker in ("BACKEND_KEY", "setBackendBase", "defaultBackend"):
        if marker not in client:
            raise AssertionError(f"client.ts 里没有 {marker}：后端地址没法改 / 没法默认")
    hardcoded = [
        name
        for name, text in _sources().items()
        if re.search(r"https?://(127\.0\.0\.1|localhost)", _strip_comments(text))
    ]
    if hardcoded:
        raise AssertionError(
            f"这些文件写死了后端地址：{hardcoded}；开发态的默认值放 web/.env.development"
        )
    return "地址只从 localStorage / 本页 origin 来；源码 0 处写死"


@check("四种「不是正常答案」的状态各有话术（拒答/截断/停止/错误不合并成一句「出错了」）")
def _() -> str:
    messages = (WEB_SRC / "components" / "MessageList.vue").read_text(encoding="utf-8")
    missing = [
        marker
        for marker in ("message.refused", "message.truncated", "message.stopped", "message.error")
        if marker not in messages
    ]
    if missing:
        raise AssertionError(f"MessageList.vue 少了这些状态分支：{missing}")
    if "页码未知" not in (WEB_SRC / "components" / "CitationItem.vue").read_text(encoding="utf-8"):
        raise AssertionError(
            "CitationItem.vue 没有「页码未知」分支：page=null 是「不知道」，不许拿 0/1 顶上"
        )
    return "拒答 / 截断 / 停止 / 错误 / 页码未知 五个分支都在"


# ══ 二、接口行为（TestClient + 假件） ═════════════════════════════════════════
@check("capabilities 报的检索闭集与领域层一致（caps 与代码不许各说一套）")
def _() -> str:
    from project_rag.domain import FUSION_MODES, RETRIEVAL_MODES
    from project_rag.domain.auth import USER_ROLES

    body = _World().client().get("/api/v1/capabilities").json()
    assert body["retrieval"]["modes"] == list(RETRIEVAL_MODES), body["retrieval"]["modes"]
    assert body["retrieval"]["fusions"] == list(FUSION_MODES), body["retrieval"]["fusions"]
    assert body["roles"] == list(USER_ROLES), body["roles"]
    assert [model["id"] for model in body["models"]] == ["deepseek-flash"], body["models"]
    assert body["retrieval"]["defaults"]["fusion"] in FUSION_MODES
    assert body["retrieval"]["rerank_available"] is True
    return f"modes={body['retrieval']['modes']} fusions={body['retrieval']['fusions']} 模型 1 个"


@check("capabilities 的 limits 与 QueryRequest 的边界是同一批常量（前端夹取不会夹到 422）")
def _() -> str:
    from pydantic import ValidationError

    from project_rag.api.schemas import K_MAX, MAX_CONTEXT_MAX, RRF_K_MAX, QueryRequest

    limits = _World().client().get("/api/v1/capabilities").json()["retrieval"]["limits"]
    assert limits == {"k_max": K_MAX, "max_context_max": MAX_CONTEXT_MAX, "rrf_k_max": RRF_K_MAX}
    QueryRequest(query="x", k=K_MAX, max_context=MAX_CONTEXT_MAX, rrf_k=RRF_K_MAX)
    for field, too_big in (
        ("k", K_MAX + 1),
        ("max_context", MAX_CONTEXT_MAX + 1),
        ("rrf_k", RRF_K_MAX + 1),
    ):
        try:
            # 用 model_validate 而不是 QueryRequest(**{field: …}):动态字段名展开会让
            # pyright 拿这个 int 去逐个匹配所有参数（29 条假错误）
            QueryRequest.model_validate({"query": "x", field: too_big})
        except ValidationError:
            continue
        raise AssertionError(f"{field}={too_big} 竟然通过了：上限报了却不拦 = 前端会夹到 422")
    return f"limits 与 Field(le=) 同源：k≤{K_MAX} / max_context≤{MAX_CONTEXT_MAX} / rrf_k≤{RRF_K_MAX}"


@check("mode 翻译成权重：vector 不跑关键字那一路，keyword 不跑向量那一路（用调用计数断言）")
def _() -> str:
    world = _World()
    client = world.client()
    body = {"query": "加班上限", "rerank": False}

    assert client.post("/api/v1/query", json={**body, "mode": "vector"}).status_code == 200
    assert (world.vector.searches, world.keyword.searches) == (1, 0), (
        f"mode=vector 时 vector/keyword 各跑了 {world.vector.searches}/{world.keyword.searches} 次"
    )

    assert client.post("/api/v1/query", json={**body, "mode": "keyword"}).status_code == 200
    assert (world.vector.searches, world.keyword.searches) == (1, 1), (
        f"mode=keyword 时 vector/keyword 各跑了 {world.vector.searches}/{world.keyword.searches} 次"
        "（向量那一路不该再跑：白跑一趟账本上 embed/vector 两段就都在骗人）"
    )

    assert client.post("/api/v1/query", json={**body, "mode": "hybrid"}).status_code == 200
    assert (world.vector.searches, world.keyword.searches) == (2, 2)
    return "vector: 1/0；keyword: 1/1；hybrid: 2/2（计数只增不减）"


@check("mode 与权重不能同时给 → 422；两路权重同时 0 → 422（两个真值来源不许并存）")
def _() -> str:
    client = _World().client()
    conflict = client.post(
        "/api/v1/query", json={"query": "x", "mode": "vector", "vector_weight": 0.9}
    )
    assert conflict.status_code == 422, conflict.text
    assert "不能同时给" in conflict.text, conflict.text
    empty = client.post("/api/v1/query", json={"query": "x", "bm25_weight": 0, "vector_weight": 0})
    assert empty.status_code == 422, empty.text
    assert "不能同时为 0" in empty.text, empty.text
    return "两种冲突都在出参层被拦（422 + 人话），不会伪装成「知识库里没有」"


@check("_options：请求给了用请求的，没给用配置的（默认值只有一处来源）")
def _() -> str:
    from project_rag.config import get_settings

    rag = get_settings().rag
    default = _options_for()
    assert (default.k, default.max_context) == (rag.retrieval_k, rag.candidate_m)
    assert (default.fusion, default.rrf_k) == (rag.fusion, rag.rrf_k)

    asked = _options_for(k=9, max_context=3, fusion="weighted", rrf_k=7, source_filter="bagu_ai")
    assert (asked.k, asked.max_context) == (9, 3)
    assert (asked.fusion, asked.rrf_k, asked.source) == ("weighted", 7, "bagu_ai")

    vector_only = _options_for(mode="vector")
    assert (vector_only.bm25_weight, vector_only.vector_weight) == (0.0, rag.vector_weight)
    keyword_only = _options_for(mode="keyword")
    assert (keyword_only.bm25_weight, keyword_only.vector_weight) == (rag.bm25_weight, 0.0)
    return f"默认 k={rag.retrieval_k} fusion={rag.fusion}；mode 翻译成权重 0.0/… 两向都对"


@check("rerank=true 而没装重排器 → 400，话术点名去掉哪个字段；不勾则压根不构造重排器")
def _() -> str:
    world = _World(reranker=False)
    client = world.client()
    forced = client.post("/api/v1/query", json={"query": "x", "rerank": True})
    assert forced.status_code == 400, forced.text
    detail = forced.json()["detail"]
    assert "rerank" in detail and "去掉" in detail, f"话术不可执行：{detail}"

    # 缺省 rerank 时按配置来：测试配置里 rag.rerank=true,而这里没装重排器 →
    # `knowledge_callable` 的 _required 会抛,这正是"配置说开、部署没装"该有的样子
    from project_rag.config import get_settings

    if get_settings().rag.rerank:
        off = _World(reranker=True)
        assert off.client().post("/api/v1/query", json={"query": "x", "rerank": False}).status_code == 200
        assert off.reranker.calls == 0, "rerank=false 竟然还构造了重排器"
        on = _World(reranker=True)
        assert on.client().post("/api/v1/query", json={"query": "x", "rerank": True}).status_code == 200
        assert on.reranker.calls == 1, "rerank=true 没有构造重排器"
        shown = get_settings().rag.rerank
    else:
        shown = get_settings().rag.rerank
    return f"400 话术可执行；rerank=false 构造 0 次 / true 构造 1 次（配置默认={shown}）"


@check("sessions：无认证给空表、认证按账号过滤、没装目录 → 503、**存储层坏掉给人话**")
def _() -> str:
    anonymous = _World(auth=False, catalog=False).client().get("/api/v1/sessions")
    assert anonymous.status_code == 200 and anonymous.json() == {"sessions": []}, anonymous.text

    missing = _World(auth=True, catalog=False)
    token = missing.token("admin")
    response = missing.client().get(
        "/api/v1/sessions", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 503, response.text

    world = _World(auth=True)
    admin = world.token("admin")
    employee = world.token("employee")
    admin_rows = world.client().get(
        "/api/v1/sessions", headers={"Authorization": f"Bearer {admin}"}
    ).json()["sessions"]
    employee_rows = world.client().get(
        "/api/v1/sessions", headers={"Authorization": f"Bearer {employee}"}
    ).json()["sessions"]
    assert [row["session_id"] for row in admin_rows] == ["s-admin-1", "s-admin-2"], admin_rows
    assert [row["session_id"] for row in employee_rows] == ["s-emp-1"], employee_rows
    assert world.catalog.calls[0][0] == "u-admin", world.catalog.calls
    fields = set(admin_rows[0])
    assert fields == {"session_id", "title", "rounds", "created_at", "updated_at"}, fields

    # 存储层坏掉时要说人话:这条路径以前把 MySQL 的异常直接抛给 FastAPI,前端拿到的是
    # 一句 "Internal Server Error" —— 用户看到的是"会话都没了",而真正的原因(作者那次是
    # 两族排序规则 JOIN 报 1267)一个字都没露出来
    broken = _World(auth=True)
    broken_token = broken.token("admin")

    def _boom(user_id: str, *, limit: int):  # noqa: ANN202 - 假件
        raise RuntimeError("(1267, \"Illegal mix of collations\")")

    broken.catalog.list_for = _boom  # type: ignore[method-assign]
    failed = broken.client().get(
        "/api/v1/sessions", headers={"Authorization": f"Bearer {broken_token}"}
    )
    assert failed.status_code == 503, failed.text
    detail = failed.json()["detail"]
    assert "读不到会话目录" in detail and "1267" in detail, detail
    return f"空表/503/过滤三态都在;存储层坏掉 → 503「{detail[:40]}…」"


@check("会话标题的裁法只在领域层一处：压成一行、超 40 字加省略号")
def _() -> str:
    from project_rag.domain.sessions import TITLE_CHARS, session_title

    assert TITLE_CHARS == 40
    assert session_title("  加班\n上限   是多少  ") == "加班 上限 是多少", session_title("  加班\n上限   是多少  ")
    long = "问" * (TITLE_CHARS + 5)
    trimmed = session_title(long)
    assert len(trimmed) == TITLE_CHARS + 1 and trimmed.endswith("…"), len(trimmed)
    assert session_title("短") == "短"
    return f"空白压平 + 裁到 {TITLE_CHARS} 字加省略号"


@check("根路径跳前端 app；static/ 下只剩这一份界面（旧调试页 已删）")
def _() -> str:
    client = _World().client()
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307, root.text
    assert root.headers["location"] == "/static/app/", root.headers["location"]

    page = client.get("/static/app/")
    assert page.status_code == 200, page.text
    assert 'id="app"' in page.text, "产物 index.html 里没有挂载点 #app"

    # 旧的内置调试页（101 行的 `static/index.html`）删掉之后，`static/` 下不许再出现第二份
    # 页面：两份 UI 的代价是两套要跟着后端一起改的东西，而没人日常打开的那一份漂了也不会
    # 有人发现 —— 它当年就比新前端晚半天才跟上 `/api/v1` 迁移。这条是防它（或它的同类）
    # 被"顺手"加回来：文件级扫一遍，再对 HTTP 断言一次 404。
    strays = sorted(
        str(path.relative_to(STATIC))
        for path in STATIC.rglob("*.html")
        if path.is_file()
    )
    assert strays == ["app/index.html"], f"static/ 下出现了别的页面（界面只留一份）：{strays}"
    gone = client.get("/static/index.html")
    assert gone.status_code == 404, "旧调试页又回来了 —— 界面只留一份"
    return "307 → /static/app/；static/ 下只有 app/index.html"


def main() -> int:
    return report(
        "M7.1 前端（聊天页）+ capabilities / 检索旋钮 / 会话目录",
        hint="前端改动后先 `cd web && npm run build`；产物要入库（Python 侧与镜像都不装 node）",
    )


if __name__ == "__main__":
    sys.exit(main())

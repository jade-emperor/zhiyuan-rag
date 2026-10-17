#!/usr/bin/env python
"""M5.4 验收：FastAPI 接口层 —— 当时那 9 条路由 + 问候拦截 + 静态文件;
P0-3 之后又钉上认证/角色/文档治理/命名空间(业务路由一律 `/api/v1`)。

用法::

    uv run python scripts/acceptance/check_m5_api.py

本脚本是 **M5.4 契约的唯一权威**（`docs/STATUS.md` §0.1 / §3 只留设计理由）。

**不需要起服务器、不需要容器、不花钱**:全程用 FastAPI 的 ``TestClient`` 打真请求
（HTTP + WebSocket),依赖用假件注入 —— 和 CLI 那套注入方式一样。

公开契约::

    from project_rag.api import create_app
    create_app(deps: Deps | None = None) -> FastAPI
        # 传 deps = 不读 .env、不连任何东西;不传 = build_real_deps()
        # 刻意不在模块级建 app:import 一个模块不该有副作用(读配置、要密钥)

    from project_rag.application.greeting import match_greeting
    match_greeting(text) -> str | None      # 整句只是寒暄时给模板话术

端点(9 个)::

    POST   /api/v1/create_session        -> {"session_id": uuid4}
    GET    /api/v1/history/{session_id}  -> {"session_id", "rounds": [{question, answer, citations}]}
    DELETE /api/v1/history/{session_id}  -> {"session_id", "deleted": int}
    POST   /api/v1/query                 -> QueryResponse(非流式)
    WS     /api/v1/stream                -> 逐帧 {type: sources|token|done|error}
    GET    /health                    -> {"status": "ok"}
    GET    /api/v1/sources               -> {"sources": [...]}
    GET    /                          -> 静态首页
    GET    /static/{path}             -> 静态文件挂载

**四件刻意不同的事**（每条都对着一个容易踩的坑）::

    1. 端点全是 **async def**,但同步的活全在 ``await`` 后面 —— 两道缝:``_call`` 搬
       「一次同步调用」、``_in_thread`` 搬「一整条同步帧流」,都进 anyio 的线程池。
       常见写法是「async def 里直接跑同步 pymysql/openai」:一个请求卡住事件循环、
       并发时全排队。本项目 ports 是同步的(pymysql / pymilvus / langchain / torch),
       所以纯异步的正确形态就是「协程外壳 + 单一线程池缝」。验收两条:
       ① 从 ``app.routes`` 上检查**每个 HTTP/WS 端点都是协程函数**(不许退回同步 def);
       ② **行为检查**:慢依赖在跑时后台协程仍在推进 —— 把同步代码写回协程里,这条会红。
    2. POST /api/v1/query **直接给完整答案**,不要求前端换 WebSocket 才拿得到结果。
       我们有同步用例,强制前端换协议是更差的契约。
    3. **路由只有一处**:HTTP/WS 都调 answer_question / stream_question。绕开用例直接查库的
       那条路径会漏掉「写历史」这一步(FAQ 命中的答案压根不进历史)—— 本脚本有一条检查专门钉
       「FAQ 命中的那一轮也进了历史」。
    4. **问候命中不关连接**:``break`` 掉整条连接用户就得重连;这里发完话术继续等下一条
       (检查:同一条 WS 上先发问候、再发问题,两次都要有回复)。

请假名单之外的两条口径:``/api/v1/sources`` 读**向量库里的真实 source**(不是配置白名单);
``channel`` 比 domain 多一个 ``"greeting"`` —— 寒暄既不是 FAQ 通道也不是知识库通道,
混进那两种前端就没法区分了。

起服务那条也钉住::

    run()  ← 必须 configure_logging() → _warmup(deps) → uvicorn.run(host=api.host, port=api.port)
        # 日志装在这里、不装进 create_app():工厂要纯粹,起服务才是进程入口
        # 预热也在这里:把模型/索引/连接的冷启动从「第一个用户」挪到启动(M5.6,实测 4.4s)
        # 绑 0.0.0.0 而不是回环:WSL 里绑 127.0.0.1 时 Windows 浏览器连不上(实测),
        # 进容器后绑回环也映射不出去。**地址本身走配置**(api.host/port),类默认仍是
        # 0.0.0.0:8080 —— 这条检查比对的是「run() 读到的是配置值」,不是某个具体地址。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import io
import re
import sys
import time
import typing
import uuid
from pathlib import Path

from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from _harness import check, report
from _fakes import FakeConversationStore, FakeEmbedder, FakeFaqStore, FakeKeywordIndex, FakeLLM, FakeMatcher, FakeReranker, FakeVectorStore

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

FAQ_QUESTION = "退款流程怎么走？"
FAQ_ANSWER = "在订单页点「申请退款」，1-3 个工作日到账。"
KB_PARENT = "装饰器是一个接收函数并返回新函数的函数。"
KB_PARTS = ("装饰器是", "给函数加行为的语法 [1]")
KB_HALF = "装饰器是给函数加行为的语法"
REFUSAL = "信息不足,无法回答,请联系人工客服。"
_SLOW_SECONDS = 0.3  # 假依赖在池子里睡多久
_TICK_SECONDS = 0.005  # 后台协程多久推进一次


def _require(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(f"{module_name} 缺少公开成员 {missing}")
    return module


def _create_app():
    return _require("project_rag.api", ("create_app",)).create_app


def _match_greeting():
    return _require("project_rag.application.greeting", ("match_greeting",)).match_greeting


def _imports_of(path: Path) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            pairs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            pairs.append((node.module or "", node.level))
    return pairs


# ── 假件（不起服务器、不连库、不联网） ───────────────────────────────────────


class _SlowFaqStore(FakeFaqStore):
    """``load_all`` 里睡一会儿 —— 用来判断「请求卡在同步依赖里」时事件循环还转不转。

    它不是模拟慢查询的性能,只是把「一段同步阻塞」钉在已知位置:如果端点把这段
    同步代码写在协程里直接调,事件循环就会停摆 ``_SLOW_SECONDS``。
    """

    def load_all(self):
        time.sleep(_SLOW_SECONDS)
        return super().load_all()


def _kb_hit(source: str = "ai", parent: str = KB_PARENT):
    from project_rag.domain import Chunk, RetrievedChunk

    # page=7 是 P0-5 加的:让「页码一路走到 HTTP 出参」这条链在接口这一层也有断言。
    # 夹具刻意给一个**真实页号**(而不是留 None),否则「忘了从 Citation 抄到 CitationOut」
    # 会表现成 null —— 而 null 看起来完全合法(它本来就表示"不知道")。
    return RetrievedChunk(
        chunk=Chunk("c1", "装饰器 子块", source, "d1", "p1", parent, page=7), score=0.91
    )


def _faq_entry():
    FaqEntry = _require("project_rag.domain", ("FaqEntry",)).FaqEntry
    return FaqEntry(id=7, question=FAQ_QUESTION, answer=FAQ_ANSWER)


class _World:
    """一套假依赖 + 记录，装成一个可注入的 Deps。"""

    def __init__(
        self,
        *,
        faq_score: float | None = None,
        pieces=KB_PARTS,
        finish_reason: str = "stop",
        hits=None,
        turns=None,
    ) -> None:
        self.llm = FakeLLM(pieces, finish_reason=finish_reason)
        self.embedder = FakeEmbedder()
        self.vector_store = FakeVectorStore([_kb_hit()] if hits is None else hits)
        self.keyword_index = FakeKeywordIndex()
        self.reranker = FakeReranker()
        self.faq_store = FakeFaqStore([_faq_entry()])
        self.matcher = FakeMatcher(faq_score)
        self.conversation = FakeConversationStore(turns)

    def deps(self, **overrides):
        from project_rag.cli import Deps

        fields = {
            "llm_factory": lambda: self.llm,
            "faq_store_factory": lambda: self.faq_store,
            "matcher_factory": lambda: self.matcher,
            "cache_factory": None,
            "faq_threshold": 0.85,
            "embedder_factory": lambda: self.embedder,
            "vector_store_factory": lambda: self.vector_store,
            "keyword_index_factory": lambda: self.keyword_index,
            "reranker_factory": lambda: self.reranker,
            "conversation_store_factory": lambda: self.conversation,
        }
        fields.update(overrides)
        return Deps(**fields)

    def app(self, **overrides) -> typing.Any:
        """输入:工厂覆盖。行为:建一个注入了假依赖的应用。输出:FastAPI 应用。

        返回 Any 是刻意的:``TestClient.app`` 的静态类型是 ``_WrapASGI2``,拿不到 ``routes``;
        要检查路由表就得直接拿 create_app 的返回值。
        """
        return _create_app()(self.deps(**overrides))

    def client(self, **overrides) -> TestClient:
        return TestClient(self.app(**overrides))


# ── 0. 脚本自身 ───────────────────────────────────────────────────────────────
@check("脚本自身：检查不许引用后文定义的帮助函数（@check 在定义时就立即执行）")
def _() -> str:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    defined: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            defined[node.name] = node.lineno
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        title = ""
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                title = str(decorator.args[0].value)
            elif isinstance(decorator, ast.Name):
                title = decorator.id
        if not title:
            continue
        used = {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
        }
        late = [
            (name, defined[name])
            for name in sorted(used & defined.keys())
            if defined[name] > node.lineno
        ]
        if late:
            where = "、".join(f"{name}@{line}" for name, line in late)
            problems.append(f"「{title}」(第 {node.lineno} 行) 用了后文定义的 {where}")
    if problems:
        raise AssertionError(
            "这些检查引用了**后文**才定义的帮助函数：\n    " + "\n    ".join(problems)
        )
    return f"{len(defined)} 个顶层定义全部先于引用它的检查"


# ── 1. 工厂与应用形状 ─────────────────────────────────────────────────────────
@check("工厂：create_app(deps) 不读 .env、不连任何东西，返回 FastAPI；模块级没有 app")
def _() -> str:
    from fastapi import FastAPI

    world = _World()
    app = _create_app()(world.deps())
    if not isinstance(app, FastAPI):
        raise AssertionError(f"create_app 应返回 FastAPI，实际 {type(app)}")
    module = importlib.import_module("project_rag.api.app")
    if hasattr(module, "app"):
        raise AssertionError(
            "api/app.py 模块级建了 app —— import 一个模块不该读配置、要密钥;"
            "应用实例由调用方建(生产在 run() 里建)"
        )
    return "工厂可用，模块级无副作用"


@check("起服务：装日志 → 预热 → 按配置绑地址起 uvicorn；工厂保持纯粹（不装日志）")
def _() -> str:
    module = importlib.import_module("project_rag.api.app")
    observability = importlib.import_module("project_rag.observability")
    for name in ("configure_logging", "_warmup"):
        if name in inspect.getsource(module.create_app):
            raise AssertionError(
                f"create_app() 里不该出现 {name} —— 工厂要纯粹:测试注入假依赖时,"
                "不该顺手改全局日志、更不该去加载真模型"
            )
    if not hasattr(module, "_warmup"):
        raise AssertionError("api/app.py 少了 _warmup —— 首个请求就要自己扛 4.4s 的冷启动")

    order: list[str] = []
    seen: dict[str, typing.Any] = {}
    real_configure = observability.configure_logging
    real_warmup = module._warmup
    import uvicorn

    real_run = uvicorn.run

    def fake_configure(*args, **kwargs) -> None:
        order.append("logging")

    def fake_warmup(deps) -> None:
        order.append("warmup")

    def fake_run(app, **kwargs) -> None:
        order.append("serve")
        seen.update(kwargs)

    # 用 setattr 打桩:直接赋值会被 pyright 按原签名挑刺(和 ports 的假件同一个理由)
    setattr(observability, "configure_logging", fake_configure)
    setattr(module, "_warmup", fake_warmup)
    setattr(uvicorn, "run", fake_run)
    try:
        module.run()
    finally:
        setattr(observability, "configure_logging", real_configure)
        setattr(module, "_warmup", real_warmup)
        setattr(uvicorn, "run", real_run)
    if order != ["logging", "warmup", "serve"]:
        raise AssertionError(
            f"run() 的顺序应是「装日志 → 预热 → 起服务」,实际 {order} —— "
            "漏装日志:API 进程里除了 uvicorn 的访问日志业务日志一条都留不下(实测);"
            "漏预热:首个请求比热请求多付 4.4s(实测)"
        )
    # 绑定地址改成从配置读了,所以这里**比对配置而不是比对字面量**:
    # 断字面量会把「作者改了 RAG_API__HOST 就红」变成假故障(法则 #26);
    # 断「run() 读到的是配置里的值」才既挡住漏读、又不管作者选哪个地址。
    from project_rag.config import ApiConfig, get_settings

    settings = get_settings()
    if ApiConfig().host != "0.0.0.0" or ApiConfig().port != 8080:
        raise AssertionError(
            "ApiConfig 的**类默认值**该是 0.0.0.0:8080 —— 绑回环时 WSL 里 Windows 浏览器"
            f"连不上、进容器也映射不出去。实际 {ApiConfig().host}:{ApiConfig().port}"
        )
    if seen.get("host") != settings.api.host or seen.get("port") != settings.api.port:
        raise AssertionError(
            f"run() 应把 uvicorn 绑到配置里的 {settings.api.host}:{settings.api.port},实际 {seen} —— "
            "漏读配置 = 改 RAG_API__HOST / RAG_API__PORT 不生效"
        )
    return (
        f"run(): configure_logging() → _warmup(deps) → "
        f"uvicorn.run({settings.api.host}:{settings.api.port})"
    )


@check("路由清单：M5 那 9 条（8 条业务路由 + 1 个静态挂载）都还在 —— 改名不改行为")
def _() -> str:
    app = _World().app()
    routes = {
        (route.path, frozenset(getattr(route, "methods", None) or []))
        for route in app.routes
        if isinstance(route, APIRoute)
    }
    ws = {route.path for route in app.routes if isinstance(route, APIWebSocketRoute)}
    mounts = {route.path for route in app.routes if isinstance(route, Mount)}

    want = {
        ("/api/v1/create_session", frozenset({"POST"})),
        ("/api/v1/history/{session_id}", frozenset({"GET"})),
        ("/api/v1/history/{session_id}", frozenset({"DELETE"})),
        ("/api/v1/query", frozenset({"POST"})),
        ("/health", frozenset({"GET"})),
        ("/api/v1/sources", frozenset({"GET"})),
        ("/", frozenset({"GET"})),
    }
    missing = want - routes
    if missing:
        raise AssertionError(f"缺这些路由：{sorted(missing)}；实际 {sorted(routes)}")
    if ws != {"/api/v1/stream"}:
        raise AssertionError(f"WebSocket 路由应为 /api/v1/stream，实际 {ws}")
    if "/static" not in mounts:
        raise AssertionError(f"静态文件没挂上，实际 mount {mounts}")
    return f"M5 那 {len(want)} 条路由都在（另有 P0-3 的 v1 端点，由「命名空间」那条统一盯）"


@check("命名空间：业务接口一律在 /api/v1 下（老 /api/* 一条不留，也不留别名）")
def _() -> str:
    """P0-3b-5-② 的统一迁移：`/api/*` → `/api/v1/*`，metric 挪进 `admin`。

    **为什么要一条检查盯着**：漏一个老式路径进去，前端的 TS 客户端（从 OpenAPI 生成，
    见 `deferred.md` 第 55 条）就会有两个前缀；而**留别名更糟** —— 两套路径是两个真值来源，
    契约要写两遍、认证要接两处，然后总有一套先烂。

    **允许留在根上的只有三样**，它们不是业务接口、也不跟 API 版本走：
    `/health`（探活，编排系统要用）、`/`（307 跳前端 app）、`/static`（前端产物的资源）。
    """
    app = _World().app()
    paths = {
        route.path
        for route in app.routes
        if isinstance(route, (APIRoute, APIWebSocketRoute))
    }
    root_ok = {"/health", "/"}
    stray = sorted(
        path
        for path in paths
        if path not in root_ok and not path.startswith("/api/v1/")
    )
    if stray:
        legacy = [path for path in stray if path.startswith("/api/")]
        hint = (
            "（它们看着像老接口：别留别名，别名 = 两个真值来源）" if legacy else ""
        )
        raise AssertionError(
            f"这些路由不在 /api/v1 下：{stray}{hint}；根上只允许 {sorted(root_ok)}"
        )
    business = sorted(path for path in paths if path.startswith("/api/v1/"))
    if len(business) < 10:
        raise AssertionError(
            f"只找到 {len(business)} 条 v1 路由，太少（像是装载错了 app）：{business}"
        )
    return f"{len(business)} 条业务路由全在 /api/v1 下；根上只有 {sorted(root_ok)}"


@check("并发姿势：HTTP 与 WS 端点全是 coroutine function（这一层没有同步端点）")
def _() -> str:
    app = _World().app()
    sync_paths = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and not inspect.iscoroutinefunction(route.endpoint)
    ]
    if sync_paths:
        raise AssertionError(
            f"这些 HTTP 端点还是同步 def：{sync_paths}。"
            "这一层统一写成 async def,同步的活交给 _call / _in_thread 进线程池 —— "
            "不能靠 FastAPI 给 def 端点自动开线程(两条入口两种姿势,迟早不一致)"
        )
    ws_routes = [route for route in app.routes if isinstance(route, APIWebSocketRoute)]
    if not ws_routes or not inspect.iscoroutinefunction(ws_routes[0].endpoint):
        raise AssertionError("WS 端点应是 async def(它要用 _in_thread 逐帧搬同步流)")
    http_count = len([r for r in app.routes if isinstance(r, APIRoute)])
    return f"{http_count} 条 HTTP + 1 条 WS 端点全是协程"


@check("事件循环：慢依赖在跑时后台协程仍在推进（async 里塞同步代码会让它停摆）")
def _() -> str:
    import asyncio

    import httpx

    async def scenario() -> tuple[int, bool, int]:
        app = _World().app(faq_store_factory=lambda: _SlowFaqStore([_faq_entry()]))
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(_TICK_SECONDS)
                ticks += 1

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            beat = asyncio.create_task(ticker())
            call = asyncio.create_task(
                client.post("/api/v1/query", json={"query": "装饰器有什么用"})
            )
            await asyncio.sleep(_SLOW_SECONDS / 2)  # 这一刻请求正卡在慢依赖里
            during = ticks
            still_running = not call.done()
            response = await call
            beat.cancel()
        return during, still_running, response.status_code

    started = time.perf_counter()
    during, still_running, status = asyncio.run(scenario())
    elapsed = time.perf_counter() - started
    if during < 5:
        raise AssertionError(
            f"事件循环被占住了：慢依赖跑 {_SLOW_SECONDS}s,期间后台协程只推进 {during} 次"
            f"(每 {_TICK_SECONDS}s 一次,共 {elapsed:.2f}s)。端点是 async def 却直接调用了"
            "同步代码 —— 一个请求就能卡住事件循环;同步的活要 await _call(...) 交进线程池"
        )
    if not still_running:
        raise AssertionError(
            "慢依赖没起作用:请求在采样时就已经结束,这条检查判定不了任何东西(检查本身失效了)"
        )
    if status != 200:
        raise AssertionError(f"被慢依赖拖住的那条请求本身应正常返回,实际 HTTP {status}")
    return f"慢依赖 {_SLOW_SECONDS}s 内后台协程推进 {during} 次(循环没被占住)"


@check("分层：只有 api/ 能 import fastapi/starlette；application/ports/domain/adapters 都不许")
def _() -> str:
    leaks = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if path.parts[-2] != "api" or path.name == "__init__.py"
        for module, level in _imports_of(path)
        if level == 0 and module.split(".")[0] in ("fastapi", "starlette", "uvicorn")
    ]
    leaks = [name for name in leaks if not name.startswith("api")]
    if leaks:
        raise AssertionError(f"这些文件 import 了 Web 框架：{leaks}")
    allowed = (
        "project_rag.api",
        "project_rag.application",
        "project_rag.domain",
        "project_rag.ports",
        "project_rag.config",
        "project_rag.observability",
        "project_rag.cli.deps",
    )
    bad = [
        module
        for path in sorted(SRC.joinpath("api").rglob("*.py"))
        for module, level in _imports_of(path)
        if level == 0
        and module.split(".")[0] == "project_rag"
        and not module.startswith(allowed)
    ]
    if bad:
        raise AssertionError(
            f"api/ 依赖了不该依赖的东西：{sorted(set(bad))}；"
            "装配只能借 cli/deps.py 那一套(组合根只该有一套)"
        )
    return "Web 框架只在 api/；api/ 只借组合根与用例"


# ── 2. 问候拦截 ───────────────────────────────────────────────────────────────
@check("问候：整句寒暄才拦；「你好，帮我看看这个报错」不算寒暄（用 search 会拦错）")
def _() -> str:
    match = _match_greeting()
    for text in ("你好", "您好！", "hi", "Hello~", "在吗?", "谢谢", "早上好"):
        if match(text) is None:
            raise AssertionError(f"{text!r} 应该被当成寒暄")
    for text in ("你好，帮我看看这个报错", "谢谢，那退款流程怎么走？", "装饰器是什么", ""):
        if match(text) is not None:
            raise AssertionError(f"{text!r} 不该被当成寒暄（它带着真问题）")
    return "7 条寒暄拦住、4 条真问题放行"


@check("HTTP：POST /api/v1/query 问候 → channel=greeting；不查 FAQ、不查向量库、不写历史")
def _() -> str:
    world = _World()
    response = world.client().post("/api/v1/query", json={"query": "你好"})
    if response.status_code != 200:
        raise AssertionError(f"状态码 {response.status_code}：{response.text[:200]}")
    body = response.json()
    if body["channel"] != "greeting" or not body["answer"]:
        raise AssertionError(f"问候应走 greeting 通道并给一句人话，实际 {body}")
    if world.faq_store.loads or world.matcher.calls or world.vector_store.loads:
        raise AssertionError("寒暄不该查库/走向量检索")
    if world.conversation.records:
        raise AssertionError("寒暄不该进历史")
    return f"channel=greeting，零库调用（{body['answer'][:12]}…）"


# ── 3. 非流式端点 ─────────────────────────────────────────────────────────────
@check("HTTP：健康检查 200；/api/v1/sources 读向量库真实来源（去重升序），缺工厂给 503")
def _() -> str:
    world = _World(hits=[_kb_hit("bagu_java"), _kb_hit("ai"), _kb_hit("ai")])
    client = world.client()
    health = client.get("/health")
    if health.status_code != 200 or health.json() != {"status": "ok"}:
        raise AssertionError(f"健康检查不对：{health.status_code} {health.text}")
    sources = client.get("/api/v1/sources")
    if sources.status_code != 200 or sources.json() != {"sources": ["ai", "bagu_java"]}:
        raise AssertionError(f"来源清单不对：{sources.status_code} {sources.text}")
    if not world.vector_store.loads:
        raise AssertionError("来源应从向量库读，而不是写死在配置里")

    broken = _World().client(vector_store_factory=None)
    missing = broken.get("/api/v1/sources")
    if missing.status_code != 503:
        raise AssertionError(
            f"工厂没装配应给 503（服务没配好 ≠ 服务崩了），实际 {missing.status_code}"
        )
    return "健康 + 来源去重升序 + 缺工厂 503"


@check("HTTP：POST /api/v1/create_session 发 uuid4（不可猜，它同时是历史凭证）")
def _() -> str:
    client = _World().client()
    first = client.post("/api/v1/create_session")
    if first.status_code != 200:
        raise AssertionError(f"状态码 {first.status_code}")
    session_id = first.json()["session_id"]
    if str(uuid.UUID(session_id)) != session_id or uuid.UUID(session_id).version != 4:
        raise AssertionError(f"应是 uuid4，实际 {session_id!r}")
    if client.post("/api/v1/create_session").json()["session_id"] == session_id:
        raise AssertionError("两次应该给不同的 ID")
    return f"uuid4：{session_id[:8]}…"


@check("HTTP：/api/v1/query FAQ 命中 → 直答 + faq_score + channel=faq，且**这一轮进了历史**")
def _() -> str:
    world = _World(faq_score=0.97)
    body = world.client().post(
        "/api/v1/query", json={"query": FAQ_QUESTION, "session_id": "s-web"}
    ).json()
    if body["answer"] != FAQ_ANSWER or body["channel"] != "faq":
        raise AssertionError(f"FAQ 命中应直返标准答案，实际 {body}")
    if body["faq_score"] != 0.97 or body["session_id"] != "s-web":
        raise AssertionError(f"应带上命中分数与会话 ID，实际 {body}")
    if world.llm.calls:
        raise AssertionError("FAQ 命中不该调模型")
    if len(world.conversation.records) != 1:
        raise AssertionError(
            "FAQ 命中的那一轮**也要写历史** —— 绕开用例的那条路径会漏掉它,"
            f"MySQL 命中的答案压根没进历史。实际 records={world.conversation.records}"
        )
    return "直答 + 分数 + 写历史"


@check("HTTP：/api/v1/query 知识库 → channel=rag + 引用（预览是父块正文，模型真读到的那段）")
def _() -> str:
    world = _World()
    body = world.client().post("/api/v1/query", json={"query": "装饰器有什么用"}).json()
    if body["channel"] != "rag" or not body["citations"]:
        raise AssertionError(f"应走知识库并带引用，实际 {body}")
    citation = body["citations"][0]
    if citation["preview"] != KB_PARENT[:120] or citation["source"] != "ai":
        raise AssertionError(f"引用预览应是父块正文，实际 {citation}")
    if citation["page"] != 7:
        raise AssertionError(
            f"引用该带子块的页码（P0-5：这个夹具是第 7 页），实际 {citation.get('page')!r} —— "
            "漏了这一步，前端就只能显示「页码未知」，而库里明明有"
        )
    if body["session_id"] is not None:
        raise AssertionError("没给 session_id 时响应里也该是 null")
    if world.conversation.records:
        raise AssertionError("没给 session_id 就不该碰会话存储")
    return f"channel=rag，{len(body['citations'])} 条引用"


@check("HTTP：拒答也是一句人话（200 + 拒答话术），不是 500 也不是 null —— 早期版本返回过 null")
def _() -> str:
    world = _World(hits=[])
    response = world.client().post("/api/v1/query", json={"query": "语料里没有的问题"})
    if response.status_code != 200:
        raise AssertionError(f"拒答不该是错误码，实际 {response.status_code}")
    body = response.json()
    if body["answer"] != REFUSAL or not body["refused"]:
        raise AssertionError(f"应是拒答话术 + refused=true，实际 {body}")
    if body["answer"] is None:
        raise AssertionError("answer 不允许是 null")
    return "200 + 拒答话术"


@check("HTTP：截断照原样返回，但 truncated=true（前端能提示「没说完」）")
def _() -> str:
    world = _World(pieces=(KB_HALF,), finish_reason="length")
    body = world.client().post("/api/v1/query", json={"query": "装饰器有什么用"}).json()
    if not body["truncated"] or body["answer"] != KB_HALF:
        raise AssertionError(f"截断语义不对：{body}")
    return "truncated=true，半截照发"


@check("HTTP：请求校验 —— 缺 query / 空串 / k<1 都是 422（pydantic 拦在进用例之前）")
def _() -> str:
    client = _World().client()
    for payload in ({}, {"query": ""}, {"query": "x", "k": 0}):
        response = client.post("/api/v1/query", json=payload)
        if response.status_code != 422:
            raise AssertionError(f"{payload} 应被 422 拦下，实际 {response.status_code}")
    return "三种非法请求都 422"


@check("HTTP：历史 GET 只回最近 N 轮（与喂给模型的同一份）、**带当轮引用**；DELETE 返回删掉的轮数")
def _() -> str:
    from project_rag.domain import Chunk, Citation, ConversationTurn, snapshot_citations

    world = _World()
    client = world.client()
    hit = Citation(
        index=1,
        chunk=Chunk("c1", "子块", "bagu_ai", "d1", "p1", "父块正文", page=7),
        score=0.9,
        rerank_score=0.4,
    )
    for i in range(1, 4):
        world.conversation.record(
            "s-web",
            ConversationTurn(
                question=f"q{i}",
                answer=f"a{i} [1]",
                # 第三轮故意没有引用:老账/拒答就是这种形状,它该回空列表而不是缺字段
                citations=snapshot_citations((hit,)) if i == 3 else (),
            ),
            keep=5,
        )
    got = client.get("/api/v1/history/s-web")
    if got.status_code != 200:
        raise AssertionError(f"状态码 {got.status_code}")
    rounds = got.json()["rounds"]
    if [turn["question"] for turn in rounds] != ["q1", "q2", "q3"]:
        raise AssertionError(f"应按时间正序返回，实际 {rounds}")
    if world.conversation.reads[-1][1] != 5:
        raise AssertionError(f"读的应是配置里的轮数，实际 {world.conversation.reads[-1]}")
    # 引用必须随答案一起回去(答案里的 [1] 没有卡片可点 = 没有出处),且形状与实时那份同源
    cards = rounds[2]["citations"]
    if len(cards) != 1 or cards[0]["index"] != 1 or cards[0]["page"] != 7:
        raise AssertionError(f"历史该带当轮引用卡片,实际 {cards}")
    if set(cards[0]) != {"index", "source", "doc_id", "preview", "score", "rerank_score", "page"}:
        raise AssertionError(f"引用卡片的字段与实时那份不一致:{sorted(cards[0])}")
    if rounds[0]["citations"] != []:
        raise AssertionError(f"没有出处的那一轮该给空列表(不是缺字段也不是编一条),实际 {rounds[0]}")

    deleted = client.delete("/api/v1/history/s-web").json()
    if deleted["deleted"] != 3 or deleted["session_id"] != "s-web":
        raise AssertionError(f"删除结果不对：{deleted}")
    if world.conversation.recent("s-web", limit=5):
        raise AssertionError("删完应为空")
    return "GET 最近 N 轮 + 当轮引用卡片(与实时同字段) + DELETE 返回条数"


@check("响应模型：answer 必填、channel 只认 faq/rag/greeting/throttled、citations 是出参模型")
def _() -> str:
    Schemas = importlib.import_module("project_rag.api.schemas")
    hints = typing.get_type_hints(Schemas.QueryResponse)
    # 闭集里那两个"不是通道"的值都是**接口层的前置拦截**:
    #   greeting = 寒暄(直接回话术,不查库)      —— M5 就有
    #   throttled = 触发限流(回"稍后再试")      —— M7.4 加(作者定:问答超限给兜底话术而不是 429)
    # 钉死这个闭集,是为了让"多一种出口"必须显式改这里 —— 顺手塞一个值进去,
    # 前端的 channel 分支就会漏掉它(显示成光秃秃一句话)。
    want = ("faq", "rag", "greeting", "throttled")
    if typing.get_args(hints["channel"]) != want:
        raise AssertionError(f"channel 取值应为 {'/'.join(want)}，实际 {hints['channel']}")
    field = Schemas.QueryResponse.model_fields["answer"]
    if not field.is_required():
        raise AssertionError("answer 必须必填（拒答也是一句话），不能给默认值")
    if hints["answer"] is not str:
        raise AssertionError(f"answer 应是 str、不是 str | None，实际 {hints['answer']}")
    if "CitationOut" not in dir(Schemas):
        raise AssertionError("引用出参模型 CitationOut 不在 schemas 里")
    return "QueryResponse/ CitationOut 形状正确"


# ── 4. WebSocket ─────────────────────────────────────────────────────────────
@check("WS：FAQ 命中 → token + done(channel=faq)；同一连接还能接着问下一条")
def _() -> str:
    world = _World(pieces=(KB_PARTS[0],), faq_score=0.97)
    with world.client().websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": FAQ_QUESTION})
        first, second = ws.receive_json(), ws.receive_json()
        if first != {"type": "token", "text": FAQ_ANSWER}:
            raise AssertionError(f"第一帧应是标准答案的 token 帧，实际 {first}")
        if second["type"] != "done" or second["channel"] != "faq":
            raise AssertionError(f"第二帧应是 done(channel=faq)，实际 {second}")
        # 坑:问候/一次性答案之后 break 掉整条连接，用户就得重连。这里再问一条必须还通
        ws.send_json({"query": FAQ_QUESTION})
        again = ws.receive_json()
        if again["type"] != "token":
            raise AssertionError(f"同一连接的第二条请求应有回复，实际 {again}")
    return "token + done，连接可复用"


@check("WS：知识库 → sources 在最前、逐帧 token、done 收尾（帧协议与 CLI 那条流一致）")
def _() -> str:
    world = _World()
    with world.client().websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": "装饰器有什么用"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                break
    kinds = [frame["type"] for frame in frames]
    if kinds[0] != "sources" or set(kinds[1:-1]) != {"token"}:
        raise AssertionError(f"帧顺序应为 sources → token* → done，实际 {kinds}")
    joined = "".join(f["text"] for f in frames if f["type"] == "token")
    if joined != frames[-1]["answer"]:
        raise AssertionError(f"恒等式破了：{joined!r} ≠ {frames[-1]['answer']!r}")
    if not frames[0]["citations"] or not frames[-1]["citations"]:
        raise AssertionError("sources 与 done 都要带引用")
    return f"{len(frames)} 帧：sources → token* → done"


@check("WS：截断 → done.truncated=true；带 session_id → 这一轮进历史")
def _() -> str:
    world = _World(pieces=(KB_HALF,), finish_reason="length")
    with world.client().websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": "装饰器有什么用", "session_id": "s-ws"})
        while True:
            frame = ws.receive_json()
            if frame["type"] == "done":
                break
    if not frame["truncated"] or frame["answer"] != KB_HALF:
        raise AssertionError(f"截断语义不对：{frame}")
    if len(world.conversation.records) != 1 or world.conversation.records[0][0] != "s-ws":
        raise AssertionError(f"带 session_id 的流式回答也要写历史，实际 {world.conversation.records}")
    return "truncated + 写历史"


@check("WS：问候 → token + done(greeting)，**不关连接**（早期版本在那里 break 掉了）")
def _() -> str:
    world = _World()
    with world.client().websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": "你好"})
        token, done = ws.receive_json(), ws.receive_json()
        if token["type"] != "token" or done["type"] != "done" or done["channel"] != "greeting":
            raise AssertionError(f"问候帧不对：{token} {done}")
        if world.conversation.records:
            raise AssertionError("问候不该进历史")
        ws.send_json({"query": "装饰器有什么用"})
        nxt = ws.receive_json()
        if nxt["type"] != "sources":
            raise AssertionError(f"问候之后连接应还活着，实际收到 {nxt}")
    return "问候后连接仍可用"


@check("WS：请求体畸形（非 JSON / 字段不合法）都回一帧 type=error，连接留着（不是把连接崩掉）")
def _() -> str:
    world = _World()
    with world.client().websocket_connect("/api/v1/stream") as ws:

        def ask(payload: dict) -> list[str]:
            """发一问,把帧读到 done,返回帧类型序列。

            必须读干净:只取第一帧的话,上一轮剩下的 token / done 会留在队列里,
            下一条断言就会拿它当「这一轮的回复」——假绿假红都会从这里来。
            """
            ws.send_json(payload)
            kinds: list[str] = []
            while True:
                frame = ws.receive_json()
                kinds.append(frame["type"])
                if frame["type"] == "done":
                    return kinds

        ws.send_json({"query": ""})
        error = ws.receive_json()
        if error["type"] != "error" or "不合法" not in error["detail"]:
            raise AssertionError(f"应回 error 帧，实际 {error}")
        if ask({"query": "装饰器有什么用"})[0] != "sources":
            raise AssertionError("出错之后连接应还能继续用")

        # 非 JSON 那条:`receive_json()` 会抛 JSONDecodeError,而它发生在「校验」那个 try 之外。
        # 曾经就这么漏到外层 except,连接跟着断(前端得重连才能再问)—— 这条专门钉住那种写法。
        ws.send_text("{ 这不是 JSON")
        error = ws.receive_json()
        if error["type"] != "error" or "JSON" not in error["detail"]:
            raise AssertionError(f"非 JSON 也要回 error 帧并说明不是 JSON，实际 {error}")
        if ask({"query": "装饰器有什么用"})[0] != "sources":
            raise AssertionError("非 JSON 出错之后连接应还能继续用（别把连接崩掉）")
    return "两种畸形都回 error 帧 + 连接可继续"


# ── 5. CORS / 静态 ───────────────────────────────────────────────────────────
@check("CORS：预检请求带上 access-control-allow-origin（全局 CORS）")
def _() -> str:
    client = _World().client()
    response = client.options(
        "/api/v1/query",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    if response.headers.get("access-control-allow-origin") != "*":
        raise AssertionError(f"CORS 头不对：{dict(response.headers)}")
    return "allow-origin: *"


@check("静态：GET / 307 → /static/app/；目录 URL 与它引用的资源都取得到；旧调试页已删（404）")
def _() -> str:
    """静态入口的三条性质,判据全落在**用户真的会输的那个 URL** 上(STATUS.md §0.3 第 32 条)。

    之前这里断言的是「`/static/index.html` 能用、页面里能看到 `/api/v1/stream`
    与 `?token=`」—— 那份内置调试页已经删掉(见 `app.py` 根路由的说明),于是:
    「页面走的是服务端那条 WS」改由 `check_m7_frontend` 的**前端清单↔后端路由表**那条管
    (读 `endpoints.ts`,比在压缩过的产物里搜字符串结实);这里只留静态挂载本身的性质。
    """
    client = _World().client()
    root = client.get("/", follow_redirects=False)
    if root.status_code != 307 or root.headers.get("location") != "/static/app/":
        raise AssertionError(
            f"根路径应 307 跳 /static/app/（URL 得以目录结尾,产物的相对 base 才解析得对），"
            f"实际 {root.status_code} → {root.headers.get('location')!r}"
        )
    page = client.get("/static/app/")
    if page.status_code != 200 or "text/html" not in page.headers.get("content-type", ""):
        raise AssertionError(
            f"用户会输的 /static/app/ 应给 HTML（StaticFiles 要 html=True 才有目录索引），"
            f"实际 {page.status_code} {page.headers.get('content-type')!r}"
        )
    if 'id="app"' not in page.text:
        raise AssertionError("产物 index.html 里没有挂载点 #app —— 这不是前端产物")
    # 「文件在」不等于「页面能打开」:产物引用的资源必须真能取到(同上第 32 条)
    for asset in re.findall(r'(?:src|href)="\./(assets/[^"]+)"', page.text):
        got = client.get(f"/static/app/{asset}")
        if got.status_code != 200:
            raise AssertionError(f"产物引用的 {asset} 取不到:{got.status_code}(页面会白屏)")
    # 只剩一份界面:旧的内置调试页 删了。两份 UI = 两套要跟着后端改的东西,
    # 而其中一套没人日常打开 —— 它漂了也不会有人发现。这条防止它被"顺手"加回来。
    gone = client.get("/static/index.html")
    if gone.status_code != 404:
        raise AssertionError(
            f"旧内置调试页应该已经删掉(404),实际 {gone.status_code} —— 界面只留一份"
        )
    return "307 → /static/app/；资源自洽；旧调试页 404"


def main() -> int:
    return report("M5.4 FastAPI 接口层验收")


if __name__ == "__main__":
    sys.exit(main())

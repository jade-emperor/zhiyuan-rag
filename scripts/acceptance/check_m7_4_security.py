#!/usr/bin/env python
"""契约：安全收口（限流 / 登出撤销 / 路由守卫全量扫描 / CSP / CORS）+ 桌面壳。

用法：

    uv run python scripts/acceptance/check_m7_4_security.py

**这一份在堵什么洞**：界面做完了，但安全那一半还留着四个口子：

1. **没有限流**：一次问答要烧 CPU(嵌入 + 重排)和 token，登录还能被拿来试口令 ——
   一个循环脚本就能把额度吃光，而这件事在日志里长得跟"今天大家都很活跃"一模一样；
2. **"退出"只是前端删了本地令牌**：服务端那枚照样有效到 TTL 结束(默认 12 小时)，
   谁之前拷走了它，"退出"对他没有任何作用；
3. **新增端点可以悄悄忘掉认证**：现在靠人肉自觉（`/capabilities` 这样公开的、
   `/documents` 这样要 Bearer 的，各写各的）；
4. **界面没有 CSP**：令牌还在 `localStorage`（已知缺口），
   注入一段 script 就能把它读走 —— CSP 是那道兜底。

外加桌面壳（的另一半）：**它的可验证部分**在这里钉住 ——
``--help`` / ``--check`` 不依赖 GUI 栈、产物与引用资源都在、``--backend`` 拼进 URL。
开窗那一步需要图形界面，这台机器没有，所以**不在回归里假装验过**
（判定标准写在 ``scripts/acceptance/`` 里那几条契约的 docstring）。

公开契约：

    POST /api/v1/auth/logout   -> {"revoked": bool}   # 幂等;撤销后同一枚令牌立刻 401
    限流                         每分钟每身份:query 60 / upload 10 / login 10(可按档配)
                              -> 超限 429 + Retry-After(HTTP);WS 回 error 帧且不断连接
    GET  /static/app/          -> Content-Security-Policy(script-src 'self'、frame-ancestors 'none')
    project-rag-desktop --check --print-url -> 打印会打开的 URL(不开窗,不需要 pywebview)
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"


def _read_web(relative: str) -> str:
    """输入:相对 `web/src/` 的路径。行为:读它。输出:正文(不在就报错,不静默给空串)。"""
    path = ROOT / "web" / "src" / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path}")
    return path.read_text(encoding="utf-8")


def _deps(**over: Any):
    """输入:工厂覆盖。行为:一套可注入的假依赖(问答能真的跑通)。输出:Deps。"""
    from _fakes import (
        FakeEmbedder,
        FakeFaqStore,
        FakeKeywordIndex,
        FakeLLM,
        FakeMatcher,
        FakeReranker,
        FakeVectorStore,
    )
    from project_rag.cli import Deps

    fields: dict[str, Any] = {
        "llm_factory": lambda: FakeLLM(["答"]),
        "faq_store_factory": lambda: FakeFaqStore([]),
        "matcher_factory": lambda: FakeMatcher(None),
        "embedder_factory": lambda: FakeEmbedder(),
        "vector_store_factory": lambda: FakeVectorStore([]),
        "keyword_index_factory": lambda: FakeKeywordIndex(),
        "reranker_factory": lambda: FakeReranker(),
        "suffixes_factory": lambda: (".pdf",),
    }
    fields.update(over)
    return Deps(**fields)


def _client(**over: Any):
    """输入:工厂覆盖。行为:建一个注入了假依赖的 TestClient。输出:client。"""
    from fastapi.testclient import TestClient

    from project_rag.api import create_app

    return TestClient(create_app(_deps(**over)))


def _query(client, **payload):
    """输入:客户端 + 额外字段。行为:打一次问答(默认不起重排,省得构造模型)。输出:响应。"""
    body = {"query": "装饰器是什么", "rerank": False, **payload}
    return client.post("/api/v1/query", json=body)


class _FakeAuth:
    """认证存储的内存替身:账号 + 令牌撤销(登出那条要用到 revoke)。"""

    PASSWORDS = {"boss": "pw-123456", "worker": "pw-123456"}

    def __init__(self) -> None:
        from project_rag.application.security import hash_password
        from project_rag.domain.auth import User

        self.users = {
            "boss": (User("u-boss", "boss", "admin"), hash_password(self.PASSWORDS["boss"])),
            "worker": (
                User("u-worker", "worker", "employee"),
                hash_password(self.PASSWORDS["worker"]),
            ),
        }
        self.tokens: dict[str, tuple[str, float]] = {}

    def init_schema(self) -> None: ...

    def create_user(self, *args: Any, **kwargs: Any):  # pragma: no cover - 本脚本不用
        raise NotImplementedError

    def set_password(self, user_id, password_hash) -> None: ...

    def delete_user(self, user_id: str) -> int:  # pragma: no cover - 本脚本用不到
        return 0

    def list_users(self):
        return [user for user, _ in self.users.values()]

    def find_by_username(self, username):
        return self.users.get(username)

    def issue_token(self, user, token_hash, ttl_seconds) -> None:
        import time

        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash):
        import time

        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next(user for user, _ in self.users.values() if user.user_id == item[0])

    def revoke_token(self, token_hash) -> None:
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id) -> None: ...

    def owner_of(self, session_id):
        return None


# ══ 一、限流 ═════════════════════════════════════════════════════════════════
def _memory():
    """输入:无。行为:一个进程内存限流器(离线可跑的那一份)。输出:MemoryRateLimiter。"""
    from project_rag.adapters.ratelimit_memory import MemoryRateLimiter

    return MemoryRateLimiter()


def _redis_limiter(**over):
    """输入:配置覆盖。行为:一个 Redis 限流器(真库,连不上就抛)。输出:RedisRateLimiter。"""
    from project_rag.adapters.ratelimit_redis import RedisRateLimiter
    from project_rag.config import get_settings

    settings = get_settings()
    prefix = over.pop("key_prefix", f"ratelimit-test:{time.time_ns()}:")
    return RedisRateLimiter(settings.redis, key_prefix=prefix), prefix


def _redis_or_skip():
    """输入:无。行为:探一下 Redis 在不在。输出:限流器 + 键前缀;连不上就 SKIP。"""
    from _harness import SkipCheck

    try:
        limiter, prefix = _redis_limiter()
        limiter._client.ping()  # noqa: SLF001 - 探活:连不上就跳过而不是假绿
    except Exception as exc:  # noqa: BLE001
        raise SkipCheck(
            f"Redis 连不上({exc});跑 `docker compose up -d redis` 或看 check_env_services"
        ) from exc
    return limiter, prefix


def _app_with_limiter(limiter):
    """输入:限流器。行为:建一个把它注入进去的 TestClient。输出:client。"""
    return _client(rate_limiter_factory=lambda: limiter)


@check("限流语义（内存那份）：窗口内放行、超了给 ≥1 秒、窗口过了又放行、limit≤0 是关掉")
def _() -> str:
    window = _memory()
    at = lambda tick: window._check_at("k", limit=3, window_s=60.0, now=tick)  # noqa: SLF001
    assert [at(tick) for tick in (0.0, 0.1, 0.2)] == [0, 0, 0]
    retry = at(0.5)
    assert retry >= 1, f"被拒时给的等待秒数必须 ≥1(0 会被读成「立刻重试」),实际 {retry}"
    assert at(61.0) == 0, "窗口过了还不放行"
    assert window._check_at("other", limit=3, window_s=60.0, now=61.0) == 0, "另一个键该互不影响"  # noqa: SLF001
    # limit<=0 = 明确关掉这一档(不是「限 0 次」)
    assert window._check_at("off", limit=0, window_s=60.0, now=61.0) == 0  # noqa: SLF001
    assert window._check_at("off", limit=-1, window_s=60.0, now=61.0) == 0  # noqa: SLF001
    return f"3 次放行 → 第 4 次拒(等 {retry}s) → 61s 后放行;limit≤0 = 关"


@check("限流语义（内存那份）：过期时间戳被裁掉（不会每个键攒无限多个浮点数）")
def _() -> str:
    window = _memory()
    for tick in range(200):
        window._check_at("k", limit=1, window_s=10.0, now=float(tick))  # noqa: SLF001
    kept = len(window.hits["k"])
    assert kept <= 11, f"窗口里最多该留 11 个时间戳(10 秒 + 边界),实际留了 {kept}"
    assert window.keys_seen == 1, window.keys_seen
    return f"200 次调用之后窗口里只留 {kept} 个时间戳"


@check("限流语义（Redis 那份）：与内存那份一字不差（窗口 / 拒绝 / limit≤0 / forget）")
def _() -> str:
    limiter, prefix = _redis_or_skip()
    key = "same-semantics"
    assert [limiter.check(key, limit=3, window_s=60.0) for _ in range(3)] == [0, 0, 0]
    retry = limiter.check(key, limit=3, window_s=60.0)
    assert retry >= 1, f"Redis 那份给的等待秒数必须 ≥1,实际 {retry}"
    assert limiter.check(key, limit=0, window_s=60.0) == 0, "limit≤0 该直接放行(不问 Redis)"
    # 键有 TTL:窗口过后自己过期,不需要谁来清理(不然键会一直堆在 Redis 里)
    assert int(str(limiter._client.pttl(prefix + key))) > 0, "计数键没设 TTL"  # noqa: SLF001
    # 窗口的边界要**按这一次给的窗口**算(而不是"设 TTL 时那个窗口"):
    # 先用 60 秒窗口塞满,再等 1.1 秒用 1 秒窗口问一次 —— 那两条命中已经落在窗口之外,
    # 必须被裁掉并放行。**没有裁剪**的话计数还是 2 ≥ limit,会一直拒到键 TTL 到期
    # (RT-12 第一次就是找不到这样的场景才没红:同窗口下键自己会过期,裁剪看起来是多余的)。
    mixed = "mixed-window"
    assert [limiter.check(mixed, limit=2, window_s=60.0) for _ in range(2)] == [0, 0]
    time.sleep(1.1)
    assert limiter.check(mixed, limit=2, window_s=1.0) == 0, (
        "旧命中必须按**这次的**窗口裁掉(否则它会一直占着名额直到键过期)"
    )
    assert limiter.check(mixed, limit=2, window_s=1.0) == 0
    assert limiter.check(mixed, limit=2, window_s=1.0) >= 1, "1 秒窗口内第 3 次该被拒"
    limiter._client.delete(prefix + mixed)  # noqa: SLF001

    limiter.forget(key)
    assert limiter.check(key, limit=3, window_s=60.0) == 0, "forget 之后该恢复满额"
    limiter._client.delete(prefix + key)  # noqa: SLF001
    return f"3 次放行 → 第 4 次拒(等 {retry}s);键有 TTL;forget 后恢复"


@check("换 Redis 的**全部理由**：两个实例共享一份计数（多 worker / 重启都不清零）")
def _() -> str:
    first, prefix = _redis_or_skip()
    second, _ = _redis_limiter(key_prefix=prefix)
    assert first is not second and first._client is not second._client, "得是两个独立实例"  # noqa: SLF001

    key = "shared"
    assert [first.check(key, limit=2, window_s=60.0) for _ in range(2)] == [0, 0]
    blocked = second.check(key, limit=2, window_s=60.0)
    assert blocked >= 1, (
        "另一个实例该看得见已经用掉的额度 —— 看不见就说明计数还在各自进程里(那正是要换掉的东西)"
    )

    # "重启":再建一个实例(全新的对象、全新的连接),额度仍然是用掉的状态
    restarted, _ = _redis_limiter(key_prefix=prefix)
    assert restarted.check(key, limit=2, window_s=60.0) >= 1, "重启之后额度该还是用掉的"
    first._client.delete(prefix + key)  # noqa: SLF001
    return f"实例 A 用掉 2 次 → 实例 B 与「重启后」的实例都被拒(等 {blocked}s)"


@check("限流是原子的：20 个线程抢 5 个名额，恰好 5 个通过（Redis 一份 Lua）")
def _() -> str:
    import threading

    limiter, prefix = _redis_or_skip()
    key = "concurrent"
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def burst() -> None:
        barrier.wait()  # 尽量让 20 个请求同时挤进去(不并发的话这条测不出原子性)
        allowed = limiter.check(key, limit=5, window_s=60.0)
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=burst) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    passed = sum(1 for item in results if item == 0)
    assert passed == 5, (
        f"20 个并发请求、额度 5:该恰好放行 5 个,实际 {passed} 个 —— "
        "多放行说明「先查数量再写入」不是原子的(Redis 那份必须是一段 Lua)"
    )
    limiter._client.delete(prefix + key)  # noqa: SLF001
    return f"20 并发 / 额度 5 → 放行 {passed} 个,其余 {20 - passed} 个被拒"


@check("Redis 故障时降级放行（不 503）,但留痕:计数 + 每个窗口最多一条 WARNING")
def _() -> str:
    import logging

    from project_rag.adapters.ratelimit_redis import RedisRateLimiter
    from project_rag.config import get_settings

    settings = get_settings()
    dead = settings.redis.model_copy(update={"port": 1})  # 没人监听 1 号端口
    limiter = RedisRateLimiter(dead, key_prefix="ratelimit-test:dead:")

    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    module_logger = logging.getLogger("project_rag.adapters.ratelimit_redis")
    module_logger.addHandler(_Sink())
    try:
        assert limiter.check("k", limit=1, window_s=60.0) == 0, "Redis 挂了该放行,不是 503"
        assert limiter.check("k", limit=1, window_s=60.0) == 0, "第二次照样放行"
        assert limiter.degraded == 2, f"降级次数要记下来,实际 {limiter.degraded}"
        assert len(records) == 1, (
            f"同一窗口里该只告警一次(不然故障期间日志会被刷爆),实际 {len(records)} 条"
        )
        assert "限流降级" in records[0], records[0]
    finally:
        module_logger.handlers = [
            handler for handler in module_logger.handlers if not isinstance(handler, _Sink)
        ]
    return "Redis 不可用 → 放行(2 次),degraded=2,告警只 1 条"


@check("配置真的落地：enabled=false 不限流 / backend=memory 用内存那份 / 乱写要炸")
def _() -> str:
    from project_rag.adapters.ratelimit_memory import MemoryRateLimiter
    from project_rag.adapters.ratelimit_redis import RedisRateLimiter
    from project_rag.cli.deps import _rate_limiter
    from project_rag.config import get_settings

    settings = get_settings()

    def with_limit(**over):
        return settings.model_copy(
            update={"api": settings.api.model_copy(update={"rate_limit": settings.api.rate_limit.model_copy(update=over)})}
        )

    default_factory = _rate_limiter(settings)
    assert default_factory is not None and isinstance(default_factory(), RedisRateLimiter), (
        "默认真该是 Redis 那一份"
    )
    memory_factory = _rate_limiter(with_limit(backend="memory"))
    assert memory_factory is not None and isinstance(memory_factory(), MemoryRateLimiter)
    assert _rate_limiter(with_limit(enabled=False)) is None, "enabled=false 该彻底不接(不限流)"
    try:
        _rate_limiter(with_limit(backend="memcached"))
    except ValueError as exc:
        assert "memcached" in str(exc), exc
    else:
        raise AssertionError("不认识的后端名该当场炸,而不是悄悄退化成某一种")

    # 生产装配必然接上限流(与"生产路径必然开着认证"同一条口径)
    from project_rag.cli.deps import build_real_deps

    assert build_real_deps().rate_limiter_factory is not None, (
        "build_real_deps 没接限流器 —— 那等于生产路径上没有任何闸门"
    )
    return "redis / memory / enabled=false / 非法值 四条都生效;build_real_deps 必然接上"


@check("问答超限回的是**兜底话术**（200 + channel=throttled + 带秒数），不是 429")
def _() -> str:
    from project_rag.config import get_settings

    limit = get_settings().api.rate_limit.query
    # **显式注入**限流器:裸 `Deps()` 是"不注入就不造"= 不限流(与其它工厂同一条口径),
    # 所以接口层这些用例要把那份内存实现交进去;Redis 那一份另有专门的用例。
    client = _app_with_limiter(_memory())
    codes = [_query(client).status_code for _ in range(limit)]
    assert set(codes) == {200}, f"额度内的请求应该都成功:{sorted(set(codes))}"

    blocked = _query(client)
    body = blocked.json()
    assert blocked.status_code == 200, (
        f"问答超限该回**兜底话术**(200),而不是错误码;实际 {blocked.status_code}"
    )
    assert body["channel"] == "throttled", f"得让前端分得出这是限流:{body['channel']!r}"
    assert body["citations"] == [] and body["refused"] is False, (
        f"兜底话术不是拒答、也没有引用:{body}"
    )
    assert re.search(r"等约 \d+ 秒", body["answer"]), f"话术里必须给出等待秒数:{body['answer']!r}"
    assert str(get_settings().api.rate_limit.query) in body["answer"], (
        f"话术里要能看出这一档的上限:{body['answer']!r}"
    )
    retry = blocked.headers.get("retry-after")
    assert retry is not None and int(retry) >= 1, (
        f"回 200 也要带 Retry-After(写脚本的人照样能按它退避),实际 {dict(blocked.headers)}"
    )
    return f"额度 {limit} 次;第 {limit + 1} 次 → 200 + channel=throttled + Retry-After={retry};话术「{body['answer'][:26]}…」"


@check("兜底话术**不写进会话历史**（那不是这一轮问答的内容）")
def _() -> str:
    from _fakes import FakeConversationStore

    from project_rag.config import get_settings

    store = FakeConversationStore({"s1": []})
    client = _client(
        conversation_store_factory=lambda: store, rate_limiter_factory=_memory
    )
    limit = get_settings().api.rate_limit.query
    for _ in range(limit):
        assert _query(client, session_id="s1").status_code == 200
    written = len(store.records)

    blocked = _query(client, session_id="s1")
    assert blocked.json()["channel"] == "throttled", blocked.json()["channel"]
    assert len(store.records) == written, (
        f"被限流的那一轮被写进历史了({written} → {len(store.records)}) —— "
        "下一轮的提示词里会多出一句系统自己说的话(与寒暄同一条口径:不写)"
    )
    return f"额度内写了 {written} 轮;被限流那轮一轮都没写"


@check("HTTP 与 WS 共用同一份额度（两条入口一个闸门,不是各算各的）")
def _() -> str:
    from project_rag.config import get_settings

    limit = get_settings().api.rate_limit.query
    client = _app_with_limiter(_memory())
    for _ in range(limit):
        assert _query(client).status_code == 200
    with client.websocket_connect("/api/v1/stream") as ws:
        ws.send_json({"query": "装饰器是什么"})
        first = ws.receive_json()
        assert first["type"] == "token", f"WS 该按**话术**的形制回(token 帧),实际 {first}"
        done = ws.receive_json()
        assert done["type"] == "done" and done["channel"] == "throttled", (
            f"HTTP 已经用光额度,WS 该被同一档挡住并给出兜底话术:{done}"
        )
        assert re.search(r"等约 \d+ 秒", done["answer"]), done["answer"]
        # **连接不能断,也不能变成 error 帧**:再发一条照样能问(额度还在,所以还是话术)
        ws.send_json({"query": "再问一句"})
        again = ws.receive_json()
        assert again["type"] == "token", f"连接应该还活着且继续走话术形制:{again}"
    return f"HTTP 用光 {limit} 次之后,WS 回 token+done(channel=throttled)且连接不断"


@check("登录限流：连错会被挡住；登录成功会清掉本 IP 的计数")
def _() -> str:
    from project_rag.config import get_settings

    limit = get_settings().api.rate_limit.login
    assert limit >= 3, "这条用例要 limit≥3 才写得清楚"
    client = _app_with_limiter(_memory())
    client = _client(auth_store_factory=lambda: _FakeAuth(), rate_limiter_factory=_memory)

    def attempt(password: str):
        return client.post(
            "/api/v1/auth/login", json={"username": "boss", "password": password}
        )

    # 先错 limit 次 → 第 limit+1 次被挡住
    for _ in range(limit):
        assert attempt("wrong-password").status_code == 401
    blocked = attempt("wrong-password")
    assert blocked.status_code == 429, f"第 {limit + 1} 次爆破该被挡住,实际 {blocked.status_code}"
    assert blocked.headers.get("retry-after"), "429 要带 Retry-After"
    # 文案要**说清是被拦了 + 等几秒**,而且**不能是问答那句"问得有点密集"**
    # (登录页上没有人"问"什么;三档共用一句话是第一版的错,浏览器用例的期望正是"请求过于频繁")
    detail = blocked.json().get("detail", "")
    assert "过于频繁" in detail and "秒" in detail, f"429 的话术要可执行:{detail!r}"
    assert "问得有点密集" not in detail, f"登录页不该出现聊天式兜底话术:{detail!r}"

    # 换一个**新的 app** 验"登录成功清计数":先错 limit-1 次,再成功一次,
    # 之后还能连着成功 limit-1 次 —— 没有 forget 的话第一次就会被自己的失败次数挡住。
    fresh = _client(auth_store_factory=lambda: _FakeAuth(), rate_limiter_factory=_memory)
    for _ in range(limit - 1):
        assert fresh.post(
            "/api/v1/auth/login", json={"username": "boss", "password": "nope"}
        ).status_code == 401
    ok = fresh.post("/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"})
    assert ok.status_code == 200, ok.text
    for index in range(limit - 1):
        again = fresh.post(
            "/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"}
        )
        assert again.status_code == 200, (
            f"登录成功之后第 {index + 1} 次又被挡了 —— forget 没生效(用户被自己刚才的失败牵连)"
        )
    # 键里带的是**来源 IP**(socket 对端),不认 X-Forwarded-For:
    # 信那个头而没有可信反代时,谁都能伪造一个 IP 绕过限流。
    source = (ROOT / "src" / "project_rag" / "api" / "app.py").read_text(encoding="utf-8")
    assert "X-Forwarded-For" not in source and "x-forwarded-for" not in source, (
        "限流的键用了 X-Forwarded-For:没有可信反代时那是可伪造的"
    )
    return f"错 {limit} 次 → 429(带 Retry-After);成功一次后计数清零(键 = socket 对端,不信 XFF)"


@check("上传限流：超限 429（上传背后是几分钟的 CPU 嵌入,闸门更紧）")
def _() -> str:
    from project_rag.config import get_settings

    limit = get_settings().api.rate_limit.upload
    store = _FakeAuth()
    client = _client(auth_store_factory=lambda: store, rate_limiter_factory=_memory)
    assert limit < get_settings().api.rate_limit.query, "上传那一档该比问答更紧"
    token = client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    codes = [
        client.post(
            "/api/v1/documents?filename=x.pdf", content=b"tiny", headers=headers
        ).status_code
        for _ in range(limit + 1)
    ]
    assert codes[-1] == 429, f"第 {limit + 1} 次上传该被挡住:{codes}"
    assert 401 not in codes, f"带令牌不该 401:{codes[:3]}"
    # 未认证的请求:**到不了限流那一行** —— `Depends(auth.user)` 在进入函数体之前就 401 了
    # (缺 Authorization 头时连库都不用查)。这是刻意的:401 极便宜,而**真正烧钱的路径
    # 本来就要令牌**(问答/上传/删除);唯一不需要令牌的入口是登录,而登录是限流在鉴权之前。
    anon = _client(auth_store_factory=lambda: _FakeAuth(), rate_limiter_factory=_memory)
    anon_codes = [
        anon.post(
            "/api/v1/documents?filename=y.pdf",
            content=b"tiny",
            headers={"Content-Type": "application/octet-stream"},
        ).status_code
        for _ in range(limit + 1)
    ]
    assert set(anon_codes) == {401}, f"裸请求该一律 401(依赖在函数体之前就拦了):{set(anon_codes)}"
    return f"上传档 = {limit} 次/分钟;带令牌第 {limit + 1} 次 429;裸请求在依赖层就 401"



@check("接口层用的是组合根给的**那一份**：换一个 app 实例,额度照样是被用掉的")
def _() -> str:
    from project_rag.application.security import hash_password
    from project_rag.domain.auth import User

    limiter, prefix = _redis_or_skip()
    # 两个"进程":各自 create_app、各自一个 TestClient,但共用同一份 Redis 计数
    first = _client(rate_limiter_factory=lambda: limiter)
    second = _client(rate_limiter_factory=lambda: _redis_limiter(key_prefix=prefix)[0])

    from project_rag.config import get_settings

    limit = get_settings().api.rate_limit.query
    for _ in range(limit):
        assert _query(first).status_code == 200
    blocked = _query(second)
    body = blocked.json()
    assert body.get("channel") == "throttled", (
        f"另一个 app 实例该看到额度已用完(拿到 {body.get('channel')!r} / HTTP {blocked.status_code})—— "
        "回兜底话术说明接口层真的在用组合根给的那份 Redis 计数,而不是自己 new 了个内存版"
    )
    assert blocked.headers.get("retry-after"), "兜底话术也要带 Retry-After"
    limiter._client.delete(prefix + "query:ip:testclient")  # noqa: SLF001
    return f"实例 A 用光 {limit} 次 → 实例 B 的同一个 IP 拿到兜底话术(共享生效)"

# ══ 二、登出与撤销 ═══════════════════════════════════════════════════════════
@check("登出：撤销之后同一枚令牌立刻 401（不是只删了前端副本）")
def _() -> str:
    client = _client(auth_store_factory=lambda: _FakeAuth())
    token = client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200, "登出前该是好的"

    out = client.post("/api/v1/auth/logout", headers=headers)
    assert out.status_code == 200 and out.json()["revoked"] is True, out.text
    after = client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401, f"撤销之后这枚令牌必须立刻失效,实际 {after.status_code}"

    # 幂等:再登出一次给 revoked=false 而不是报错
    again = client.post("/api/v1/auth/logout", headers=headers)
    assert again.status_code == 200 and again.json()["revoked"] is False, again.text
    # 没带令牌也照样 200
    assert client.post("/api/v1/auth/logout").json() == {"revoked": False}
    return "登出 → me 变 401;重复登出与不带令牌都 200 + revoked=false"


# ══ 三、路由守卫全量扫描 ═════════════════════════════════════════════════════
@check("路由守卫：不要求认证的路由**只有**白名单那几条（新增端点忘加认证 → 这里红）")
def _() -> str:
    from project_rag.api import create_app

    app = create_app(_deps(auth_store_factory=lambda: _FakeAuth()))
    # 白名单 = **刻意公开**的那几条,每条都要说得出理由:
    #   /health            探活(容器 healthcheck 用,不能要令牌)
    #   /capabilities      能力自述,不含任何数据;登录页要用它渲染上传提示
    #   /auth/login        拿令牌的入口,当然不能先要令牌
    #   /auth/captcha     登录/注册前置的图,**必须**在拿到账号之前就能取(否则注册先要有账号)
    #   /auth/register    自助注册:入口本身就是"还没有账号"的人用的,鉴权靠验证码 + 口令策略 + 限流
    #   /auth/logout       幂等退出(没令牌也给 200 + revoked=false),令牌本身在头里
    #   /api/v1/stream     WebSocket:**浏览器不能给 WS 设头**,令牌只能走 ?token=(路由里手验)
    #   /docs /redoc /openapi.json  FastAPI 自带的接口文档:暴露的是**形状**不是数据
    # / /static 前端产物(旧的调试页 已删,现在只剩这一份界面)
    whitelist = {
        "/health",
        "/api/v1/capabilities",
        "/api/v1/auth/login",
        "/api/v1/auth/captcha",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/stream",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        "/static",
        "/",
    }
    public: set[str] = set()
    guarded: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        dependant = getattr(route, "dependant", None)
        if dependant is None:  # Mount / 静态挂载
            public.add(path)
            continue
        needs_auth = any(
            "Authenticator.user" in getattr(dep.call, "__qualname__", "")
            for dep in dependant.dependencies
        )
        (guarded if needs_auth else public).add(path)

    assert public <= whitelist, (
        f"这些路由**不需要认证**却不在白名单里:{sorted(public - whitelist)};"
        "要么给它加 Depends(auth.user),要么把它写进白名单并说明为什么公开"
    )
    # WS 的令牌校验看不到依赖树(它在函数体里手验),所以单独断言它在源码里真的调了
    source_text = (ROOT / "src" / "project_rag" / "api" / "app.py").read_text(encoding="utf-8")
    assert "user_for_ws_token" in source_text, "WS 的令牌校验不见了"
    # 语料来源是业务数据,必须挂在认证后面（之前它是裸的 —— 这条扫描抓到的)
    assert "/api/v1/sources" in guarded, "/api/v1/sources 又不要求认证了(它是业务数据)"
    # 管理接口:依赖树只看得到"要 Bearer",看不到函数体里的 require_admin ——
    # 所以用**行为**钉(员工 403、无令牌 401),比数一数 require_admin 出现几次结实得多。
    store = _FakeAuth()
    guarded_client = TestClient(create_app(_deps(auth_store_factory=lambda: store)))
    employee = guarded_client.post(
        "/api/v1/auth/login", json={"username": "worker", "password": "pw-123456"}
    ).json()["token"]
    boss = guarded_client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "pw-123456"}
    ).json()["token"]
    calls = (
        ("post", "/api/v1/documents?filename=x.pdf"),
        ("delete", "/api/v1/documents/x.pdf"),
        ("get", "/api/v1/reports/metrics?window=24h"),
    )
    for method, path in calls:
        anon = getattr(guarded_client, method)(path)
        assert anon.status_code == 401, f"{method} {path} 没令牌该 401,实际 {anon.status_code}"
        as_employee = getattr(guarded_client, method)(
            path, headers={"Authorization": f"Bearer {employee}"}
        )
        assert as_employee.status_code == 403, (
            f"{method} {path} 员工该 403(管理动作),实际 {as_employee.status_code}"
        )
        as_boss = getattr(guarded_client, method)(
            path, headers={"Authorization": f"Bearer {boss}"}
        )
        assert as_boss.status_code != 403, f"{method} {path} 管理员不该 403:{as_boss.status_code}"
    return f"公开仅 {sorted(public)};其余 {len(guarded)} 条都要 Bearer;三个管理动作都是 401/403/放行"


# ══ 四、CSP 与 CORS ══════════════════════════════════════════════════════════
@check("CSP：前端产物带 script-src 'self' 与 frame-ancestors 'none'；接口不带")
def _() -> str:
    client = _client()
    page = client.get("/static/app/")
    csp = page.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp, f"没有 script-src 'self':{csp!r}"
    assert "frame-ancestors 'none'" in csp, f"没有 frame-ancestors 'none':{csp!r}"
    assert "object-src 'none'" in csp, csp
    assert page.headers.get("x-content-type-options") == "nosniff", dict(page.headers)
    assert client.get("/api/v1/capabilities").headers.get("content-security-policy") is None, (
        "接口不该带 CSP(JSON 加它没意义,只会让排障时多一层噪声)"
    )
    # 产物必须是**外链脚本**,否则 script-src 'self' 会把页面打死
    html = (ROOT / "src" / "project_rag" / "api" / "static" / "app" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "<script " in html and "src=" in html, "产物里没有外链脚本?"
    assert "<script>" not in html, "产物里有内联脚本:script-src 'self' 会把它挡掉(页面白屏)"
    return "CSP 三条都在;接口无 CSP;产物是外链脚本(不会被自己的 CSP 打死)"


@check("CORS：明确不带凭证；「全开 + 绑非回环」会打 WARNING")
def _() -> str:
    import logging

    from project_rag.api import app as app_module
    from project_rag.config import get_settings

    client = _client()
    # 走 app 对象而不是 TestClient:TestClient.app 的静态类型是 _WrapASGI2,拿不到中间件表
    from project_rag.api import create_app

    cors = [
        middleware
        for middleware in create_app(_deps()).user_middleware
        if "CORSMiddleware" in str(middleware)
    ]
    assert cors, "没找到 CORSMiddleware"
    assert cors[0].kwargs.get("allow_credentials") is False, (
        "CORS 带了凭证:通配来源 + 带凭证 = 任意站点都能带着用户身份打这个服务"
    )

    settings = get_settings()
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    app_module._logger.addHandler(_Sink())
    try:
        app_module._warn_wide_cors(settings)
        wide = len(records)
        narrowed = settings.model_copy(
            update={
                "api": settings.api.model_copy(
                    update={"cors_origins": ["http://localhost:5173"]}
                )
            }
        )
        app_module._warn_wide_cors(narrowed)
        assert wide == 1, f"CORS 全开 + 绑 {settings.api.host} 时该打一条 WARNING,实际 {wide} 条"
        assert len(records) == 1, f"收紧来源之后不该再打,实际又打了 {len(records) - 1} 条"
    finally:
        app_module._logger.handlers = [
            handler for handler in app_module._logger.handlers if not isinstance(handler, _Sink)
        ]
    return "allow_credentials=False;全开打 1 条 WARNING、收紧不打"


# ══ 五、桌面壳（可验证的那一半） ═════════════════════════════════════════════
@check("桌面壳：--help / --check 不需要 GUI 栈；本地模式起回环服务托管产物")
def _() -> str:
    import urllib.request

    from project_rag import desktop

    help_run = subprocess.run(
        [sys.executable, "-m", "project_rag.desktop", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert help_run.returncode == 0, f"--help 退出码 {help_run.returncode}:{help_run.stderr[:200]}"
    assert "--backend" in help_run.stdout and "--remote" in help_run.stdout, help_run.stdout[:200]

    check_run = subprocess.run(
        [sys.executable, "-m", "project_rag.desktop", "--check", "--print-url"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert check_run.returncode == 0, f"--check 退出码 {check_run.returncode}:{check_run.stderr[:200]}"
    url = check_run.stdout.splitlines()[0]
    assert url.startswith("http://127.0.0.1:"), (
        f"本地模式该给一个回环 HTTP 地址(不是 file://:ES module 在 file:// 下会被 CORS 挡掉),实际 {url}"
    )
    assert url.endswith("/index.html"), url
    assert "✔" in check_run.stdout, f"--check 该说清产物没问题:{check_run.stdout}"

    # **真的把服务起起来取一次页面**:这是壳的核心假设(本地托管 + 可配后端),必须验到
    server = desktop._BundleServer(desktop.bundle_index().parent)
    try:
        base = server.start()
        body = urllib.request.urlopen(f"{base}/index.html", timeout=10).read().decode("utf-8")
        assert 'id="app"' in body, "回环服务发出来的不是前端产物"
        asset = re.search(r'src="\./(assets/[^"]+)"', body)
        assert asset, body[:200]
        asset_code = urllib.request.urlopen(f"{base}/{asset.group(1)}", timeout=10).status
        assert asset_code == 200, f"产物里的 JS 取不到:{asset.group(1)} → {asset_code}"
    finally:
        server.stop()

    local = desktop._local_url(desktop._BundleServer(desktop.bundle_index().parent), "http://x:1")
    assert local.endswith("?backend=http://x:1"), local
    remote = desktop.target_url(remote="http://box:8080/", backend=None)
    assert remote == "http://box:8080/static/app/", remote
    return "回环服务真能发出产物与 JS;--backend 拼进 URL;--remote 走后端托管"


@check("桌面壳：产物缺失或引用资源不见了 → 报出来（不静默开一个白窗口）")
def _() -> str:
    import tempfile

    from project_rag import desktop

    original = desktop._BUNDLE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            desktop._BUNDLE = Path(tmp) / "index.html"
            missing = desktop.bundle_problems()
            assert missing and "跑" in " ".join(missing), f"产物不在时报的话不可执行:{missing}"

            # 产物在、但它引用的 assets 不在(最常见的一种坏法:产物是旧的)
            desktop._BUNDLE.write_text(
                '<!doctype html><script type="module" src="./assets/gone.js"></script>',
                encoding="utf-8",
            )
            stale = desktop.bundle_problems()
            assert stale and "gone.js" in " ".join(stale), f"没查出断掉的引用:{stale}"
    finally:
        desktop._BUNDLE = original
    assert desktop.bundle_problems() == [], "还原之后应该没问题"
    return "产物不在 / 引用断掉 两种坏法都报出来,且话术给重建命令"


@check("桌面壳：入口与可选依赖都写进了 pyproject（pywebview 不进运行时依赖）")
def _() -> str:
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    script = data["project"]["scripts"].get("project-rag-desktop")
    assert script == "project_rag.desktop:main", f"控制台入口不对:{script!r}"
    extras = data["project"].get("optional-dependencies", {})
    assert "desktop" in extras, f"没有 desktop 这一档:{sorted(extras)}"
    assert any("pywebview" in dep for dep in extras["desktop"]), extras["desktop"]
    # 主依赖里**不许**有它:主依赖进镜像,而 GUI 栈在服务器上用不到(装机体积白涨)
    main_deps = " ".join(data["project"]["dependencies"])
    assert "pywebview" not in main_deps, "pywebview 跑进主依赖了(它会进镜像)"
    return f"入口 {script};desktop extra = {extras['desktop']};主依赖里没有 pywebview"


# ══ 六、前端静态 ═════════════════════════════════════════════════════════════
@check("前端：限流那一条用暖色提醒 + 说明「并非系统异常」，不是红字错误")
def _() -> str:
    messages = _read_web("components/MessageList.vue")
    assert "throttled" in messages, "前端不认识 channel=throttled(会显示成光秃秃一句话)"
    assert re.search(r"throttled.*'稍后重试'|'稍后重试'", messages), "没有给 throttled 一个标签"
    assert re.search(r"badgeTone|tag warn", messages), (
        "限流那条没有单独的配色(与普通答案混在一起,用户看不出该等一会儿)"
    )
    assert "并非系统异常" in messages, "没有告诉用户「这不是错误」(不然他会去狂点重试)"
    # 提示**不能画成红字**:`.alert.error` 是"出错了"的语气,与话术的意思正好相反
    assert re.search(r"channel === 'throttled'\" class=\"alert info\"", messages), (
        "限流那条提示用的不是 info 语气(红字会让人以为系统坏了)"
    )
    types = _read_web("api/types.ts")
    assert "'throttled'" in types, "types.ts 的 Channel 里没有 throttled"
    return "标签「稍后重试」+ 暖色徽章 + 「并非系统异常」说明三处都在"


@check("前端：?backend= 只认一次并抹掉；登出先调接口再清本地")
def _() -> str:
    main = (WEB_SRC / "main.ts").read_text(encoding="utf-8")
    client = _read_web("api/client.ts")
    auth = (WEB_SRC / "stores" / "auth.ts").read_text(encoding="utf-8")

    assert "seedBackendFromQuery()" in main, "挂载前没有认 ?backend="
    assert main.index("seedBackendFromQuery()") < main.index("createApp(App)"), (
        "认参数必须发生在建 app 之前(组件渲染时就要读后端地址)"
    )
    assert "replaceState" in client and "params.delete('backend')" in client, (
        "认完之后没有把 ?backend= 从地址栏抹掉"
    )
    assert "https?:" in client, "没有校验它像不像一个地址"
    assert "await api.logout()" in auth, "退出没有调服务端的登出(只删了本地副本)"
    assert "clearAuth()" in auth, "退出没有清本地"
    return "?backend= 挂载前认一次 + 抹掉;退出 = 先撤销后清本地"


def main() -> int:
    return report(
        "安全收口（限流 / 登出撤销 / 路由守卫 / CSP / CORS）+ 桌面壳",
        hint="前端改动后先 `cd web && npm run build`；桌面壳开窗要图形界面,这台机器验不了",
    )


if __name__ == "__main__":
    sys.exit(main())

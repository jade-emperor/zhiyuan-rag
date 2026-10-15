"""FastAPI 入口:把合流问答暴露成 HTTP + WebSocket。

**这一版是纯异步的。** 上一版把 HTTP 端点写成同步 ``def``、靠 FastAPI 自动丢线程池,
方向是反的:WS 那条已经是 ``async def``,两条入口两种姿势,迟早不一致;更麻烦的是
「哪段代码会阻塞循环」被藏进了框架的自动行为里,读代码看不出来。

「纯异步」在这里的**准确含义**得说清,否则就是自欺欺人:

* **api/ 这一层全是协程** —— 每个端点都是 ``async def``,端点体内没有一次同步阻塞调用;
* **同步的活(适配器)全部在 ``await`` 后面进 anyio 的线程池** —— 两道缝:``_call`` 搬
  「一次同步调用」,``_in_thread`` 搬「一整条同步帧流」。

为什么不能再纯一点:``pymysql`` / ``pymilvus`` / ``langchain`` / ``torch`` 全是同步库
一个请求把循环占住,并发时所有请求一起排队。

**这两道缝也正是将来的迁移点**:等适配器换成异步实现(aiomysql / ``AsyncOpenAI`` /
pymilvus 的 async 客户端),把 ``_call`` 里的 ``run_in_threadpool`` 换成直接 ``await``
即可,端点的形状一行都不用改 —— 这就是「现在就写成异步」的意义。

接口层的三个决定:

1. **``POST /api/v1/query`` 直接给完整答案**,不要求前端换 WebSocket 才拿得到结果 ——
   我们有同步用例,强制换协议是更差的契约;要逐字出字才走 WS。
2. **过滤走 ``RetrievalOptions.source``,不引 ``VALID_SOURCES`` 白名单**:白名单该从哪来
   还没定论(见 ``deferred.md`` §1.3 第 22 条);而 ``/api/v1/sources`` 直接从向量库读**真实**
   来源,比在配置里写死一份更可信。
3. **问候拦截放在 ``application/greeting.py``**:「什么算寒暄」是业务规则,HTTP 与 WS
   共用一份,不写在接口层。
4. **业务接口一律在 ``/api/v1`` 下** —— 从 ``/api/*`` 统一迁过来(P0-3b-5-②),
   **没有留别名**:两套路径就是两个真值来源,契约要写两遍,而前端只要一个前缀。
   留在根上的只有 ``/health``(探活)、``/``(跳前端 app)与 ``/static``(静态资源,
   前端产物在 ``/static/app/``)—— 它们不是业务接口,也不该跟着 API 版本走。这条有契约钉着(``check_m5_api`` 的「命名空间」那条):漏一个
   老式路径进去,前端的 TS 客户端就会生成两个前缀。

装配复用 ``cli/deps.py`` 那一套(``Deps`` / ``build_real_deps`` / ``knowledge_callable``):
**组合根只该有一套**,api 再写一份装配,两份迟早不一致。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from fastapi import (
    BackgroundTasks,
    Depends,
    Header,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from project_rag.api.auth import Authenticator
from project_rag.api.schemas import (
    K_MAX,
    TOP_MAX,
    MAX_CONTEXT_MAX,
    RRF_K_MAX,
    CapabilitiesOut,
    ChunkPreviewOut,
    CitationOut,
    CostLineOut,
    CostOut,
    PeriodTotalOut,
    CreateGroupRequest,
    CreateUserOut,
    CreateUserRequest,
    DeletedDocumentOut,
    DeletedGroupOut,
    DeletedOut,
    DeletedUserOut,
    DocumentAclOut,
    DocumentOut,
    DocumentPreviewOut,
    DocumentsOut,
    DocumentSummaryOut,
    GroupOut,
    GroupsOut,
    HealthOut,
    HistoryOut,
    HistoryTurnOut,
    LoginOut,
    LoginRequest,
    LogoutOut,
    MeOut,
    MetricsOut,
    MetricsCapabilitiesOut,
    MetricsMinNOut,
    ModelOut,
    ModelSettingsOut,
    QueryRequest,
    QueryResponse,
    ResetPasswordRequest,
    RetrievalDefaultsOut,
    RetrievalLimitsOut,
    RetrievalOut,
    SegmentOut,
    SessionOut,
    SessionSummaryOut,
    SessionsOut,
    SetDocumentAclRequest,
    SetMembersRequest,
    SlowRowOut,
    SourcesOut,
    UserOut,
    UsersOut,
)
from project_rag.application import Session, answer_question, stream_question
from project_rag.application.access import UNRESTRICTED, AccessContext, context_of
from project_rag.application.accounts import (
    check_delete_account,
    check_group_name,
    check_new_account,
    check_new_password,
    role_of,
)
from project_rag.application.security import TOKEN_TTL_SECONDS
from project_rag.application.greeting import match_greeting
from project_rag.cli.deps import (
    Deps,
    attach_ledger,
    build_real_deps,
    ensure_access_schema,
    knowledge_callable,
)
from project_rag.config import PROJECT_ROOT, get_settings, project_path
from project_rag.domain import (
    FUSION_MODES,
    RETRIEVAL_MODES,
    Citation,
    QaAnswer,
    QaEvent,
    RetrievalMode,
    RetrievalOptions,
)
from project_rag.domain.access import DocumentAcl, Group
from project_rag.domain.auth import USER_ROLES, User
from project_rag.ports import RateLimiter
from project_rag.observability import get_logger

_logger = get_logger("api.app")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 报表窗口白名单(小时数)。与 CLI 的 `metrics --window` **同一套**:两处能选的窗口不一样,
# 会出现"命令行看得到、接口看不到"的怪事。改这里要同步改 cli/commands/metrics.py。
_WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}

_T = TypeVar("_T")


# ── 两道缝：api/ 里所有同步代码都从这里进线程池 ───────────────────────────────
async def _call(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """输入:同步可调用 + 参数。行为:整体挪到 anyio 的线程池里跑。输出:它的返回值。

    第一道缝:搬「一次同步调用」(一整条 HTTP 请求、一次库操作、一次工厂构造)。

    为什么不用 ``asyncio.to_thread``:它每次调用新建一个线程、**没有上限** —— 突发流量
    能把线程数打爆;Starlette 的 ``run_in_threadpool`` 走 anyio 的 ``CapacityLimiter``
    (默认 40 个 worker),超额就排队等空位,服务端要的是**限流**而不是放开。

    代价说清楚:池子里的活**不可取消**(客户端中途断开也会跑完)。要能取消,得等适配器
    自己变成异步的 —— 那时这道缝就该拆掉,而不是在这里加超时。
    """
    return await run_in_threadpool(fn, *args, **kwargs)


async def _in_thread(events: Iterator[QaEvent]) -> AsyncIterator[QaEvent]:
    """输入:同步帧流。行为:在**线程池**里逐帧取,取完结束。输出:异步帧流。

    第二道缝:``stream_question`` 内部是同步的 LLM 流 + 同步 MySQL,直接在事件循环里
    迭代它,一个用户的流会把所有人的请求都卡住。WebSocket 那条没有
    FastAPI 的自动线程池,所以逐帧搬。
    """
    iterator = iter(events)
    while True:
        item = await _call(_next_or_none, iterator)
        if item is None:
            return
        yield item


def _next_or_none(iterator: Iterator[QaEvent]) -> QaEvent | None:
    """输入:同步迭代器。行为:取下一帧,取完给 None。输出:帧或 None。

    单独抽出来是为了让 ``_call`` 收到一个**形状明确的同步可调用**:直接把
    ``next(it, None)`` 这种带默认值的调用当参数传,类型检查器挑不出匹配的重载。
    """
    return next(iterator, None)


# ── 同步核心：装端口、调用例、转出参（都在线程池里跑） ─────────────────────────
def _optional_factory(deps: Deps, name: str) -> Any:
    """输入:依赖集合 + 工厂字段名。行为:有就造,没配给 None(**不报 503**)。输出:实例或 None。

    与 ``_factory`` 的分工:``_factory`` 用于"这个能力是必须的"(没配就 503);
    这里用于"没配是一种合法模式"(认证:注入式测试刻意不开认证)。
    """
    factory = getattr(deps, name, None)
    return None if factory is None else factory()


def _factory(deps: Deps, name: str) -> Any:
    """输入:依赖集合 + 工厂名。行为:取到一个非空工厂并调用它。输出:端口实例。

    没装配就 503(不是 500):「服务没配好」和「服务崩了」对前端是两件事。

    **注意它是同步函数**:工厂可能造重物(BGE-M3 加载 1.2s、上 G 内存),所以调用点
    必须在 ``_call`` 里 —— 在事件循环里调它就等于把循环按在那里。
    """
    factory = getattr(deps, name, None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail=f"{name} 没装配:检查 build_real_deps"
        )
    return factory()


def _session(deps: Deps, session_id: str | None) -> Session | None:
    """输入:依赖 + 会话 ID。行为:给了 ID 就绑一个 Session。输出:Session;没给 ID 给 None。"""
    if not session_id:
        return None
    conversation = get_settings().conversation
    return Session(
        store=_factory(deps, "conversation_store_factory"),
        session_id=session_id,
        # 滑动窗口的两半:轮数(读写共用)+ 整个历史块的字符预算(只影响进提示词的那一份)
        rounds=conversation.history_rounds,
        max_chars=conversation.history_max_chars,
    )


def _metrics_capabilities() -> MetricsCapabilitiesOut:
    """输入:无。行为:把报表的白名单与样本量门槛报给前端。输出:能力模型。

    数字全部**从真源读**:窗口来自路由用的那张表、样本量来自 `application/metrics.py`
    的常量。抄一份到这里就是第二个真值来源 —— 后端加了窗口前端不知道(或反过来)。
    """
    from project_rag.application.metrics import MIN_N_P50, MIN_N_P95, MIN_N_P99

    return MetricsCapabilitiesOut(
        windows=list(_WINDOWS),
        default_window="24h",
        top_max=TOP_MAX,
        min_n=MetricsMinNOut(p50=MIN_N_P50, p95=MIN_N_P95, p99=MIN_N_P99),
    )


# ── 限流(M7.4):计数在组合根给的实现里(默认 Redis,见 adapters/ratelimit_redis.py);
#    这一层只管**策略**:哪几档、按谁计数、超了回什么 ──────────────────────
def _rate_limit_key(connection: HTTPConnection, user: User | None) -> str:
    """输入:连接(HTTP 或 WebSocket)+ 当前用户。行为:算出这一档按谁计数。输出:键。

    **登录时按 IP、登录后按账号**:登录那一刻还没有身份,只能按来源 IP 记;
    有了账号就该按账号 —— 否则同一个办公室的同事会互相挤占额度(同一个出口 IP)。
    """
    if user is not None:
        return f"user:{user.username}"
    client = connection.client
    return f"ip:{client.host if client is not None else 'unknown'}"


def _rate_limit_check(
    limiter: RateLimiter | None, bucket: str, key: str, limit: int, window_s: float
) -> int:
    """输入:限流器(可为 None = 不限流)、档位名、键、上限、窗口。行为:判定一次。

    输出:``0`` = 放行;``> 0`` = 被限,值是建议等待秒数。

    **刻意不在这里抛异常**:同一个判定有三种出口 —— 问答回兜底话术(200)、
    登录/上传回 429、WebSocket 回帧;"怎么回"是各条路径的事,判定只回答"让不让过"。
    """
    if limiter is None:
        return 0
    return limiter.check(f"{bucket}:{key}", limit=limit, window_s=window_s)


# 档位 → 人话里的名字。只在这一处:429 的 detail 与问答的兜底话术共用它,
# 两处各写一份的话,同一件事会出现两种说法。
_BUCKET_NAMES = {"query": "提问", "upload": "上传", "login": "登录尝试"}


def _throttle_text(bucket: str, limit: int, retry_after: int, window_s: float) -> str:
    """输入:档位、上限、建议等待秒数、窗口。行为:拼一句**可执行**的话。输出:文本。

    **必须带秒数**:只说"请求过于频繁"的话,用户只能靠反复重试去猜(法律 39 的同一条规矩)。

    **问答与另外两档的说法不一样**,因为场景不一样:问答是"我在跟你聊天,先缓一下"(那是
    兜底话术,200);登录与上传是要**说清这是被拦了**(429)。第一版三档共用问答那句,
    于是**登录页上出现了"问得有点密集"** —— 登录页上没有人"问"什么(实测:浏览器用例
    期望的是"请求过于频繁",而那两条断言因为假后端一直没接限流器而长期没人跑到)。
    """
    minutes = max(1, int(window_s // 60)) if window_s >= 60 else None
    quota = f"{minutes} 分钟最多 {limit} 次" if minutes else f"{int(window_s)} 秒内最多 {limit} 次"
    if bucket != "query":
        what = _BUCKET_NAMES.get(bucket, "请求")
        return f"{what}过于频繁,请等约 {retry_after} 秒再试(这一档上限:{quota})。"
    return (
        f"问得有点密集,我需要缓一下 —— 请等约 {retry_after} 秒再试(这一档上限:{quota})。"
    )


def _enforce_rate_limit(
    limiter: RateLimiter | None, bucket: str, key: str, limit: int, window_s: float
) -> None:
    """输入:同 `_rate_limit_check`。行为:超限就**抛 429**(带 ``Retry-After``)。输出:无(放行)。

    给**没有"答案"可兜底**的那两条路用:
    * 登录 —— 兜底话术会看着像"登录成功了",那是安全上最糟的一种回法;
    * 上传 —— 管理员点的是按钮,需要的是"为什么没传上去",不是一段"回答"。
    """
    retry_after = _rate_limit_check(limiter, bucket, key, limit, window_s)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_throttle_text(bucket, limit, retry_after, window_s),
            headers={"Retry-After": str(retry_after)},
        )


# 前端 app 的 CSP。**脚本只许来自本站**:Vite 的产物全是外链脚本(没有内联),
# 所以这一条不会误伤;而它挡住了"往页面里注入一段 script"这类 XSS ——
# 令牌还在 localStorage 里(M7.4 的已知缺口,见 deferred),CSP 是那道兜底。
# `connect-src *`:这个 app 的整个设计就是"填一个后端地址就能连",所以不能限死来源;
# 想限死的人该收紧的是后端地址白名单,不是 CSP。
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src *; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _warn_wide_cors(settings) -> None:  # noqa: ANN001 - Settings,避免 import 环
    """输入:配置。行为:对"CORS 全开 + 绑非回环地址"这个组合打一条 WARNING。输出:无。

    **为什么不拦启动**:本机开发就该全开(Docker/WSL 里来源五花八门),拦了会把人逼去
    改代码。而这个组合确实是"任何站点都能拿用户的浏览器打这个服务" ——
    所以留一条日志痕:出事时至少查得到"当时是全开的"。
    """
    origins = settings.api.cors_origins
    host = settings.api.host
    loopback = ("127.0.0.1", "localhost", "::1")
    if "*" in origins and host not in loopback:
        _logger.warning(
            "CORS 全开(cors_origins=['*'])且绑在 %s:任何站点都能拿用户的浏览器打这个服务。"
            "对外部署时把 api.cors_origins 收紧成具体来源。",
            host,
        )


def _weights(request: QueryRequest, *, bm25: float, vector: float) -> tuple[float, float]:
    """输入:请求 + 配置里的两个权重。行为:把 mode / 权重合成最终的两路权重。输出:(bm25, vector)。

    **``mode`` 只活在接口层**:领域层的 ``RetrievalOptions`` 没有这个字段 ——
    多一个字段就有两个真值来源("权重说关、模式说开"时谁赢?)。所以翻译在这里做,
    而"权重 ≤ 0 的那一路不跑"这条语义在用例层(``answer_rag.retrieve``)。

    校验已经由 ``QueryRequest`` 挡在前面(mode 与权重不能同时给),这里只做合成。
    """
    if request.bm25_weight is not None or request.vector_weight is not None:
        return (
            request.bm25_weight if request.bm25_weight is not None else bm25,
            request.vector_weight if request.vector_weight is not None else vector,
        )
    mode: RetrievalMode = request.mode or "hybrid"
    if mode == "vector":
        return 0.0, vector  # 关掉关键字那一路
    if mode == "keyword":
        return bm25, 0.0  # 关掉向量那一路
    return bm25, vector


def _options(deps: Deps, request: QueryRequest, access: AccessContext) -> RetrievalOptions:
    """输入:依赖 + 请求 + 可见范围结论。行为:配置里的旋钮、请求里的覆盖、可见范围拼成一次检索的参数。输出:RetrievalOptions。

    口径四条:
    1. **请求里给了就用请求的,没给就用 yaml 的** —— 默认值只有一处真值来源;
    2. ``mode`` 翻译成权重(见 ``_weights``);
    3. ``rerank`` **不进 options** —— 它由"传不传重排器"表达(见 ``_rerank_enabled``
       与 ``cli/commands/rag.py``,两处同一个口径);
    4. ``denied_docs`` 来自可见范围,**不接受请求覆盖** —— 没人能靠改一次请求体
       把自己的受限文档"要"回来(那正是这一整套东西存在的意义)。
    """
    settings = get_settings()
    bm25_weight, vector_weight = _weights(
        request, bm25=settings.rag.bm25_weight, vector=settings.rag.vector_weight
    )
    return RetrievalOptions(
        k=request.k if request.k is not None else settings.rag.retrieval_k,
        max_context=(
            request.max_context if request.max_context is not None else settings.rag.candidate_m
        ),
        source=request.source_filter,
        fusion=request.fusion if request.fusion is not None else settings.rag.fusion,
        rrf_k=request.rrf_k if request.rrf_k is not None else settings.rag.rrf_k,
        bm25_weight=bm25_weight,
        vector_weight=vector_weight,
        denied_docs=tuple(sorted(access.denied)),
    )


def _rerank_enabled(deps: Deps, request: QueryRequest) -> bool:
    """输入:依赖 + 请求。行为:这一次要不要装重排器。输出:布尔。

    显式要重排、而这个部署根本没配重排器时**当场 400** —— 不静默降级成"不重排":
    用户勾了重排却拿到没重排的结果,那是看不见的谎(法律 32)。不给这个字段则按配置。
    """
    if request.rerank is None:
        return get_settings().rag.rerank
    if request.rerank and deps.reranker_factory is None:
        raise HTTPException(
            status_code=400,
            detail="这个部署没有配重排器(reranker_factory 为空),rerank=true 无法满足;"
            "去掉 rerank 字段就用配置里的默认值",
        )
    return request.rerank


def _citations(citations: Iterable[Citation]) -> list[CitationOut]:
    """输入:引用序列。行为:转成出参模型(预览用父块正文,模型真读到的那段)。输出:出参列表。

    ``page`` 从引用上取(它又问子块),None 就是"不知道" —— 这里不做任何兜底,
    编一个页号比没有页号糟得多。
    """
    return [
        CitationOut(
            index=citation.index,
            source=citation.chunk.source,
            doc_id=citation.chunk.doc_id,
            preview=citation.chunk.context_text[:120],
            score=citation.score,
            rerank_score=citation.rerank_score,
            page=citation.page,
        )
        for citation in citations
    ]


def _to_response(result: QaAnswer, session_id: str | None) -> QueryResponse:
    """输入:用例回答 + 会话 ID。行为:转成出参模型。输出:QueryResponse。"""
    return QueryResponse(
        answer=result.answer,
        channel=result.channel,
        refused=result.refused,
        truncated=result.truncated,
        citations=_citations(result.citations),
        faq_score=result.faq_match.score if result.faq_match is not None else None,
        session_id=session_id,
    )


def _frame(event: QaEvent, session_id: str | None) -> dict[str, Any]:
    """输入:一帧 + 会话 ID。行为:转成 WebSocket 的 JSON 帧。输出:可发送的字典。

    帧名与 ``application/streaming.py`` 的协议一一对应(``sources`` / ``token`` / ``done``),
    前端不需要认识领域类型。
    """
    if event.kind == "sources":
        return {
            "type": "sources",
            "citations": [c.model_dump() for c in _citations(event.citations)],
        }
    if event.kind == "token":
        return {"type": "token", "text": event.text}
    return {
        "type": "done",
        "answer": event.answer,
        "channel": event.channel,
        "refused": event.refused,
        "truncated": event.truncated,
        "citations": [c.model_dump() for c in _citations(event.citations)],
        "session_id": session_id,
    }


def _answer(
    deps: Deps, request: QueryRequest, access: AccessContext
) -> QueryResponse:
    """输入:依赖 + 请求 + 可见范围。行为:同步地把一次问答跑完(装端口 → 用例 → 出参)。输出:QueryResponse。

    **一次请求只跳一次线程**:工厂可能造重物、用例要做检索与生成,把它们全放进这一个
    同步函数里让 ``_call`` 一次搬完,比「每个端口跳一次」好推理,也不会漏掉某个调用点。
    """
    result = answer_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps,
            request.query,
            _options(deps, request, access),
            rerank=_rerank_enabled(deps, request),
        ),
        faq_threshold=deps.faq_threshold,
        session=_session(deps, request.session_id),
        thinking=deps.thinking,  # 只进延迟账,不影响答案
    )
    return _to_response(result, request.session_id)


def _stream_events(
    deps: Deps, request: QueryRequest, access: AccessContext
) -> Iterator[QaEvent]:
    """输入:依赖 + 请求 + 可见范围。行为:同步地把流式用例的帧流**搭起来**(装端口)。输出:同步帧迭代器。

    重活(嵌入/检索/生成)发生在迭代它的时候,那时已经在 ``_in_thread`` 的池子里了;
    但「搭起来」这一步要调工厂(可能加载模型),所以它本身也得在 ``_call`` 里执行。
    """
    return stream_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps,
            request.query,
            _options(deps, request, access),
            rerank=_rerank_enabled(deps, request),
        ),
        faq_threshold=deps.faq_threshold,
        session=_session(deps, request.session_id),
        thinking=deps.thinking,  # 只进延迟账,不影响答案
    )


def _sources(deps: Deps) -> SourcesOut:
    """输入:依赖。行为:读向量库里的全部块,取来源去重升序。输出:SourcesOut。"""
    chunks = _factory(deps, "vector_store_factory").load_all()
    return SourcesOut(sources=sorted({chunk.source for chunk in chunks}))


def _history(deps: Deps, session_id: str) -> HistoryOut:
    """输入:依赖 + 会话 ID。行为:读最近 N 轮(与喂给模型的**同一份**)。输出:HistoryOut。"""
    store = _factory(deps, "conversation_store_factory")
    turns = store.recent(session_id, limit=get_settings().conversation.history_rounds)
    return HistoryOut(
        session_id=session_id,
        rounds=[
            HistoryTurnOut(question=turn.question, answer=turn.answer) for turn in turns
        ],
    )


def _delete_history(deps: Deps, session_id: str) -> DeletedOut:
    """输入:依赖 + 会话 ID。行为:清空这一段会话。输出:删掉的轮数。"""
    store = _factory(deps, "conversation_store_factory")
    return DeletedOut(session_id=session_id, deleted=store.clear(session_id))


def _greeting_response(reply: str, session_id: str | None) -> QueryResponse:
    """输入:问候话术 + 会话 ID。行为:包成出参。输出:QueryResponse。

    寒暄**不查库、不走向量检索、也不写历史**:它没有任何需要
    检索的内容,写进历史只会给下一轮的提示词添噪声。
    """
    return QueryResponse(answer=reply, channel="greeting", session_id=session_id)


# 同一时刻只跑一个入库任务:嵌入模型是 CPU 密集的,并发跑只会让两边都慢十倍,
# 而且两份文档的块交错写库更难排查。**进程内锁**够用(单进程部署);
# 多进程部署要换成 Redis 锁或独立 worker —— 记在 deferred。
_INGEST_LOCK = threading.Lock()


async def _stream_to_file(request: Request, target: Path, max_bytes: int) -> int:
    """输入:请求、落盘路径、上限字节数。行为:**边收边写边数**,超限/中断就删掉半截文件并报 413。输出:字节数。

    **为什么不用 multipart**:FastAPI 的 ``UploadFile`` 需要 ``python-multipart`` 这个额外依赖,
    而浏览器的 ``fetch(url, {method: "POST", body: file})`` **原生就把文件当请求体发** ——
    不需要 FormData、不需要新依赖、前端还少一层编码。代价是文件名得走 query 参数
    (所以下面那句"只取 basename"更要紧了)。

    **为什么边收边写**:一次性 ``await request.body()`` 等于让一个 2G 的文件先把内存吃光
    再判断超限。分块收 + 立刻落盘,超限时删掉半截文件,不留垃圾。

    **为什么先写 ``<名字>.part`` 再改名**:知识库清单是"磁盘上有什么"的视图,
    而**传到一半的文件不是一份文档**。中途失败、客户端断线留下的半截文件会一直挂在清单上
    显示成「未入库」,用户只能自己去磁盘上删。``.part`` 不在认识的后缀里(磁盘索引按后缀过滤),
    所以半截文件对清单**不存在**;``os.replace`` 在同一文件系统内是原子改名 ——
    要么是完整文件,要么什么都没有。收尾把 ``.part`` 清掉:那是垃圾,不是"下次续传"。
    """
    part = target.with_name(f"{target.name}.part")
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with part.open("wb") as sink:
            async for block in request.stream():
                written += len(block)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {max_bytes // (1024 * 1024)}MB 上限",
                    )
                sink.write(block)
        os.replace(part, target)
    except BaseException:
        # 413 / 客户端断线 / 磁盘满 / 任务被取消 —— 无论哪种,半截文件都不许留下。
        # 用 BaseException 而不是 Exception:被取消时也要清,否则留下垃圾。
        part.unlink(missing_ok=True)
        raise
    return written


def _run_ingest(deps: Deps, record, path: Path) -> None:
    """输入:依赖、文档记录、文件路径。行为:串行地跑一次入库,把状态推进。输出:无。

    跑在后台任务里(响应已经发回去了)。**异常在用例里已经写成 failed 状态**,
    这里只记日志 —— 后台任务里再抛没人接,只会变成一条丑陋的 traceback。
    """
    from project_rag.application.documents import ingest_uploaded

    settings = get_settings()
    with _INGEST_LOCK:
        try:
            ingest_uploaded(
                _factory(deps, "document_store_factory"),
                record,
                path,
                chunker=_factory(deps, "chunker_factory"),
                embedder=_factory(deps, "embedder_factory"),
                vector_store=_factory(deps, "vector_store_factory"),
                batch_size=settings.rag.ingest_batch_size,
                load=_factory(deps, "loader_factory"),
                # 稀疏编码器只在 `keyword.backend=milvus` 那一档装配(见 cli/deps.py);
                # 没装配就是 None —— 用例据此决定"这批块要不要算稀疏向量"
                sparse_encoder=_optional_factory(deps, "sparse_encoder_factory"),
            )
        except Exception as exc:  # noqa: BLE001 - 状态已记 failed,这里只留日志
            _logger.error("后台入库失败 %s:%s", record.filename, exc)


def _uploads_dir() -> Path:
    """输入:无。行为:上传目录的绝对路径(相对路径按**项目根**解析,与 models/data 同一套)。输出:路径。"""
    return project_path(get_settings().api.uploads_dir)


def _corpus_dir() -> Path:
    """输入:无。行为:语料根目录的绝对路径(同一套解析:相对项目根)。输出:路径。"""
    return project_path(get_settings().api.corpus_dir)


def _disk_files(deps: Deps) -> tuple[Any, ...]:
    """输入:依赖。行为:取一份**磁盘索引**(上传目录 + 语料目录里的文件与大小)。输出:``DiskFile`` 元组。

    没装配时给空表:那时清单只剩登记表那一处,``state`` 会一片 ``file_missing`` ——
    与其编一个"文件都在"的假象,不如让没装配的部署如实显示"磁盘这一处我没看"
    (契约里有断言盯着"真装配一定有它")。
    """
    if deps.disk_files_factory is None:
        return ()
    return tuple(deps.disk_files_factory())


def _no_such_document(document_id: str) -> HTTPException:
    """输入:文档 ID。行为:造那句"没有这份文档"的 404。输出:``HTTPException``。

    **为什么两种情形共用一句**:① 登记表里没有(可能是孤儿块,文献 39 指一条能走的路);
    ② 这份文档被收窄了、而调用方不在名单里。第一种要给出排障入口,第二种一个字都不该多说 ——
    合起来唯一的做法就是**都说第一种那句**:它不泄露存在性,而且对第一种情形仍然有用。
    """
    return HTTPException(
        status_code=404,
        detail=(
            f"登记表里没有这份文档({document_id!r})。先跑 `project-rag doctor` 看有没有"
            "「库里有块、登记表没有行」的孤儿块;那种块要用 `project-rag doctor --prune-orphans` 清,"
            "或者把对应语料重新灌一次"
        ),
    )


def _access(deps: Deps, user: User | None) -> AccessContext:
    """输入:依赖 + 当前用户。行为:问出「这次能看哪些文档」。输出:``AccessContext``。

    **读不到就 503,不放行**(与 ``api/auth.py`` 里"连不上认证库就 503"同一条口径):
    把一次数据库抖动变成"所有文档都可见"是最坏的一种失败方式 —— 它静默、而且方向正好是放宽。

    没接可见范围时给不受限的空上下文:那**只可能来自显式注入**(``create_app(Deps())``
    或验收脚本),``build_real_deps`` 永远把它接上 —— 所以生产路径上没有"没配权限"这一档。
    """
    store = _optional_factory(deps, "access_store_factory")
    if store is None or user is None:
        # user is None = 无认证模式(注入式测试):没有"这个人",也就没有"他能看什么"。
        return UNRESTRICTED
    try:
        rules = store.rules()
        groups = store.groups_by_user().get(user.user_id, ())
    except Exception as exc:  # noqa: BLE001 - 翻成 503:读不到规则时**不静默放行**
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"读不到文档可见范围({type(exc).__name__}: {exc});"
            "宁可拒绝也不放行 —— 放行等于把一次数据库抖动变成一次全网可见",
        ) from exc
    return context_of(
        rules, user_id=user.user_id, group_ids=groups, sees_all=user.sees_all_documents
    )


def _documents(deps: Deps, access: AccessContext) -> DocumentsOut:
    """输入:依赖 + 可见范围。行为:登记表 + 磁盘 + 向量库**三处对账**后给清单(再按可见范围过滤)。输出:清单。

    **为什么不是"读一遍登记表"**:登记表是一份没人失效的缓存 —— CLI 灌完不写它、
    有人 rm 一个语料文件不写它、drop 一个集合更不写它。只念登记表的结果就是
    「集合清空了卡片还写已入库 677 块」「语料目录里新放的文件前端根本看不到」
    (作者报的那个 bug)。这里把三处摆到一起,``state`` 说的才是"现在到底怎么样"。

    **被收窄且没授权给他的文档不出现在清单里**(P0-6):清单是"你能检索什么"的视图,
    列出一份他既搜不到、点预览还会 403 的文档,只会让他以为系统坏了。
    每行的 ``restricted`` 仍然如实给出 —— 那是"这份是限定的,你在名单里"。
    """
    from project_rag.application.documents import document_views, list_documents, summarize

    records = list_documents(_factory(deps, "document_store_factory"))
    views = document_views(
        records,
        files=_disk_files(deps),
        indexed=_factory(deps, "vector_store_factory").chunk_counts(),
    )
    restricted = _restricted_documents(deps)
    visible = [view for view in views if access.allows(view.document_id)]
    summary = summarize(visible)
    return DocumentsOut(
        documents=[
            DocumentOut(
                document_id=view.document_id,
                source=view.source,
                filename=view.filename,
                status=view.status,
                state=view.state,
                chunks=view.chunks,
                size_bytes=view.size_bytes,
                disk_size_bytes=view.disk_size_bytes,
                on_disk=view.on_disk,
                created_at=view.created_at,
                error=view.error,
                searchable=view.searchable,
                indexed_chunks=view.indexed_chunks,
                restricted=view.document_id in restricted,
            )
            for view in visible
        ],
        summary=DocumentSummaryOut(
            total=summary.total,
            ok=summary.ok,
            changed=summary.changed,
            not_indexed=summary.not_indexed,
            file_missing=summary.file_missing,
            unregistered=summary.unregistered,
            in_flight=summary.in_flight,
        ),
        uploads_dir=str(_uploads_dir()),
        corpus_dir=str(_corpus_dir()),
    )


def _restricted_documents(deps: Deps) -> frozenset[str]:
    """输入:依赖。行为:取出"被收窄过"的文档 ID。输出:集合(没接可见范围时为空)。

    清单上要标一句"这是限定的",而"有没有规则"这件事只有规则表知道
    (向量库、登记表都不记它)。读一次全表:规则通常只有几条,而清单每次刷新都会问。
    """
    store = _optional_factory(deps, "access_store_factory")
    if store is None:
        return frozenset()
    return frozenset(store.rules())


def _document_preview(
    deps: Deps, document_id: str, limit: int, chars: int, access: AccessContext
) -> DocumentPreviewOut:
    """输入:依赖 + 文档 ID + 两个上限 + 可见范围。行为:查得到**而且他有权看**才给预览(折叠父块)。输出:预览。

    **先查登记表**:向量库里可能有登记表没有的孤儿块(P0-3b 的对账正是抓这个)。
    那种块照样能被检索到,但"这份文档"在管理页上不存在 —— 给 404 并**指一条能走的路**
    (法律 39),而不是回一份没有主人的预览。

    **看不见的文档一律 404(不是 403)**:403 等于告诉他"这份文档存在,只是不给你看" ——
    受限文档的**存在本身**也是信息。404 与"没这份文档"完全一样,这一点是刻意的。
    """
    from project_rag.application.documents import preview_document

    # **看不见的与不存在的回同一句话**:403 会把"这份文档存在、只是不给你看"说出来,
    # 而"存在本身"也是信息(名字往往就是内容:XX客户报价单)。所以这里不是"回 404 就行",
    # 而是**逐字相同**的 404 —— 两种情形必须无法区分。
    if not access.allows(document_id):
        raise _no_such_document(document_id)
    store = _factory(deps, "document_store_factory")
    if store.get(document_id) is None:
        raise _no_such_document(document_id)
    preview = preview_document(
        _factory(deps, "vector_store_factory"), document_id, limit=limit, chars=chars
    )
    return DocumentPreviewOut(
        document_id=preview.document_id,
        parent_blocks=preview.parent_blocks,
        child_chunks=preview.child_chunks,
        returned=len(preview.blocks),
        truncated=preview.truncated,
        blocks=[
            ChunkPreviewOut(
                index=block.index,
                page=block.page,
                last_page=block.last_page,
                chars=block.chars,
                text=block.text,
            )
            for block in preview.blocks
        ],
    )

def _source_file(deps: Deps, document_id: str) -> Any:
    """输入:依赖 + 文档 ID。行为:在**同一份磁盘索引**里找这份文档的原件。输出:路径;找不到给 None。

    **为什么走索引而不是拼 ``uploads_dir / filename``**:清单上显示的"磁盘那份"与删除时
    动手的"那份"必须是同一个。拿 ``filename`` 直接拼,对语料文档拼出来的是上传目录下的
    同名文件(存在的话就是**别人的文件**),而真正的语料源文件在 ``data/corpus/**`` 里 ——
    删错东西是不可逆的,这里不省这一步。
    """
    for file in _disk_files(deps):
        if document_id in file.doc_ids:
            return file.path
    return None


def _delete_document(deps: Deps, document_id: str):
    """输入:依赖 + 文档 ID。行为:标记→删向量→删行;**顺带清掉它的可见范围规则**;原件只在上传目录里才删。输出:结果。

    **为什么要清规则**:授权表按 ``document_id`` 存(没有外键,详见 ``ports/access.py``)。
    不清的话,将来重新上传一个同名文件会**继承上一份的授权** —— 旧文件曾经只给三个人看,
    新的同名文件也就只给那三个人看,而管理员以为自己传了一份公开文档。
    """
    from project_rag.application.documents import delete_document

    outcome = delete_document(
        _factory(deps, "document_store_factory"),
        _factory(deps, "vector_store_factory"),
        document_id,
        uploads_dir=_uploads_dir(),
        source_file=_source_file(deps, document_id),
    )
    store = _optional_factory(deps, "access_store_factory")
    if store is not None and outcome.removed_record:
        # 只在真的删掉了登记行时清规则:文档不存在时什么都不做(删除是幂等的,
        # 而"删一个不存在的 ID"顺手把它的规则清了,等于给别人一个间接改权限的入口)。
        removed = store.forget_document(document_id)
        if removed:
            _logger.info("删文档时清掉可见范围规则 %d 条:%s", removed, document_id)
    return outcome


def _metrics(deps: Deps, window: str, top: int) -> MetricsOut:
    """输入:依赖 + 窗口名 + 最慢榜条数。行为:读窗口内的账本行并聚合成报表。输出:MetricsOut。

    **口径不在这一层**:分位数、冷热分层、"``-`` 不参与统计" 全在 ``application/metrics.py``
    (那是能在没有 MySQL 的机器上单测的纯函数)。这一层只做两件事:把窗口名换成起始时刻、
    把 dataclass 搬进 pydantic 模型。

    **路径说明**:``/api/v1/admin/metrics`` —— 挪进 ``admin`` 并挂 ``require_admin``(P0-3b-5-②
    统一迁移时一起做的)。延迟数据是**运营视角**的东西:谁都能读,对外就是信息泄露
    (能看到流量高峰、最慢的请求、拒答率)。
    """
    from dataclasses import asdict

    from project_rag.application.metrics import DEFAULT_SEGMENTS, summarize

    store = _factory(deps, "ledger_store_factory")
    hours = _WINDOWS[window]
    # 窗口下界给**带时区的绝对时刻**:账本表里存的是 UTC,宿主(CST)与容器(UTC)
    # 上的 `datetime.now()` 不是同一串数字。
    rows = store.load_since(datetime.now(UTC) - timedelta(hours=hours))
    summary = summarize(rows, window=window, segments=DEFAULT_SEGMENTS, top_n=top)
    return MetricsOut(
        window=summary.window,
        rows=summary.rows,
        cold_rows=summary.cold_rows,
        warm_rows=summary.warm_rows,
        refused=summary.refused,
        truncated=summary.truncated,
        aborted=summary.aborted,
        faq_hits=summary.faq_hits,
        rag_answers=summary.rag_answers,
        segments=[SegmentOut(**asdict(seg)) for seg in summary.segments],
        slowest=[SlowRowOut(**asdict(item)) for item in summary.slowest],
    )


def _cost(deps: Deps, window: str) -> CostOut:
    """输入:依赖 + 窗口名。行为:读窗口内的账本行,按单价表折成钱。输出:CostOut。

    **口径不在这一层**:哪些行算"调了模型"、没配单价怎么办、金额怎么四舍五入,
    全在 ``application/cost.py``(那是能在没有 MySQL 的机器上单测的纯函数)。
    这一层只做两件事:把窗口名换成起始时刻、把 dataclass 搬进 pydantic 模型。

    **和延迟报表读的是同一张表、同一批行**:所以"这一窗有多少次模型调用"在两个报表里
    是同一个数 —— 两个报表各读各的口径,迟早会出现"延迟说 30 次、消耗说 42 次"。
    """
    from project_rag.application.cost import summarize_cost

    store = _factory(deps, "ledger_store_factory")
    hours = _WINDOWS[window]
    # 窗口下界给**带时区的绝对时刻**:账本表里存的是 UTC,宿主(CST)与容器(UTC)
    # 上的 `datetime.now()` 不是同一串数字。
    rows = store.load_since(datetime.now(UTC) - timedelta(hours=hours))
    report = summarize_cost(
        rows,
        prices=deps.cost_prices,
        currency=deps.cost_currency,
        window=window,
        # **时段规则必须传下去**:不传的话所有调用都按高峰价算(贵一倍)。
        rule=deps.cost_rule,
    )
    return CostOut(
        window=report.window,
        currency=report.currency,
        metered_calls=report.metered_calls,
        unmetered_calls=report.unmetered_calls,
        period_unknown_calls=report.period_unknown_calls,
        zone_unknown_calls=report.zone_unknown_calls,
        calendar_unknown_calls=report.calendar_unknown_calls,
        cache_unknown_calls=report.cache_unknown_calls,
        input_tokens=report.input_tokens,
        cached_input_tokens=report.cached_input_tokens,
        output_tokens=report.output_tokens,
        total_tokens=report.total_tokens,
        total_cost=report.total_cost,
        all_priced=report.all_priced,
        unpriced_models=list(report.unpriced_models),
        periods=[
            PeriodTotalOut(
                period=item.period,
                calls=item.calls,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                total_cost=item.total_cost,
            )
            for item in report.periods
        ],
        lines=[
            CostLineOut(
                model=line.model,
                period=line.period,
                calls=line.calls,
                input_tokens=line.input_tokens,
                cached_input_tokens=line.cached_input_tokens,
                output_tokens=line.output_tokens,
                total_cost=line.total_cost,
            )
            for line in report.lines
        ],
        peak_weekdays=sorted(deps.cost_rule.weekdays),
        peak_windows=[
            [start.strftime("%H:%M"), end.strftime("%H:%M")]
            for start, end in deps.cost_rule.windows
        ],
        holiday_years=sorted(deps.cost_rule.covered_years),
    )


# ── 账号、分组与文档可见范围(P0-6):都在线程池里跑,判定全在 application/ ──────
def _bad_request(exc: ValueError) -> HTTPException:
    """输入:用例层抛的 ``ValueError``。行为:翻成 400。输出:``HTTPException``。

    **用例层的校验消息是给人看的中文**(见 ``application/accounts.py``),所以原样带出去 ——
    重写一遍成"参数不合法"会把唯一有用的那句话丢掉。
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _account_out(user: User, group_ids: Sequence[str]) -> UserOut:
    """输入:用户 + 他所属的组。行为:转成出参(不含密码)。输出:``UserOut``。"""
    return UserOut(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        is_admin=user.is_admin,
        group_ids=list(group_ids),
    )


def _list_accounts(deps: Deps) -> UsersOut:
    """输入:依赖。行为:全部账号 + 各自的组。输出:``UsersOut``。

    组归属**一次全读**(``groups_by_user``)而不是逐个用户查:管理员页每次打开都要它,
    而"人 × 组"这张表很小(逐用户查是 N 次往返)。
    """
    users = _factory(deps, "auth_store_factory").list_users()
    groups: dict[str, tuple[str, ...]] = {}
    store = _optional_factory(deps, "access_store_factory")
    if store is not None:
        groups = dict(store.groups_by_user())
    return UsersOut(users=[_account_out(user, groups.get(user.user_id, ())) for user in users])


def _create_account(deps: Deps, request: CreateUserRequest) -> CreateUserOut:
    """输入:依赖 + 建号请求。行为:校验 → 算哈希 → 入库。输出:``CreateUserOut``。

    **哈希在这一层算**(而不是存储层):存储只存"已经算好的东西",那是 ``ports/auth.py``
    定下的分工 —— 换算法不动表结构,而且这条路径能在没有 MySQL 的机器上测。
    """
    from project_rag.application.security import hash_password

    store = _factory(deps, "auth_store_factory")
    users = list(store.list_users())
    try:
        name = check_new_account(
            users,
            username=request.username,
            password=request.password,
            role=request.role,
        )
        role = role_of(request.role)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    user = store.create_user(name, hash_password(request.password), role)
    _logger.info("管理员建号:%s(%s)", user.username, user.role)
    return CreateUserOut(user_id=user.user_id, username=user.username, role=user.role)


def _delete_account(deps: Deps, user_id: str, actor: User) -> DeletedUserOut:
    """输入:依赖 + 目标用户 ID + 动手的管理员。行为:三道守卫 → 删号 → 清他的组与授权。输出:``DeletedUserOut``。

    ``actor`` 是**从令牌换出来的那个人**,不是请求体里的字段 —— 让调用方自己报"我是谁",
    等于把"不能删自己"这条守卫交给调用方去诚实实现。
    """
    store = _factory(deps, "auth_store_factory")
    try:
        target = check_delete_account(
            list(store.list_users()), user_id=user_id, actor_id=actor.user_id
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    deleted = store.delete_user(target.user_id) > 0
    access = _optional_factory(deps, "access_store_factory")
    if access is not None and deleted:
        # 组员行与"以他为对象"的授权行一起清掉:留着已删用户的 ID,界面上会出现一个
        # 查不到、也删不掉的授权对象(见 ports/access.py 的 forget_user)。
        access.forget_user(target.user_id)
    _logger.info("管理员 %s 删掉账号 %s(%s)", actor.username, target.username, target.role)
    return DeletedUserOut(user_id=target.user_id, username=target.username, deleted=deleted)


def _reset_password(deps: Deps, user_id: str, password: str) -> UserOut:
    """输入:依赖 + 用户 ID + 新密码。行为:校验长度 → 算哈希 → 改。输出:那个账号。

    **改密码不吊销令牌**(与"删号即失效"不同):改密码通常发生在"本人忘了密码"这种场景,
    此时他手上没有有效令牌;而"令牌被盗、改密码想踢掉对方"是另一件事 ——
    那需要"吊销全部令牌"这个动作,记在 ``docs/deferred.md``。
    """
    from project_rag.application.security import hash_password

    store = _factory(deps, "auth_store_factory")
    target = next((user for user in store.list_users() if user.user_id == user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"没有这个账号({user_id!r})")
    try:
        check_new_password(password)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    store.set_password(target.user_id, hash_password(password))
    _logger.info("管理员改了 %s 的密码", target.username)
    access = _optional_factory(deps, "access_store_factory")
    groups = () if access is None else access.groups_by_user().get(target.user_id, ())
    return _account_out(target, groups)


def _group_out(group: Group, member_ids: Sequence[str]) -> GroupOut:
    """输入:组 + 成员 ID。行为:转成出参。输出:``GroupOut``。"""
    return GroupOut(group_id=group.group_id, name=group.name, member_ids=list(member_ids))


def _list_groups(deps: Deps) -> GroupsOut:
    """输入:依赖。行为:全部组 + 每组成员。输出:``GroupsOut``。"""
    store = _factory(deps, "access_store_factory")
    return GroupsOut(
        groups=[_group_out(group, store.members_of(group.group_id)) for group in store.list_groups()]
    )


def _create_group(deps: Deps, request: CreateGroupRequest) -> GroupOut:
    """输入:依赖 + 建组请求。行为:校验重名 → 建。输出:``GroupOut``(新组没有成员)。"""
    store = _factory(deps, "access_store_factory")
    try:
        name = check_group_name(list(store.list_groups()), name=request.name)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    group = store.create_group(name)
    _logger.info("建组:%s", group.name)
    return _group_out(group, ())


def _delete_group(deps: Deps, group_id: str) -> DeletedGroupOut:
    """输入:依赖 + 组 ID。行为:删组(连带组员与授权给它的那些行)。输出:``DeletedGroupOut``。

    幂等:组不存在时 ``deleted=false``,照样 200(与删文档、删账号同一条口径)。
    """
    store = _factory(deps, "access_store_factory")
    return DeletedGroupOut(group_id=group_id, deleted=store.delete_group(group_id) > 0)


def _set_members(deps: Deps, group_id: str, request: SetMembersRequest) -> GroupOut:
    """输入:依赖 + 组 ID + **整份**成员名单。行为:校验组与人存在 → 覆盖成员。输出:更新后的组。

    **人必须真的存在**:授权名单里混进一个不存在的用户 ID,界面上会显示一个查不到的名字,
    而管理员没法判断这份名单到底对不对(与 ``forget_user`` 要解决的是同一类问题)。
    """
    store = _factory(deps, "access_store_factory")
    group = next(
        (item for item in store.list_groups() if item.group_id == group_id), None
    )
    if group is None:
        raise HTTPException(status_code=404, detail=f"没有这个组({group_id!r})")
    known = {user.user_id for user in _factory(deps, "auth_store_factory").list_users()}
    unknown = [user_id for user_id in request.user_ids if user_id not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"这些账号不存在:{unknown}")
    store.set_members(group_id, request.user_ids)
    _logger.info("改组 %s 的成员:%d 人", group.name, len(set(request.user_ids)))
    return _group_out(group, store.members_of(group_id))


def _document_acl(deps: Deps, document_id: str) -> DocumentAclOut:
    """输入:依赖 + 文档 ID。行为:取这份文档的可见范围。输出:``DocumentAclOut``。

    **不查文档存不存在**:授权表可以比登记表"多"(文档被删时规则也会被删,
    但手工改库、或删到一半崩了都可能留下孤儿的规则行)。那时回一份空范围比 404 有用 ——
    管理员正要看的就是"为什么这份不见了"。
    """
    store = _factory(deps, "access_store_factory")
    acl = store.subjects_of(document_id)
    return _acl_out(acl)


def _acl_out(acl: DocumentAcl) -> DocumentAclOut:
    """输入:``DocumentAcl``。行为:转成出参(``restricted`` 是**算出来的**)。输出:``DocumentAclOut``。"""
    return DocumentAclOut(
        document_id=acl.document_id,
        restricted=acl.restricted,
        user_ids=list(acl.user_ids),
        group_ids=list(acl.group_ids),
    )


def _set_document_acl(
    deps: Deps, document_id: str, request: SetDocumentAclRequest
) -> DocumentAclOut:
    """输入:依赖 + 文档 ID + **整份**授权名单。行为:校验 ID 存在 → 覆盖规则。输出:更新后的范围。

    两份名单都空 = **取消限制**(回到人人可见),不是"谁都看不到" —— 见 ``domain/access.py``。
    校验 ID 存在,理由同 ``_set_members``:名单里出现不存在的对象时,没人能看出那份名单是错的。
    """
    store = _factory(deps, "access_store_factory")
    known_users = {user.user_id for user in _factory(deps, "auth_store_factory").list_users()}
    unknown_users = [user_id for user_id in request.user_ids if user_id not in known_users]
    if unknown_users:
        raise HTTPException(status_code=400, detail=f"这些账号不存在:{unknown_users}")
    known_groups = {group.group_id for group in store.list_groups()}
    unknown_groups = [group_id for group_id in request.group_ids if group_id not in known_groups]
    if unknown_groups:
        raise HTTPException(status_code=400, detail=f"这些组不存在:{unknown_groups}")
    store.set_document_subjects(
        document_id, user_ids=request.user_ids, group_ids=request.group_ids
    )
    _logger.info(
        "改可见范围 %s:%d 人 / %d 组",
        document_id,
        len(set(request.user_ids)),
        len(set(request.group_ids)),
    )
    return _acl_out(store.subjects_of(document_id))


def create_app(deps: Deps | None = None) -> FastAPI:
    """输入:依赖集合(不传就真实装配)。行为:建 FastAPI 应用并注册全部路由。输出:应用。

    传 ``deps`` = 不读 .env、不连任何东西,所以整条 HTTP/WS 链路可测(同 CLI 的注入方式)。
    """
    deps = deps if deps is not None else build_real_deps()
    settings = get_settings()
    # 账本落表:与 CLI 同一个入口(幂等;连不上 MySQL 只记 warning,服务照常起)。
    # 放在 create_app 里而不是 run() 里:**注入 deps 的测试路径也要落表**,
    # 否则"验收里跑出来的那些请求"不会进表,报表就只覆盖了一半流量。
    attach_ledger(deps)
    # 可见范围的三张表:建表是幂等的,失败只记一条告警(理由见 cli/deps.ensure_access_schema)
    access_problem = ensure_access_schema(deps)
    if access_problem:
        _logger.warning("可见范围未启用(建表失败):%s", access_problem)
    # 认证:这个签名就是"生产路径必然开着、只有注入式测试才是 None"的那一处
    auth = Authenticator(_optional_factory(deps, "auth_store_factory"))
    # 限流计数器**从组合根来**(Redis 或内存,见 cli/deps.py 的 `_rate_limiter`);
    # 没装配 = 不限流(与其它工厂同一条口径,`build_real_deps` 永远接上)。
    limiter = _optional_factory(deps, "rate_limiter_factory")

    app = FastAPI(
        title="project-rag",
        description="知识库问答问答:FAQ 优先,未命中转知识库(答案带引用)",
        version="0.1.0",
    )
    # 本机开发默认全开(api.cors_origins = ["*"]);要对外就得收紧到具体来源
    # (见 deferred.md §1:限流/鉴权)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        # 明确不带上凭证:**这个服务只用 Bearer 令牌,不用 cookie**。
        # 留这一行是为了把"通配来源 + 带凭证"这个组合钉死 —— 那是浏览器里
        # 唯一一种"任意站点都能带着你的身份打这个服务"的配置,而它是靠改一个
        # 布尔值就能打开的(将来真要用 cookie 会话时必须显式改这里,顺带被 review 看见)。
        allow_credentials=False,
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        """给前端产物加 CSP(与几条常规安全头)。**只管静态页,不碰 /api**。

        为什么只管静态页:接口返回的是 JSON,加 CSP 没有意义,却会让 `curl -i`
        的输出变脏、也让排障的人多一层要忽略的东西。
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/app"):
            response.headers.setdefault("Content-Security-Policy", _CSP)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response
    # html=True:请求一个目录时给它的 index.html —— 前端产物挂在 /static/app/ 下,
    # 没有这一条 `/static/app/` 会 404(StaticFiles 默认不做目录索引)。
    app.mount(
        "/static", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static"
    )

    @app.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        """进程探活。**刻意不探容器**:容器状态有 check_env_services.py 负责,这里只回答「服务在不在」。

        没有同步调用,所以不需要 ``_call`` —— 该是纯协程的地方就纯协程。
        """
        return HealthOut(status="ok")

    @app.get("/api/v1/sources", response_model=SourcesOut)
    async def sources(user: User | None = Depends(auth.user)) -> SourcesOut:
        """语料里真实有哪些来源(去重升序)。**要 Bearer**(与 `API.md` 的表一致)。

        读的是**向量库**而不是配置白名单:要回答的是「库里有哪几门课」,不是「配置里写了哪几个」。
        语料上千块,``load_all`` 一次可接受;上万块时再给端口加一个 ``sources()``(见 deferred)。

        (M7.4)补上的认证:它是**业务数据**(哪几门课、有哪些文档来源),
        而在这之前它和 `/capabilities` 一样裸着 —— `/capabilities` 公开是有理由的
        (不含任何数据,登录页要用它渲染上传提示),这一条没有。

        (P0-6)再加上"能不能用知识库"这道门:运维在这一条上拿 403 —— 来源标签是语料的目录结构。
        """
        auth.require_knowledge(user)
        return await _call(_sources, deps)

    @app.get("/api/v1/capabilities", response_model=CapabilitiesOut)
    async def capabilities() -> CapabilitiesOut:
        """后端支持哪些后缀、多大、会哪几种检索、有哪些模型 —— **前端不许写死这些**。

        上传框的 ``accept``、参数面板的选项与上限、模型下拉全从这里来。
        刻意不写死在前端:支持的后缀是装载器里那张表、检索选项是领域层的闭集,
        写两份迟早漂 —— 前端会放行一个后端根本不认的文件,或者把滑条拖到后端 422 的值。
        这个接口不需要登录(它不含任何数据)。
        """
        settings = get_settings()
        rag = settings.rag
        return CapabilitiesOut(
            suffixes=list(_factory(deps, "suffixes_factory")),
            max_upload_mb=settings.api.max_upload_mb,
            # 只有一个模型:下拉框是"预留",不是"能切"。label 与 id 相同 ——
            # 编一个好听的名字(如"DeepSeek 通用")会让日志里的模型名与界面对不上。
            models=[ModelOut(id=settings.llm.model, label=settings.llm.model, default=True)],
            model_settings=ModelSettingsOut(
                temperature=settings.llm.temperature, thinking=settings.llm.thinking
            ),
            retrieval=RetrievalOut(
                modes=list(RETRIEVAL_MODES),
                fusions=list(FUSION_MODES),
                defaults=RetrievalDefaultsOut(
                    mode="hybrid",  # 配置里没有 mode(它由权重表达),接口层的默认就是两路都跑
                    fusion=rag.fusion,
                    k=rag.retrieval_k,
                    max_context=rag.candidate_m,
                    bm25_weight=rag.bm25_weight,
                    vector_weight=rag.vector_weight,
                    rrf_k=rag.rrf_k,
                    rerank=rag.rerank,
                ),
                limits=RetrievalLimitsOut(
                    k_max=K_MAX, max_context_max=MAX_CONTEXT_MAX, rrf_k_max=RRF_K_MAX
                ),
                # 「这个部署能不能重排」= 有没有那个工厂,不是配置里写没写 true:
                # 配了 true 但没装重排器时,勾上它只会拿到 400。
                rerank_available=deps.reranker_factory is not None,
            ),
            # 报表能力:窗口白名单与分位数的样本量门槛**只在这里报一次**,
            # 前端因此不必写死一份"1h/6h/24h/7d/30d"(那种抄写迟早与后端漂开)。
            metrics=_metrics_capabilities(),
            roles=list(USER_ROLES),
        )

    @app.get("/api/v1/sessions", response_model=SessionsOut)
    async def sessions(
        limit: int = Query(default=50, ge=1, le=200),
        user: User | None = Depends(auth.user),
    ) -> SessionsOut:
        """**我名下的**会话目录(标题 = 首问裁到 40 字、轮数、最近活动)。

        三条口径:

        * **只列有轮次的会话** —— 前端是"首次提问时才建会话",刚建没问过的不该占地方;
        * **无认证模式给空表**(而不是列出所有人的会话):没有账号时,"这是谁的会话"
          这个问题没有答案,而"没有答案就都给"是最坏的一种实现(法律 33);
        * 认证开着、却连会话目录工厂都没注入(**显式注入才会这样**)→ 503,
          不静默给空表:空表会被读成"这个账号还没有会话",与"装配缺了一件事"完全是两回事。

        运维在这个接口上拿 403(``require_knowledge``):会话目录是"我读过什么"的索引,
        属于知识库那一侧的东西(见 ``domain/auth.py`` 的角色表)。
        """
        auth.require_knowledge(user)
        catalog = _optional_factory(deps, "session_catalog_factory")
        if catalog is None:
            if user is None:  # 无认证模式:没有归属可过滤
                return SessionsOut(sessions=[])
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="没有接上会话目录(session_catalog_factory 为空)",
            )
        if user is None:
            return SessionsOut(sessions=[])
        rows = await _call(catalog.list_for, user.user_id, limit=limit)
        return SessionsOut(
            sessions=[
                SessionSummaryOut(
                    session_id=row.session_id,
                    title=row.title,
                    rounds=row.rounds,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]
        )

    @app.post("/api/v1/documents", response_model=DocumentOut, status_code=202)
    async def upload_document(
        background: BackgroundTasks,
        request: Request,
        user: User | None = Depends(auth.user),
        filename: str = Query(..., description="原文件名(含后缀);文件本身走请求体"),
    ) -> DocumentOut:
        """上传一份文档并**异步**入库。需要管理员。返回 202 + 一条 ``pending`` 记录。

        **为什么必须异步**:539 页 PDF 在 CPU 上嵌入要 **8 分 12 秒** —— 同步 HTTP 必然超时,
        前端也只能转圈。所以:落盘 + 登记 → 立刻返回 → 后台任务入库 → 前端轮询清单看状态。

        **文件名一律只取 basename**:客户端给的名字可以是 ``../../etc/passwd``,
        直接当路径用就是路径穿越。重传同名文件即覆盖(``document_id`` 由 来源+文件名 决定)。

        调用姿势(前端):``fetch("/api/v1/documents?filename=" + encodeURIComponent(file.name),
        {method: "POST", headers: {Authorization: "Bearer " + token}, body: file})``
        """
        # 限流在鉴权**之前**:未认证的洪水(401 也是流量)同样要过闸,
        # 那时按来源 IP 记;认证过了就按账号记(同一个办公室不会互相挤占额度)。
        _enforce_rate_limit(
            limiter,
            "upload",
            _rate_limit_key(request, user),
            settings.api.rate_limit.upload,
            float(settings.api.rate_limit.window_s),
        )
        auth.require_admin(user)
        supported = _factory(deps, "suffixes_factory")
        raw_name = Path(filename).name  # ← 只取 basename:防路径穿越
        if not raw_name:
            raise HTTPException(status_code=400, detail="文件名不能为空")
        suffix = Path(raw_name).suffix.lower()
        if suffix not in supported:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的后缀 {suffix!r};支持 {'/'.join(supported)}",
            )

        target = _uploads_dir() / raw_name
        size = await _stream_to_file(
            request, target, settings.api.max_upload_mb * 1024 * 1024
        )

        from project_rag.application.documents import register_document

        record = await _call(
            register_document,
            _factory(deps, "document_store_factory"),
            # source 用**装载器会用的那个标签**(上传目录名):否则 /api/v1/sources 显示 "uploads"
            # 而登记表写 "upload",同一批文档两个来源名。
            source=_uploads_dir().name,
            filename=raw_name,
            size_bytes=size,
        )
        background.add_task(_run_ingest, deps, record, target)
        # 刚上传的这一份:pending / 文件刚落盘(大小已知)/ 库里当然还没有它的块。
        # ``state`` 就是 ``status`` —— 在途的行不做一致性判定(清单那边同一条口径)。
        return DocumentOut(
            document_id=record.document_id,
            source=record.source,
            filename=record.filename,
            status=record.status,
            state=record.status,
            chunks=record.chunks,
            size_bytes=record.size_bytes,
            disk_size_bytes=size,
            on_disk=True,
            created_at=record.created_at,
            error=record.error,
            searchable=False,
            indexed_chunks=0,
        )

    @app.get("/api/v1/documents", response_model=DocumentsOut)
    async def documents(user: User | None = Depends(auth.user)) -> DocumentsOut:
        """库里有哪些文档 —— **登记表 + 磁盘 + 向量库三处对账后的实时视图**。

        每一行带 ``state``:三处一致是 ``ok``,不一致是那四种之一(``changed`` /
        ``not_indexed`` / ``file_missing`` / ``unregistered``)—— 光念登记表会骗人:
        集合被清空了它还写"已入库 677 块",语料目录里新放的文件它根本不知道。
        磁盘上有、登记表没有的文件**也列出来**(``unregistered``,只读),不然
        "后端直接放进去的语料"在前端永远看不见。

        员工也能看清单(他要知道自己能检索什么);**但删除只有管理员能**。
        被收窄且没授权给他的文档**不在清单里**(P0-6),每行的 ``restricted`` 说明
        "这份是限定的、你在名单里"。
        """
        auth.require_knowledge(user)
        access = await _call(_access, deps, user)
        return await _call(_documents, deps, access)

    @app.delete("/api/v1/documents/{document_id}", response_model=DeletedDocumentOut)
    async def delete_document(
        document_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedDocumentOut:
        """删一份文档:**登记行 + 向量库里的子块 + 磁盘原件**一起清。需要管理员。

        **原件只在上传目录里才删**:语料目录(``data/corpus/**``)是源材料 —— 删库不等于删源,
        清掉块与登记行之后重新灌一遍就能回来;把源文件一起删了就真没了。
        所以回执里 ``removed_file``(真删了)与 ``kept_source``(故意没动源材料)是两件事。

        删除是**幂等**的:文档不存在时照样 200,只是 ``removed_*`` 都是 false ——
        重复点删除按钮不该报错(前端拿这两个字段决定提示语)。
        """
        auth.require_admin(user)
        outcome = await _call(_delete_document, deps, document_id)
        return DeletedDocumentOut(
            document_id=outcome.document_id,
            deleted_chunks=outcome.deleted_chunks,
            removed_file=outcome.removed_file,
            removed_record=outcome.removed_record,
            kept_source=outcome.kept_source,
        )

    @app.get("/api/v1/documents/{document_id}/chunks", response_model=DocumentPreviewOut)
    async def document_chunks(
        document_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        chars: int = Query(default=400, ge=50, le=2000),
        user: User | None = Depends(auth.user),
    ) -> DocumentPreviewOut:
        """一份文档入库后的样子:按**父块**折叠后的前 N 段(带页码)。

        **权限与清单一致(Bearer,不是 admin)**:员工本来就能看清单(P0-3b 的口径 ——
        他要知道自己能检索什么),预览是清单的展开,是同一份数据的另一种看法;
        真正的管理动作(上传 / 删除)才是 admin。

        ``limit`` 上限 100、``chars`` 上限 2000:预览是给人扫一眼的,
        不是导出接口 —— 要全文请走 CLI(`project-rag search` / 直接看原文件)。

        看不见的文档与不存在的文档**回同一句 404**(见 ``_document_preview``):
        "存在但不给你看"本身也是信息。
        """
        auth.require_knowledge(user)
        access = await _call(_access, deps, user)
        return await _call(_document_preview, deps, document_id, limit, chars, access)

    @app.get("/api/v1/reports/metrics", response_model=MetricsOut)
    async def metrics(
        window: str = "24h",
        top: int = Query(default=5, ge=1, le=TOP_MAX),
        user: User | None = Depends(auth.user),
    ) -> MetricsOut:
        """延迟报表:窗口内的 p50/p95/p99(热行)、冷热条数、拒答/截断/中断率、最慢几条。

        **谁能看:运维与管理员**(``require_reports``)。路径从 ``/admin/metrics`` 改到
        ``/reports/metrics`` 就是为了这件事 —— 运维不是管理员,让他去打一个
        ``/admin/`` 开头的接口,读代码的人会以为权限配错了。

        **没接账本表时给 503**(与其它工厂一致):报表读不到数据 ≠ 报表是空的 ——
        分不清这两件事,就会把"MySQL 挂了"读成"系统没有流量"。
        """
        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window 只支持 {'/'.join(_WINDOWS)}")
        auth.require_reports(user)
        return await _call(_metrics, deps, window, top)

    @app.get("/api/v1/reports/cost", response_model=CostOut)
    async def cost(
        window: str = "24h", user: User | None = Depends(auth.user)
    ) -> CostOut:
        """消耗报表:窗口内调了几次模型、烧了多少 token、折合多少钱。

        **与延迟报表同权限、同窗口白名单、读同一批账本行** —— 两份报表的数字必须能对上
        (``window`` 的白名单只写了一份,见 ``_WINDOWS``)。

        **钱算不出来时不会编**:没配单价的模型单独列在 ``unpriced_models``、
        provider 没回报用量的调用数在 ``unmetered_calls``。前端据此显示
        "这只是已知的部分" —— 一个看起来完整的错误金额比一句"不知道"糟得多。
        """
        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window 只支持 {'/'.join(_WINDOWS)}")
        auth.require_reports(user)
        return await _call(_cost, deps, window)

    # ── 账号与可见范围(P0-6,管理员专用) ─────────────────────────────────────
    @app.get("/api/v1/admin/users", response_model=UsersOut)
    async def admin_users(user: User | None = Depends(auth.user)) -> UsersOut:
        """全部账号(用户名 / 角色 / 所属组)。**不含密码或它的哈希**。

        一次把组归属也带上:管理员页要显示"这个人属于哪几类",而那是两次查询拼出来的东西 ——
        分两个接口的话,前端会在两次刷新之间看到自相矛盾的画面(人已经进组了,列表里还没显示)。
        """
        auth.require_admin(user)
        return await _call(_list_accounts, deps)

    @app.post("/api/v1/admin/users", response_model=CreateUserOut, status_code=201)
    async def admin_create_user(
        request: CreateUserRequest, user: User | None = Depends(auth.user)
    ) -> CreateUserOut:
        """建一个账号(员工 / 运维 / 管理员)。**201 + 不回显密码**。

        校验在 ``application/accounts.py``(登录名长度与重名、密码长度、角色闭集),
        这里只把 ``ValueError`` 翻成 400 —— 那些消息是给人看的中文。
        """
        auth.require_admin(user)
        return await _call(_create_account, deps, request)

    @app.delete("/api/v1/admin/users/{user_id}", response_model=DeletedUserOut)
    async def admin_delete_user(
        user_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedUserOut:
        """删一个账号。**不能删自己,也不能删最后一个管理员**(见 ``application/accounts.py``)。

        删号之后:他的令牌换不出用户(令牌行也一并删了)、他名下的会话变成无主
        (``owner_of`` 仍返回那个已不存在的 ID → 谁都读不到,失败方向是关闭的)。
        这两条都有契约钉着(``check_p0_6_access.py``)。
        """
        auth.require_admin(user)
        actor = user
        if actor is None:  # 无认证模式:没有"动手的人",删号这条守卫就无从谈起
            raise HTTPException(status_code=503, detail="没有接上认证存储")
        return await _call(_delete_account, deps, user_id, actor)

    @app.post("/api/v1/admin/users/{user_id}/password", response_model=UserOut)
    async def admin_reset_password(
        user_id: str, request: ResetPasswordRequest, user: User | None = Depends(auth.user)
    ) -> UserOut:
        """替某个账号改密码(管理员在"本人忘了密码"时用)。

        改密码**不吊销他手上的令牌**:这条路径的场景是"他没登录进来",不是"要踢掉谁"。
        """
        auth.require_admin(user)
        return await _call(_reset_password, deps, user_id, request.password)

    @app.get("/api/v1/admin/groups", response_model=GroupsOut)
    async def admin_groups(user: User | None = Depends(auth.user)) -> GroupsOut:
        """全部组(一类员工)与各自的成员。授权那份文档给"哪一类员工"时选的就是它们。"""
        auth.require_admin(user)
        return await _call(_list_groups, deps)

    @app.post("/api/v1/admin/groups", response_model=GroupOut, status_code=201)
    async def admin_create_group(
        request: CreateGroupRequest, user: User | None = Depends(auth.user)
    ) -> GroupOut:
        """建一个组。重名报错(不覆盖)—— 组名是管理员唯一认得的标识。"""
        auth.require_admin(user)
        return await _call(_create_group, deps, request)

    @app.delete("/api/v1/admin/groups/{group_id}", response_model=DeletedGroupOut)
    async def admin_delete_group(
        group_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedGroupOut:
        """删一个组。**连带清掉它的成员与授权给它的那些规则行** —— 留着会串到同名的下一个组。"""
        auth.require_admin(user)
        return await _call(_delete_group, deps, group_id)

    @app.put("/api/v1/admin/groups/{group_id}/members", response_model=GroupOut)
    async def admin_set_members(
        group_id: str, request: SetMembersRequest, user: User | None = Depends(auth.user)
    ) -> GroupOut:
        """**整份**设置成员名单(声明式):勾完点保存,语义就是"这些人"。"""
        auth.require_admin(user)
        return await _call(_set_members, deps, group_id, request)

    @app.get("/api/v1/admin/documents/{document_id}/acl", response_model=DocumentAclOut)
    async def admin_document_acl(
        document_id: str, user: User | None = Depends(auth.user)
    ) -> DocumentAclOut:
        """一份文档的可见范围(授权给了哪些人、哪些组)。

        ``restricted=false`` = 没有规则 = 人人可见(这是默认状态,也是"取消限制"的结果)。
        """
        auth.require_admin(user)
        return await _call(_document_acl, deps, document_id)

    @app.put("/api/v1/admin/documents/{document_id}/acl", response_model=DocumentAclOut)
    async def admin_set_document_acl(
        document_id: str,
        request: SetDocumentAclRequest,
        user: User | None = Depends(auth.user),
    ) -> DocumentAclOut:
        """设置一份文档的可见范围(**整份覆盖**)。两个名单都空 = 取消限制。

        改完之后**下一条请求就生效**(检索、清单、预览三处都读同一份规则),
        不需要重启、也不需要重新入库 —— 规则不在向量库里,它是每次请求现查的。
        """
        auth.require_admin(user)
        return await _call(_set_document_acl, deps, document_id, request)

    @app.post("/api/v1/auth/login", response_model=LoginOut)
    async def login(request: LoginRequest, http_request: Request) -> LoginOut:
        """用账号密码换一个 Bearer 令牌。失败一律 401 + 同一句话(防用户名枚举)。

        **路径约定**:业务接口一律在 ``/api/v1`` 下 —— 老接口已经在
        P0-3b-5-② 那一步全部迁完(见 ``docs/STATUS.md`` §7),**没有留别名**:
        两套路径就是两个真值来源,契约要写两遍,而前端只需要一个前缀。
        """
        caller = http_request.client.host if http_request.client else "unknown"
        _enforce_rate_limit(
            limiter,
            "login",
            f"ip:{caller}",
            settings.api.rate_limit.login,
            float(settings.api.rate_limit.window_s),
        )
        token, user = await auth.login(request.username, request.password)
        # 登录成功 → 清掉这个 IP 的计数:否则"输错几次、随后输对"的用户会在接下来
        # 一分钟里被自己刚才的失败次数牵连(而那正是 forget 存在的理由)。
        if limiter is not None:
            limiter.forget(f"login:ip:{caller}")
        return LoginOut(
            token=token,
            username=user.username,
            role=user.role,
            expires_in=TOKEN_TTL_SECONDS,
        )

    @app.get("/api/v1/auth/me", response_model=MeOut)
    async def me(user: User | None = Depends(auth.user)) -> MeOut:
        """当前账号(给前端决定显不显示上传/删除/报表)。**必须带令牌**,否则 401。

        三个能力布尔值都从这里出(见 ``schemas.MeOut``):前端不比较角色字符串,
        它按"能不能"渲染页签 —— 于是加第四个角色时前端一行都不用改。
        """
        if user is None:
            raise HTTPException(status_code=503, detail="没有接上认证存储")
        return MeOut(
            username=user.username,
            role=user.role,
            is_admin=user.is_admin,
            can_view_reports=user.can_view_reports,
            can_use_knowledge=user.can_use_knowledge,
            can_manage=user.can_manage,
        )

    @app.post("/api/v1/auth/logout", response_model=LogoutOut)
    async def logout(authorization: str | None = Header(default=None)) -> LogoutOut:
        """退出登录:**让服务端那枚令牌立刻失效**(不只是前端删掉本地副本)。

        幂等:没带令牌、或令牌本来就无效,照样 200 + ``revoked=false`` ——
        重复点退出不该报错(与删文档同一条口径)。
        """
        return LogoutOut(revoked=await auth.logout(authorization))

    @app.post("/api/v1/create_session", response_model=SessionOut)
    async def create_session(user: User | None = Depends(auth.user)) -> SessionOut:
        """发一个新会话 ID,并**当场记下归属** —— 归属必须在拿到 ID 的那一刻就绑定,
        否则"先建会话再补绑"之间有一段谁都能读它的窗口。

        纯计算 + 一次插入(插入进线程池)。
        """
        auth.require_knowledge(user)
        session_id = str(uuid.uuid4())
        await auth.bind_session(session_id, user)
        return SessionOut(session_id=session_id)

    @app.get("/api/v1/history/{session_id}", response_model=HistoryOut)
    async def history(session_id: str, user: User | None = Depends(auth.user)) -> HistoryOut:
        """最近 N 轮历史 —— 与喂给模型的**同一份**(所以前端看到的和模型看到的不会不一致)。

        **先查归属再读**:这一条正是 P0-3a 要堵的洞 —— 会话 ID 以前是裸字符串,猜到就能读。
        """
        auth.require_knowledge(user)
        await auth.check_owner(session_id, user)
        return await _call(_history, deps, session_id)

    @app.delete("/api/v1/history/{session_id}", response_model=DeletedOut)
    async def clear_history(
        session_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedOut:
        """清空一段会话,返回删掉几轮。**同样是别人删不掉的**(先查归属)。"""
        auth.require_knowledge(user)
        await auth.check_owner(session_id, user)
        return await _call(_delete_history, deps, session_id)

    @app.post("/api/v1/query", response_model=QueryResponse)
    async def query(
        request: QueryRequest,
        http_request: Request,
        response: Response,
        user: User | None = Depends(auth.user),
    ) -> QueryResponse:
        """非流式问答:FAQ 命中直答,否则走知识库(拒答也是一句人话,不会是 null)。

        问候匹配是纯计算(一次正则),留在循环里;剩下的整条链路一次 ``_call`` 搬走。

        **带 session_id 时先查归属**:否则带上别人的会话 ID 就能拿他的历史继续提问 ——
        那和直接读历史一样是越权(所以这条检查放在问候分支**之前**)。

        **超限回的是兜底话术,不是 429**（作者定):问答这条路上,
        "被限流"对用户就是一句"稍后再试",把它做成错误码等于让界面上多一条红字;
        真正需要 429 的是登录与上传(见 ``_enforce_rate_limit`` 的 docstring)。
        响应仍带 ``Retry-After`` 头,写脚本的人照样能按它退避。
        """
        # 权限在限流之前:运维连问都不该能问(403),而"先扣一次限额再拒绝"会把
        # 一个纯粹的权限问题伪装成"你今天问得太多了"。
        auth.require_knowledge(user)
        retry_after = _rate_limit_check(
            limiter,
            "query",
            # 注意用 **http_request** 而不是 request:那个名字被请求体占了
            # (第一版就传错了,AttributeError: 'QueryRequest' object has no attribute 'client')
            _rate_limit_key(http_request, user),
            settings.api.rate_limit.query,
            float(settings.api.rate_limit.window_s),
        )
        if retry_after > 0:
            key = _rate_limit_key(http_request, user)
            _logger.warning("限流拦截(未进入用例,账本里没有这一条):档位=query 键=%s 等待=%ss", key, retry_after)
            response.headers["Retry-After"] = str(retry_after)
            return QueryResponse(
                answer=_throttle_text(
                    "query",
                    settings.api.rate_limit.query,
                    retry_after,
                    float(settings.api.rate_limit.window_s),
                ),
                channel="throttled",
                session_id=request.session_id,
            )
        await auth.check_owner(request.session_id, user)
        greeting = match_greeting(request.query)
        if greeting is not None:
            return _greeting_response(greeting, request.session_id)
        access = await _call(_access, deps, user)
        return await _call(_answer, deps, request, access)

    @app.websocket("/api/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        """流式问答:客户端发一条 JSON(同 QueryRequest),服务端逐帧推 ``sources``/``token``/``done``。

        三条容易踩的:
        1. 问候命中**不关连接** —— 发完话术继续等下一句(直接 ``break`` 会把整条连接关掉);
        2. 同步的帧流在**线程池**里取(见 ``_in_thread``),不卡事件循环;
        3. 请求体畸形时回一帧 ``type="error"``,连接留着 —— 前端能看到「你发的不是 JSON」。
        """
        await websocket.accept()
        # 认证:P0-3a 只堵了 HTTP,WS 这条路当时是**已知未闭合**的缺口。
        # 浏览器不能给 WS 设自定义头,所以令牌只能走 query 参数(`?token=…`)——
        # 代价是它会进服务器日志,这一点写进 api/auth.py 的 docstring 了。
        ws_token = websocket.query_params.get("token")
        ws_user = await auth.user_for_ws_token(ws_token)
        if auth.enabled and ws_user is None:
            # 先回一帧再关:光 close 的话前端只看到"连接被拒",不知道是令牌过期还是服务挂了
            await websocket.send_json(
                {"type": "error", "detail": "未认证:请在 URL 上带 ?token=<登录拿到的令牌>"}
            )
            await websocket.close(code=1008)  # 1008 = policy violation
            return
        if ws_user is not None and not ws_user.can_use_knowledge:
            # 与 HTTP 的 **同一条口径**(``require_knowledge``):运维只能看报表。
            # 这里连上都不让连 —— 令牌有效但角色不对是"这条路不对",
            # 不是"这一句话不对",回 error 帧留着连接只会让他反复撞同一面墙。
            await websocket.send_json(
                {"type": "error", "detail": "这个操作需要员工或管理员角色(运维只能看报表)"}
            )
            await websocket.close(code=1008)
            return
        # 可见范围:一次算好,循环里每次提问复用 —— 规则在连接期间变了也不会读到半新半旧的
        # 两个状态(要立刻生效就重连;这一点写在 API.md 里)。
        ws_access = await _call(_access, deps, ws_user)
        try:
            while True:
                try:
                    payload = await websocket.receive_json()
                except json.JSONDecodeError as exc:
                    # 连「不是 JSON」也要回一帧 error 并留着连接:下面那个 try 只兜得住
                    # 「JSON 合法但字段不合法」。这里漏掉的话异常会抛到外层 except,
                    # 连接跟着断 —— 前端得重连才能再问(与本文档上面第 3 条的承诺不符)。
                    await websocket.send_json(
                        {"type": "error", "detail": f"请求体不是合法 JSON:{exc}"}
                    )
                    continue
                try:
                    request = QueryRequest.model_validate(payload)
                except ValidationError as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "detail": f"请求体不合法:{exc.error_count()} 处",
                        }
                    )
                    continue

                # 归属检查:**回一帧 error 但不断连接** —— 与请求体畸形同一种处理
                # (断连接会让前端以为服务挂了,而其实只是这一句话越权)
                violation = await auth.owner_violation(request.session_id, ws_user)
                if violation is not None:
                    await websocket.send_json({"type": "error", "detail": violation})
                    continue

                # 限流:与 HTTP 的 /query **共用同一档**,回的也是**同一句兜底话术**
                # (token + done,channel="throttled")—— 与问候那条前置拦截同一个形制:
                # 不是错误、不关连接,用户看到的就是一句话"稍后再试"。
                retry_after = _rate_limit_check(
                    limiter,
                    "query",
                    _rate_limit_key(websocket, ws_user),
                    settings.api.rate_limit.query,
                    float(settings.api.rate_limit.window_s),
                )
                if retry_after > 0:
                    notice = _throttle_text(
                        "query",
                        settings.api.rate_limit.query,
                        retry_after,
                        float(settings.api.rate_limit.window_s),
                    )
                    _logger.warning(
                        "限流拦截(WS,未进入用例):键=%s 等待=%ss",
                        _rate_limit_key(websocket, ws_user),
                        retry_after,
                    )
                    await websocket.send_json({"type": "token", "text": notice})
                    await websocket.send_json(
                        {
                            "type": "done",
                            "answer": notice,
                            "channel": "throttled",
                            "refused": False,
                            "truncated": False,
                            "citations": [],
                            "session_id": request.session_id,
                        }
                    )
                    continue

                greeting = match_greeting(request.query)
                if greeting is not None:
                    await websocket.send_json({"type": "token", "text": greeting})
                    await websocket.send_json(
                        {
                            "type": "done",
                            "answer": greeting,
                            "channel": "greeting",
                            "refused": False,
                            "truncated": False,
                            "citations": [],
                            "session_id": request.session_id,
                        }
                    )
                    continue

                events = await _call(_stream_events, deps, request, ws_access)
                async for event in _in_thread(events):
                    await websocket.send_json(_frame(event, request.session_id))
        except WebSocketDisconnect:
            _logger.debug("客户端断开 WebSocket")
        except Exception as exc:  # noqa: BLE001 - 连接层的兜底:短消息回前端,细节进日志
            _logger.error("WebSocket 处理失败:%s", exc)
            _logger.debug("WebSocket 处理失败详情:", exc_info=True)
            try:
                await websocket.send_json({"type": "error", "detail": str(exc)})
            except Exception:  # noqa: BLE001 - 连接可能已经没了
                pass

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """根路径跳转到**前端 app**(M7.1 起)。

        为什么是重定向而不是直接返回 SPA 的 index.html:那份产物用**相对** base
        (`./assets/…`),只有 URL 以 `/static/app/` 结尾时相对路径才解析得对;
        直接返回文件会让浏览器去 `/assets/…` 拿资源 —— 404,而且页面白屏。
        重定向的另一个好处是 URL 变对了,用户刷新/收藏都对。

        这一份产物是**唯一的界面**:删掉了原来那个 101 行的内置调试页。
        两份 UI = 两套要跟着后端一起改的东西,而其中一份没人日常打开 —— 它漂了也不会
        有人发现(旧调试页那次就比新前端晚了半天才跟上 `/api/v1` 迁移)。排障也不缺入口:
        接口文档 `/docs`、能力自述 `/api/v1/capabilities`、`curl` 直接打 `/api/v1/query`
        能拿到一份不流式的答案;界面本身则由 `check_m7_*` 那几套契约与浏览器用例盯着。
        """
        return RedirectResponse(url="/static/app/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return app


# 预热用的问题**走配置**(api.warmup_query):挑一个已知能从真语料里召回的
# (默认「装饰器有什么用」实测召回 2 条),这样关键字那一路、融合、重排都走得到。
# 它不会调大模型(见 _warmup_once)。**换语料领域时记得换这一句** ——
# 检索不到任何块的预热等于没预热。
def _warmup_once(deps: Deps) -> None:
    """输入:依赖集合。行为:把冷启动那几笔账先付掉。输出:无(失败往上抛,由 ``_warmup`` 兜)。

    两件事:

    1. **FAQ 侧**:读一次真表 + 用真匹配器匹配一次 —— 冷建 FAQ 的 BM25 索引(实测 ~300ms);
    2. **知识库侧**:取到第一帧就收工。这一步会构造嵌入/重排/向量库/关键字索引四个单例
       (不预热的话,那是**每个请求** 4.4s 的开销),并让 BM25 的语料索引与 Milvus 连接就位。

    **为什么只取第一帧**:第一帧一定出现在调大模型**之前** —— 有引用是 ``sources`` 帧,
    没引用是拒答话术帧。所以预热把重活全干了,却一分 token 钱都不花。
    """
    settings = get_settings()
    warmup_query = settings.api.warmup_query
    entries = _factory(deps, "faq_store_factory").load_all()
    if entries:
        _factory(deps, "matcher_factory").best_match(warmup_query, entries)

    knowledge = knowledge_callable(
        deps,
        warmup_query,
        # 预热不看任何人的可见范围(UNRESTRICTED):它只走到"检索出引用"为止,
        # 不返回内容、也不落任何账 —— 换句话说,预热用的是**最宽的那个视角**,
        # 而它看到的东西一个字都不会给用户(下一条真实请求会按那个人的范围重查)。
        _options(deps, QueryRequest(query=warmup_query), UNRESTRICTED),
        rerank=settings.rag.rerank,
    )
    events = knowledge(())
    next(events, None)  # 第一帧:有引用是 sources,没引用是拒答话术 —— 都在调 LLM 之前
    close = getattr(events, "close", None)
    if callable(close):  # 收工:再迭代下去就该去调 provider 了
        close()


def _warmup(deps: Deps) -> None:
    """输入:依赖集合。行为:预热一次,失败只记日志。输出:无。

    **失败绝不拦启动**:预热是优化,不是正确性 —— 模型没下全 / Milvus 没起来 / 配置写错,
    都该让服务照常起来(真请求里照样会报错,而且那时有请求上下文,更好查)。
    """
    started = time.perf_counter()
    try:
        _warmup_once(deps)
    except Exception as exc:  # noqa: BLE001 - 预热失败不许拖垮启动
        _logger.warning("启动预热失败(不影响启动,首个请求会自己补做):%s", exc)
        _logger.debug("启动预热失败详情:", exc_info=True)
        return
    _logger.info(
        "启动预热完成(%.0f ms):模型 / 语料索引 / 连接都已就位",
        (time.perf_counter() - started) * 1000,
    )


def run() -> None:
    """控制台入口(``project-rag-api``)。输入:无。行为:装日志 →(可选)预热 → 起 uvicorn。输出:无(进程常驻)。

    **刻意不在模块级建 ``app``**:那样「import 这个模块」就会读 .env、建配置 —— 测试要的是
    一个能注入假依赖的工厂(``create_app``),不是一份 import 期副作用。

    **日志与预热都装在这里,不装进 ``create_app()``**:起服务是进程入口,这些是入口的职责;
    工厂要保持纯粹(测试注入假依赖时,不该顺手改全局日志、更不该去加载真模型)。
    漏掉日志那一步的后果实测过:``project-rag-api`` 起的进程里除了 uvicorn 的访问日志,
    **业务日志一条都留不下**。预热见 ``_warmup``(``RAG_API__WARMUP=false`` 可关)。

    **绑 ``0.0.0.0`` 而不是回环**:WSL 里绑 127.0.0.1 时 Windows 浏览器连不上(Windows 侧的
    localhost 转发没给这个端口建入口 —— 实测),进容器后绑回环也映射不出去。

    uvicorn 只起一个 worker:嵌入/重排模型是进程内单例,多 worker 会各加载一份、内存翻倍。
    """
    import uvicorn

    from project_rag.observability import configure_logging

    configure_logging()
    settings = get_settings()
    # CORS 的部署警戒线**在进程入口说**(不是 create_app):验收脚本会建几十个 app,
    # 在那里打 WARNING 只会变成噪声,而"日志里天天有的一句话"等于没有。
    _warn_wide_cors(settings)
    # 依赖只装配一次:预热与 create_app 用同一份,免得建出两套单例缓存
    deps = build_real_deps()
    if settings.api.warmup:
        _warmup(deps)
    # 地址走配置:类默认仍是 0.0.0.0:8080
    # (绑回环时 WSL 里 Windows 浏览器连不上,进容器后也映射不出去)
    uvicorn.run(create_app(deps), host=settings.api.host, port=settings.api.port)

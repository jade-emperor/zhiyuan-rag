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
4. **业务接口一律在 ``/api/v1`` 下** —— 2026-10-06 从 ``/api/*`` 统一迁过来(P0-3b-5-②),
   **没有留别名**:两套路径就是两个真值来源,契约要写两遍,而前端只要一个前缀。
   留在根上的只有 ``/health``(探活)、``/``(跳前端 app)与 ``/static``(静态资源:
   前端产物在 ``/static/app/``、内置调试页在 ``/static/index.html``)—— 它们不是业务接口,
   也不该跟着 API 版本走。这条有契约钉着(``check_m5_api`` 的「命名空间」那条):漏一个
   老式路径进去,前端的 TS 客户端就会生成两个前缀。

装配复用 ``cli/deps.py`` 那一套(``Deps`` / ``build_real_deps`` / ``knowledge_callable``):
**组合根只该有一套**,api 再写一份装配,两份迟早不一致。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from project_rag.api.auth import Authenticator
from project_rag.api.schemas import (
    K_MAX,
    MAX_CONTEXT_MAX,
    RRF_K_MAX,
    CapabilitiesOut,
    CitationOut,
    DeletedDocumentOut,
    DeletedOut,
    DocumentOut,
    DocumentsOut,
    HealthOut,
    HistoryOut,
    HistoryTurnOut,
    LoginOut,
    LoginRequest,
    MeOut,
    MetricsOut,
    ModelOut,
    ModelSettingsOut,
    QueryRequest,
    QueryResponse,
    RetrievalDefaultsOut,
    RetrievalLimitsOut,
    RetrievalOut,
    SegmentOut,
    SessionOut,
    SessionSummaryOut,
    SessionsOut,
    SlowRowOut,
    SourcesOut,
)
from project_rag.application import Session, answer_question, stream_question
from project_rag.application.security import TOKEN_TTL_SECONDS
from project_rag.application.greeting import match_greeting
from project_rag.cli.deps import Deps, attach_ledger, build_real_deps, knowledge_callable
from project_rag.config import PROJECT_ROOT, get_settings
from project_rag.domain import (
    FUSION_MODES,
    RETRIEVAL_MODES,
    Citation,
    QaAnswer,
    QaEvent,
    RetrievalMode,
    RetrievalOptions,
)
from project_rag.domain.auth import USER_ROLES, User
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
    return Session(
        store=_factory(deps, "conversation_store_factory"),
        session_id=session_id,
        rounds=get_settings().conversation.history_rounds,
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


def _options(deps: Deps, request: QueryRequest) -> RetrievalOptions:
    """输入:依赖 + 请求。行为:配置里的旋钮与请求里的覆盖拼成一次检索的参数。输出:RetrievalOptions。

    口径三条:
    1. **请求里给了就用请求的,没给就用 yaml 的** —— 默认值只有一处真值来源;
    2. ``mode`` 翻译成权重(见 ``_weights``);
    3. ``rerank`` **不进 options** —— 它由"传不传重排器"表达(见 ``_rerank_enabled``
       与 ``cli/commands/rag.py``,两处同一个口径)。
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


def _answer(deps: Deps, request: QueryRequest) -> QueryResponse:
    """输入:依赖 + 请求。行为:同步地把一次问答跑完(装端口 → 用例 → 出参)。输出:QueryResponse。

    **一次请求只跳一次线程**:工厂可能造重物、用例要做检索与生成,把它们全放进这一个
    同步函数里让 ``_call`` 一次搬完,比「每个端口跳一次」好推理,也不会漏掉某个调用点。
    """
    result = answer_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps, request.query, _options(deps, request), rerank=_rerank_enabled(deps, request)
        ),
        faq_threshold=deps.faq_threshold,
        session=_session(deps, request.session_id),
        thinking=deps.thinking,  # 只进延迟账,不影响答案
    )
    return _to_response(result, request.session_id)


def _stream_events(deps: Deps, request: QueryRequest) -> Iterator[QaEvent]:
    """输入:依赖 + 请求。行为:同步地把流式用例的帧流**搭起来**(装端口)。输出:同步帧迭代器。

    重活(嵌入/检索/生成)发生在迭代它的时候,那时已经在 ``_in_thread`` 的池子里了;
    但「搭起来」这一步要调工厂(可能加载模型),所以它本身也得在 ``_call`` 里执行。
    """
    return stream_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps, request.query, _options(deps, request), rerank=_rerank_enabled(deps, request)
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
    """输入:请求、落盘路径、上限字节数。行为:**边收边写边数**,超限就删掉半截文件并报 413。输出:字节数。

    **为什么不用 multipart**:FastAPI 的 ``UploadFile`` 需要 ``python-multipart`` 这个额外依赖,
    而浏览器的 ``fetch(url, {method: "POST", body: file})`` **原生就把文件当请求体发** ——
    不需要 FormData、不需要新依赖、前端还少一层编码。代价是文件名得走 query 参数
    (所以下面那句"只取 basename"更要紧了)。

    **为什么边收边写**:一次性 ``await request.body()`` 等于让一个 2G 的文件先把内存吃光
    再判断超限。分块收 + 立刻落盘,超限时删掉半截文件,不留垃圾。
    """
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as sink:
            async for block in request.stream():
                written += len(block)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {max_bytes // (1024 * 1024)}MB 上限",
                    )
                sink.write(block)
    except HTTPException:
        target.unlink(missing_ok=True)
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
            )
        except Exception as exc:  # noqa: BLE001 - 状态已记 failed,这里只留日志
            _logger.error("后台入库失败 %s:%s", record.filename, exc)


def _uploads_dir() -> Path:
    """输入:无。行为:上传目录的绝对路径(相对路径按**项目根**解析,与 models/data 同一套)。输出:路径。"""
    raw = Path(get_settings().api.uploads_dir)
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def _documents(deps: Deps) -> DocumentsOut:
    """输入:依赖。行为:读登记表(新→旧)。输出:清单。"""
    from project_rag.application.documents import list_documents

    records = list_documents(_factory(deps, "document_store_factory"))
    return DocumentsOut(
        documents=[
            DocumentOut(
                document_id=r.document_id,
                source=r.source,
                filename=r.filename,
                status=r.status,
                chunks=r.chunks,
                size_bytes=r.size_bytes,
                created_at=r.created_at,
                error=r.error,
                searchable=r.searchable,
            )
            for r in records
        ],
        uploads_dir=str(_uploads_dir()),
    )


def _delete_document(deps: Deps, document_id: str):
    """输入:依赖 + 文档 ID。行为:标记→删向量→删文件→删行(见 application/documents.py)。输出:结果。"""
    from project_rag.application.documents import delete_document

    return delete_document(
        _factory(deps, "document_store_factory"),
        _factory(deps, "vector_store_factory"),
        document_id,
        uploads_dir=_uploads_dir(),
    )


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
    rows = store.load_since(datetime.now() - timedelta(hours=hours))
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
    # 认证:这个签名就是"生产路径必然开着、只有注入式测试才是 None"的那一处
    auth = Authenticator(_optional_factory(deps, "auth_store_factory"))

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
    )
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
    async def sources() -> SourcesOut:
        """语料里真实有哪些来源(去重升序)。

        读的是**向量库**而不是配置白名单:要回答的是「库里有哪几门课」,不是「配置里写了哪几个」。
        语料上千块,``load_all`` 一次可接受;上万块时再给端口加一个 ``sources()``(见 deferred)。
        """
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
        """
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
        auth.require_admin(user)
        settings = get_settings()
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
        return DocumentOut(
            document_id=record.document_id,
            source=record.source,
            filename=record.filename,
            status=record.status,
            chunks=record.chunks,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            error=record.error,
            searchable=record.searchable,
        )

    @app.get("/api/v1/documents", response_model=DocumentsOut)
    async def documents(user: User | None = Depends(auth.user)) -> DocumentsOut:
        """库里有哪些文档(文件名 / 块数 / 状态 / 上传时间)—— 前端卡片要的东西。

        员工也能看清单(他要知道自己能检索什么);**但删除只有管理员能**。
        """
        return await _call(_documents, deps)

    @app.delete("/api/v1/documents/{document_id}", response_model=DeletedDocumentOut)
    async def delete_document(
        document_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedDocumentOut:
        """删一份文档:**登记行 + 向量库里的子块 + 磁盘原文件**三处一起清。需要管理员。

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
        )

    @app.get("/api/v1/admin/metrics", response_model=MetricsOut)
    async def metrics(
        window: str = "24h", top: int = 5, user: User | None = Depends(auth.user)
    ) -> MetricsOut:
        """延迟报表:窗口内的 p50/p95/p99(热行)、冷热条数、拒答/截断/中断率、最慢几条。

        **没接账本表时给 503**(与其它工厂一致):报表读不到数据 ≠ 报表是空的 ——
        分不清这两件事,就会把"MySQL 挂了"读成"系统没有流量"。
        """
        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window 只支持 {'/'.join(_WINDOWS)}")
        auth.require_admin(user)
        return await _call(_metrics, deps, window, top)

    @app.post("/api/v1/auth/login", response_model=LoginOut)
    async def login(request: LoginRequest) -> LoginOut:
        """用账号密码换一个 Bearer 令牌。失败一律 401 + 同一句话(防用户名枚举)。

        **路径约定(2026-10-06 起)**:业务接口一律在 ``/api/v1`` 下 —— 老接口已经在
        P0-3b-5-② 那一步全部迁完(见 ``docs/STATUS.md`` §7),**没有留别名**:
        两套路径就是两个真值来源,契约要写两遍,而前端只需要一个前缀。
        """
        token, user = await auth.login(request.username, request.password)
        return LoginOut(
            token=token,
            username=user.username,
            role=user.role,
            expires_in=TOKEN_TTL_SECONDS,
        )

    @app.get("/api/v1/auth/me", response_model=MeOut)
    async def me(user: User | None = Depends(auth.user)) -> MeOut:
        """当前账号(给前端决定显不显示上传/删除/报表)。**必须带令牌**,否则 401。"""
        if user is None:
            raise HTTPException(status_code=503, detail="没有接上认证存储")
        return MeOut(username=user.username, role=user.role, is_admin=user.is_admin)

    @app.post("/api/v1/create_session", response_model=SessionOut)
    async def create_session(user: User | None = Depends(auth.user)) -> SessionOut:
        """发一个新会话 ID,并**当场记下归属** —— 归属必须在拿到 ID 的那一刻就绑定,
        否则"先建会话再补绑"之间有一段谁都能读它的窗口。

        纯计算 + 一次插入(插入进线程池)。
        """
        session_id = str(uuid.uuid4())
        await auth.bind_session(session_id, user)
        return SessionOut(session_id=session_id)

    @app.get("/api/v1/history/{session_id}", response_model=HistoryOut)
    async def history(session_id: str, user: User | None = Depends(auth.user)) -> HistoryOut:
        """最近 N 轮历史 —— 与喂给模型的**同一份**(所以前端看到的和模型看到的不会不一致)。

        **先查归属再读**:这一条正是 P0-3a 要堵的洞 —— 会话 ID 以前是裸字符串,猜到就能读。
        """
        await auth.check_owner(session_id, user)
        return await _call(_history, deps, session_id)

    @app.delete("/api/v1/history/{session_id}", response_model=DeletedOut)
    async def clear_history(
        session_id: str, user: User | None = Depends(auth.user)
    ) -> DeletedOut:
        """清空一段会话,返回删掉几轮。**同样是别人删不掉的**(先查归属)。"""
        await auth.check_owner(session_id, user)
        return await _call(_delete_history, deps, session_id)

    @app.post("/api/v1/query", response_model=QueryResponse)
    async def query(
        request: QueryRequest, user: User | None = Depends(auth.user)
    ) -> QueryResponse:
        """非流式问答:FAQ 命中直答,否则走知识库(拒答也是一句人话,不会是 null)。

        问候匹配是纯计算(一次正则),留在循环里;剩下的整条链路一次 ``_call`` 搬走。

        **带 session_id 时先查归属**:否则带上别人的会话 ID 就能拿他的历史继续提问 ——
        那和直接读历史一样是越权(所以这条检查放在问候分支**之前**)。
        """
        await auth.check_owner(request.session_id, user)
        greeting = match_greeting(request.query)
        if greeting is not None:
            return _greeting_response(greeting, request.session_id)
        return await _call(_answer, deps, request)

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

                events = await _call(_stream_events, deps, request)
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

        内置调试页还在 `/static/index.html`(它窄、无依赖,后端出问题时更有用)。
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
        _options(deps, QueryRequest(query=warmup_query)),
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
    # 依赖只装配一次:预热与 create_app 用同一份,免得建出两套单例缓存
    deps = build_real_deps()
    if settings.api.warmup:
        _warmup(deps)
    # 地址走配置:类默认仍是 0.0.0.0:8080
    # (绑回环时 WSL 里 Windows 浏览器连不上,进容器后也映射不出去)
    uvicorn.run(create_app(deps), host=settings.api.host, port=settings.api.port)

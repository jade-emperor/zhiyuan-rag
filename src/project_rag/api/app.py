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

其余四处与讲义刻意不同:

1. **``POST /api/query`` 直接给完整答案**,不像讲义那样回一句「请使用 WebSocket 接口」。
   我们有同步用例,强制前端换协议是更差的契约;要逐字出字才走 WS。
2. **过滤走 ``RetrievalOptions.source``,不引 ``VALID_SOURCES`` 白名单**:白名单该从哪来
   还没定论(见 ``deferred.md`` §1.3 第 22 条);而 ``/api/sources`` 直接从向量库读**真实**
   来源,比在配置里写死一份更可信。
3. **问候拦截放在 ``application/greeting.py``**:「什么算寒暄」是业务规则,HTTP 与 WS
   共用一份,不写在接口层。

装配复用 ``cli/deps.py`` 那一套(``Deps`` / ``build_real_deps`` / ``knowledge_callable``):
**组合根只该有一套**,api 再写一份装配,两份迟早不一致。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from project_rag.api.schemas import (
    CitationOut,
    DeletedOut,
    HealthOut,
    HistoryOut,
    HistoryTurnOut,
    QueryRequest,
    QueryResponse,
    SessionOut,
    SourcesOut,
)
from project_rag.application import Session, answer_question, stream_question
from project_rag.application.greeting import match_greeting
from project_rag.cli.deps import Deps, build_real_deps, knowledge_callable
from project_rag.config import get_settings
from project_rag.domain import Citation, QaAnswer, QaEvent, RetrievalOptions
from project_rag.observability import get_logger

_logger = get_logger("api.app")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

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
    迭代它,一个用户的流会把所有人的请求都卡住(讲义 6.3 的坑)。WebSocket 那条没有
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


def _options(deps: Deps, request: QueryRequest) -> RetrievalOptions:
    """输入:依赖 + 请求。行为:把配置里的旋钮与请求里的覆盖拼成一次检索的参数。输出:RetrievalOptions。

    旋钮口径与 ``cli/commands/rag.py`` 保持一致(M4 契约里有一条 AST 比对盯着 builder 与 rag.py)。
    """
    settings = get_settings()
    return RetrievalOptions(
        k=request.k if request.k is not None else settings.rag.retrieval_k,
        max_context=settings.rag.candidate_m,
        source=request.source_filter,
        fusion=settings.rag.fusion,
        rrf_k=settings.rag.rrf_k,
        bm25_weight=settings.rag.bm25_weight,
        vector_weight=settings.rag.vector_weight,
    )


def _citations(citations: Iterable[Citation]) -> list[CitationOut]:
    """输入:引用序列。行为:转成出参模型(预览用父块正文,模型真读到的那段)。输出:出参列表。"""
    return [
        CitationOut(
            index=citation.index,
            source=citation.chunk.source,
            doc_id=citation.chunk.doc_id,
            preview=citation.chunk.context_text[:120],
            score=citation.score,
            rerank_score=citation.rerank_score,
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
    settings = get_settings()
    result = answer_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps, request.query, _options(deps, request), rerank=settings.rag.rerank
        ),
        faq_threshold=deps.faq_threshold,
        session=_session(deps, request.session_id),
        thinking=deps.thinking,  # 只进延迟账(M5.8),不影响答案
    )
    return _to_response(result, request.session_id)


def _stream_events(deps: Deps, request: QueryRequest) -> Iterator[QaEvent]:
    """输入:依赖 + 请求。行为:同步地把流式用例的帧流**搭起来**(装端口)。输出:同步帧迭代器。

    重活(嵌入/检索/生成)发生在迭代它的时候,那时已经在 ``_in_thread`` 的池子里了;
    但「搭起来」这一步要调工厂(可能加载模型),所以它本身也得在 ``_call`` 里执行。
    """
    settings = get_settings()
    return stream_question(
        request.query,
        faq_store=_factory(deps, "faq_store_factory"),
        matcher=_factory(deps, "matcher_factory"),
        knowledge=knowledge_callable(
            deps, request.query, _options(deps, request), rerank=settings.rag.rerank
        ),
        faq_threshold=deps.faq_threshold,
        session=_session(deps, request.session_id),
        thinking=deps.thinking,  # 只进延迟账(M5.8),不影响答案
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

    寒暄**不查库、不走向量检索、也不写历史**(讲义 6.3 的前置拦截):它没有任何需要
    检索的内容,写进历史只会给下一轮的提示词添噪声。
    """
    return QueryResponse(answer=reply, channel="greeting", session_id=session_id)


def create_app(deps: Deps | None = None) -> FastAPI:
    """输入:依赖集合(不传就真实装配)。行为:建 FastAPI 应用并注册全部路由。输出:应用。

    传 ``deps`` = 不读 .env、不连任何东西,所以整条 HTTP/WS 链路可测(同 CLI 的注入方式)。
    """
    deps = deps if deps is not None else build_real_deps()
    settings = get_settings()

    app = FastAPI(
        title="project-rag",
        description="知识库问答问答:FAQ 优先,未命中转知识库(答案带引用)",
        version="0.1.0",
    )
    # 本地服务先全开;要对外就得收紧到具体来源(见 deferred.md §1:限流/鉴权)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/health", response_model=HealthOut)
    async def health() -> HealthOut:
        """进程探活。**刻意不探容器**:容器状态有 check_env_services.py 负责,这里只回答「服务在不在」。

        没有同步调用,所以不需要 ``_call`` —— 该是纯协程的地方就纯协程。
        """
        return HealthOut(status="ok")

    @app.get("/api/sources", response_model=SourcesOut)
    async def sources() -> SourcesOut:
        """语料里真实有哪些来源(去重升序)。

        读的是**向量库**而不是配置白名单:要回答的是「库里有哪几门课」,不是「配置里写了哪几个」。
        语料上千块,``load_all`` 一次可接受;上万块时再给端口加一个 ``sources()``(见 deferred)。
        """
        return await _call(_sources, deps)

    @app.post("/api/create_session", response_model=SessionOut)
    async def create_session() -> SessionOut:
        """发一个新会话 ID。用 uuid4:它同时是「访问这段历史的凭证」,必须不可猜。

        纯计算,不进线程池。
        """
        return SessionOut(session_id=str(uuid.uuid4()))

    @app.get("/api/history/{session_id}", response_model=HistoryOut)
    async def history(session_id: str) -> HistoryOut:
        """最近 N 轮历史 —— 与喂给模型的**同一份**(所以前端看到的和模型看到的不会不一致)。"""
        return await _call(_history, deps, session_id)

    @app.delete("/api/history/{session_id}", response_model=DeletedOut)
    async def clear_history(session_id: str) -> DeletedOut:
        """清空一段会话,返回删掉几轮。"""
        return await _call(_delete_history, deps, session_id)

    @app.post("/api/query", response_model=QueryResponse)
    async def query(request: QueryRequest) -> QueryResponse:
        """非流式问答:FAQ 命中直答,否则走知识库(拒答也是一句人话,不会是 null)。

        问候匹配是纯计算(一次正则),留在循环里;剩下的整条链路一次 ``_call`` 搬走。
        """
        greeting = match_greeting(request.query)
        if greeting is not None:
            return _greeting_response(greeting, request.session_id)
        return await _call(_answer, deps, request)

    @app.websocket("/api/stream")
    async def stream(websocket: WebSocket) -> None:
        """流式问答:客户端发一条 JSON(同 QueryRequest),服务端逐帧推 ``sources``/``token``/``done``。

        三条与讲义不同(都是它的坑):
        1. 问候命中**不关连接** —— 发完话术继续等下一句(讲义那样直接 ``break`` 掉了整条连接);
        2. 同步的帧流在**线程池**里取(见 ``_in_thread``),不卡事件循环;
        3. 请求体畸形时回一帧 ``type="error"``,连接留着 —— 前端能看到「你发的不是 JSON」。
        """
        await websocket.accept()
        try:
            while True:
                payload = await websocket.receive_json()
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
    async def index() -> FileResponse:
        """根路径给一个能直接用的最小页面(它用 WebSocket 逐字显示)。"""
        return FileResponse(_STATIC_DIR / "index.html")

    return app


# 预热用的问题:挑一个**已知能从真语料里召回**的(实测「装饰器有什么用」召回 2 条),
# 这样关键字那一路、融合、重排都走得到。它不会调大模型(见 _warmup_once)。
_WARMUP_QUERY = "装饰器有什么用"


def _warmup_once(deps: Deps) -> None:
    """输入:依赖集合。行为:把冷启动那几笔账先付掉。输出:无(失败往上抛,由 ``_warmup`` 兜)。

    两件事:

    1. **FAQ 侧**:读一次真表 + 用真匹配器匹配一次 —— 冷建 FAQ 的 BM25 索引(实测 ~300ms);
    2. **知识库侧**:取到第一帧就收工。这一步会构造嵌入/重排/向量库/关键字索引四个单例
       (M5.6 之前那是**每个请求** 4.4s 的开销),并让 BM25 的语料索引与 Milvus 连接就位。

    **为什么只取第一帧**:第一帧一定出现在调大模型**之前** —— 有引用是 ``sources`` 帧,
    没引用是拒答话术帧。所以预热把重活全干了,却一分 token 钱都不花。
    """
    settings = get_settings()
    entries = _factory(deps, "faq_store_factory").load_all()
    if entries:
        _factory(deps, "matcher_factory").best_match(_WARMUP_QUERY, entries)

    knowledge = knowledge_callable(
        deps,
        _WARMUP_QUERY,
        _options(deps, QueryRequest(query=_WARMUP_QUERY)),
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
    # 依赖只装配一次:预热与 create_app 用同一份,免得建出两套单例缓存
    deps = build_real_deps()
    if get_settings().api.warmup:
        _warmup(deps)
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8080)

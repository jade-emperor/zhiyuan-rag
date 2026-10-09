#!/usr/bin/env python
"""M5 验收：融合路由 —— FAQ 通道优先、知识库通道兜底。

用法::

    uv run python scripts/acceptance/check_m5_qa.py

本脚本是 M5 契约的**唯一权威**（`docs/STATUS.md` 的 M5 一节只留设计理由，不重复规格）。

契约先落地、代码后手敲，所以**代码还没进项目之前这份报告是红的** ——
那是这一步的起点，不是坏了。每条 FAIL 都点名缺什么。

公开契约（import 路径本身也是承诺）::

    from project_rag.domain import Channel, QaAnswer
    Channel = Literal["faq", "rag"]
    QaAnswer(answer: str, channel: Channel, citations: tuple[Citation, ...] = (),
             faq_match: FaqMatch | None = None, refused: bool = False)

    from project_rag.application import answer_question, match_faq
    answer_question(query, *, faq_store, matcher, knowledge, faq_threshold=0.85,
                    session=None, thinking=None) -> QaAnswer
    match_faq(query, *, store, matcher, threshold=0.85) -> FaqMatch | None

    from project_rag.cli.deps import knowledge_callable
    knowledge_callable(deps, query, options, *, rerank)
        -> Callable[[Sequence[ConversationTurn]], Iterator[QaEvent]]
      # 拿在手里**不构造任何东西**；被调用时才走惰性工厂拼出一次知识库通道的调用

路由（判定的对象从「要不要检索」换成了「谁答的」）::

    faq 命中 ⇔ matcher.best_match(query, store.load_all()) 非空且 score >= faq_threshold
      命中   → channel="faq"：直接返回人写的标准答案；不调 LLM、不查向量库、**也不构造它们**
      未命中 → channel="rag"：调用 knowledge()，把它的回答连证据一起透传出去
    知识库检索为空 → refused=True，answer 就是知识库通道那句拒答话术

**为什么 knowledge 是一个函数，而不是一堆端口对象**:Python 的函数参数是**先求值**的 ——
若把嵌入端口直接传进 `answer_question`，组合根在进入用例之前就已经把 BGE-M3
（2G 内存 + 秒级加载）装起来了。FAQ 命中的问题白付这份钱；配了 cuda 却没 GPU 的机器上
更是**直接抛错**，连一条人工维护的标准答案都答不出来 —— M5.1 第一次自测就踩到了这条，
所以它不是理论洁癖，是这条契约里最贵的那个坑。用「要用时才调」的函数：路由决定留在用例里，
构造留在组合根里（`knowledge_callable`），而且**可以在验收里直接断言「闭包一次都没被调用」**。

**为什么合流这一层不做缓存**：M2 的缓存是 FAQ 单通道的（key `faq:v1:{问题}`，
值里还存着 `faq`/`llm` 的来源标记）。合流复用它会出现「缓存里躺着一条 llm 答案 →
根本走不到知识库」，`channel` 也就被迫多出一个 `llm` 取值。命中判定本身很便宜
（一次全表读 + 一次 BM25），缓存要等 M6 常驻进程有压测数据再谈（见 `docs/deferred.md`）。

`QaAnswer` 的不变式（由 `answer_question` 保证，本脚本逐条盯着）::

    channel == "faq" ⟺ faq_match is not None（命中才有那条匹配）
    channel == "faq" →  citations == () 且 refused is False
    refused is True  →  channel == "rag" 且 answer 是知识库通道的拒答话术
    faq_match is None  ≠「得了 0 分」，而是「这次没走 FAQ 通道」

**命中判定只有一处**：`score >= threshold` 写在 `application/answer_faq.py` 的
`match_faq()` 里，FAQ 单通道与合流通道都从它拿结论。两个地方各写一遍比较运算符，
「≥ 还是 >」「边界算不算命中」迟早各长一套 —— 本脚本用「同一个分数两边结论必须相同」
把这件事钉住，而不是去读源码里有没有那个 `>=`。

故障语义（与 M2 一致）：FAQ 存储（MySQL）是**必需资源**，`load_all()` 抛错必须
fail-fast 往上抛；吞掉它会让「库挂了」伪装成「知识库答的」。Redis 那种纯加速才降级。

命令：``qa -q/--question`` 必填（缺了退出码 2），``--k`` / ``--source`` 与 ``rag`` 同款
（其余旋钮来自 `RAG_RAG__*`）。输出契约：**首行是答案**（便于管道消费），
其后是这个答案的来历 ——

- faq 通道：打出命中分数（形如 `0.9700`），让人一眼看出这是标准答案而不是生成的；
- rag 通道：打出引用清单（编号 + 分数 + 来源 + 预览），与 `rag` 命令**同一份渲染实现**
  （`cli/render.py`）—— 两个命令各写一份，同一份分数就会有两种显示口径。

缺依赖 → 退出码 1 并点名缺哪个工厂（fail-fast，不许静默降级成另一条通道）。

分层（静态扫描，不靠自觉）：`application/answer_question.py` 只准依赖 标准库 +
`project_rag.domain` / `project_rag.ports` / `project_rag.observability`；
**连相对导入 `adapters` 都不行** —— 合流只认识端口，不认识具体实现。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import io
import os
import subprocess
import sys
import typing
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _harness import check, report, SkipCheck
from _fakes import FakeEmbedder, FakeFaqStore, FakeKeywordIndex, FakeLLM, FakeMatcher, FakeReranker, SearchOnlyVectorStore

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

# 契约默认值（与 config/config.yaml 的 faq.threshold 必须一致）
DEFAULT_THRESHOLD = 0.85

# 假语料：FAQ 一条、知识库一块。字面量刻意写成一眼能认出来的样子，
# 断言里出现它们时不用回头看是哪儿来的。
FAQ_QUESTION = "退款流程怎么走？"
FAQ_ANSWER = "在订单页点「申请退款」，1-3 个工作日到账。"
KB_SOURCE = "ai"
KB_PARENT = "装饰器是一个接收函数并返回新函数的函数，用来在不改动原函数的前提下加行为。"
KB_ANSWER = "装饰器是给函数加行为的语法 [1]"
# ask 那条回归检查要一个「只可能来自裸模型兜底」的答案：它和知识库的答案不一样，
# 才能区分「走了知识库通道」和「让模型自由回答」
BARE_ANSWER = "这是模型的自由回答,没有检索任何资料"

# 本脚本自己落库用的连接参数：走 check_env_services.py 那套环境变量，
# 刻意不依赖被测的 Settings，否则「配置读错」和「落库连错」会互相掩护。
MYSQL_ENV = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "10093")),
    "user": os.environ.get("MYSQL_USER", "rag"),
    "password": os.environ.get("MYSQL_PASSWORD", "1234"),
    "database": os.environ.get("MYSQL_DATABASE", "rag"),
    "charset": "utf8mb4",
}

FAQ_TABLE_DDL = (
    "CREATE TABLE {table} ("
    "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
    "question VARCHAR(512) NOT NULL, "
    "answer TEXT NOT NULL"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)

# 合流用例只准出现这些顶层模块（相对导入另外判）
ALLOWED_LAYERS = ("project_rag.domain", "project_rag.ports", "project_rag.observability")

# 「合流这一层不许碰」的第三方 SDK 名单：手写的，漏一个就等于这条纪律对它失效。
FORBIDDEN_SDKS = (
    "pymysql",
    "redis",
    "jieba",
    "rank_bm25",
    "pymilvus",
    "sentence_transformers",
    "torch",
    "langchain",
    "langchain_deepseek",
    "openai",
    "ragas",
)

# 知识库那半边的五个工厂：FAQ 命中时一个都不该被调用
KB_FACTORIES = (
    "llm_factory",
    "embedder_factory",
    "vector_store_factory",
    "keyword_index_factory",
    "reranker_factory",
)


def _imports_of(path: Path) -> list[tuple[str, int]]:
    """输入:一个 .py 文件。行为:解析出它 import 的 (模块名, 相对层数)。输出:列表。

    为什么不用 ``"jieba" in text``:docstring 里提一句「jieba 分词」也会命中 ——
    检查要盯的是**依赖**，不是文字。
    """
    pairs: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            pairs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            pairs.append((node.module or "", node.level))
    return pairs


# ── 契约导入（缺什么就报什么，不让 ImportError 淹掉后续诊断） ────────────────────
def _require(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(
            f"{module_name} 缺少公开成员 {missing}；"
            f"请在 {module_name.replace('.', '/')}/__init__.py 里显式 re-export"
        )
    return module


def _models():
    module = _require("project_rag.domain", ("FaqEntry", "FaqMatch", "QaAnswer"))
    return module.FaqEntry, module.FaqMatch, module.QaAnswer


def _qa_answer():
    return _models()[2]


def _answer_question():
    return _require("project_rag.application", ("answer_question",)).answer_question


def _match_faq():
    return _require("project_rag.application", ("match_faq",)).match_faq


def _answer_faq():
    return _require("project_rag.application", ("answer_faq",)).answer_faq


def _knowledge_callable():
    return _require("project_rag.cli.deps", ("knowledge_callable",)).knowledge_callable


def _refusal_text() -> str:
    """拒答话术的**唯一来源**是知识库通道那个常量：合流不许自己另写一句。"""
    module = _require("project_rag.application.answer_rag", ("REFUSAL_TEXT",))
    return str(module.REFUSAL_TEXT)


ANSWER_QUESTION_SIGNATURE = (
    "answer_question(query, *, faq_store, matcher, knowledge, faq_threshold=0.85, "
    "session=None, thinking=None) -> QaAnswer"
)
MATCH_FAQ_SIGNATURE = "match_faq(query, *, store, matcher, threshold=0.85) -> FaqMatch | None"
KNOWLEDGE_CALLABLE_SIGNATURE = (
    "knowledge_callable(deps, query, options, *, rerank) -> "
    "Callable[[Sequence[ConversationTurn]], RagAnswer]"
)

# ── 测试替身 ──────────────────────────────────────────────────────────────────


def _faq_entry():
    FaqEntry = _models()[0]
    return FaqEntry(id=7, question=FAQ_QUESTION, answer=FAQ_ANSWER)


def _kb_hit(chunk_id: str = "c1", parent_id: str = "p1", text: str = KB_PARENT):
    from project_rag.domain import Chunk, RetrievedChunk

    chunk = Chunk(chunk_id, f"{text[:8]} 子块", KB_SOURCE, "d1", parent_id, text)
    return RetrievedChunk(chunk=chunk, score=0.91)


def _options(*, k: int = 5, max_context: int | None = None, source: str | None = None):
    from project_rag.domain import RetrievalOptions

    return RetrievalOptions(k=k, max_context=max_context, source=source)


class _World:
    """一整套假端口 + 记录。每个检查只改一个旋钮，其余用默认值。

    两个层次共用它：**用例层**的检查用 ``knowledge``（一个会记调用次数的假知识库通道），
    **命令层**的检查用那几个端口假实现（让真的 ``answer_rag`` 跑起来看接线）。
    """

    def __init__(
        self,
        faq_score: float | None = None,
        *,
        faq_none: bool = False,
        faq_empty: bool = False,
        store_error: Exception | None = None,
        matcher_error: Exception | None = None,
        hits: list | None = None,
        keyword_hits: list | None = None,
        k: int = 5,
        max_context: int | None = None,
        source: str | None = None,
        rag=None,
    ) -> None:
        from project_rag.domain import Citation, RagAnswer

        self.entry = _faq_entry()
        self.llm = FakeLLM(reply=KB_ANSWER)
        self.embedder = FakeEmbedder()
        self.vector_store = SearchOnlyVectorStore([_kb_hit()] if hits is None else hits)
        self.keyword_index = FakeKeywordIndex(keyword_hits or [])
        self.faq_store = FakeFaqStore(
            [] if faq_empty else [self.entry], error=store_error
        )
        self.matcher = FakeMatcher(faq_score, none=faq_none, error=matcher_error)
        self.options = _options(k=k, max_context=max_context, source=source)
        # 假知识库通道的产出：默认把假向量库的命中原样当引用，两个层次看到的证据一致
        self.rag = rag if rag is not None else RagAnswer(
            answer=KB_ANSWER,
            citations=tuple(
                Citation(index=index, chunk=hit.chunk, score=hit.score, routes=("vector",))
                for index, hit in enumerate(self.vector_store.hits, start=1)
            ),
        )
        self.knowledge_calls = 0
        self.knowledge_history: tuple = ()
        self.factory_calls: dict[str, int] = {}

    def knowledge(self, history=()):
        """假的知识库通道：记下「被调用几次」和「收到什么历史」，然后按帧协议吐一帧流。

        模仿真通道的交付方式(sources + token + done),而不是直接扔一个 RagAnswer ——
        用例层只转发 token、收拢 done,喂别的东西它会当违约。
        """
        self.knowledge_calls += 1
        self.knowledge_history = tuple(history)
        QaEvent = _require("project_rag.domain", ("QaEvent",)).QaEvent
        if self.rag.citations:
            yield QaEvent(kind="sources", citations=self.rag.citations)
        yield QaEvent(kind="token", text=self.rag.answer)
        yield QaEvent(
            kind="done",
            answer=self.rag.answer,
            citations=self.rag.citations,
            refused=self.rag.refused,
            truncated=self.rag.truncated,
        )

    def call(
        self,
        query: str = FAQ_QUESTION,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        session=None,
    ):
        return _answer_question()(
            query,
            faq_store=self.faq_store,
            matcher=self.matcher,
            knowledge=self.knowledge,
            faq_threshold=threshold,
            session=session,
        )


def _deps(world: _World, **overrides):
    """按 Deps 契约装一整套假工厂，并记下每个工厂**被调用了几次**。

    调用次数是这一版最关键的证据：合流入口必须把知识库那半边的构造推迟到
    「真的要走知识库」那一刻 —— FAQ 命中的问题不该把嵌入/重排模型拽起来。
    """
    from project_rag.cli import Deps

    counters = world.factory_calls

    def wrap(name: str, factory):
        def call():
            counters[name] = counters.get(name, 0) + 1
            return factory()

        return call

    fields = {
        "llm_factory": wrap("llm_factory", lambda: world.llm),
        "faq_store_factory": wrap("faq_store_factory", lambda: world.faq_store),
        "matcher_factory": wrap("matcher_factory", lambda: world.matcher),
        "cache_factory": None,
        "faq_threshold": DEFAULT_THRESHOLD,
        "embedder_factory": wrap("embedder_factory", lambda: world.embedder),
        "vector_store_factory": wrap("vector_store_factory", lambda: world.vector_store),
        "keyword_index_factory": wrap("keyword_index_factory", lambda: world.keyword_index),
        "reranker_factory": wrap("reranker_factory", lambda: FakeReranker()),
    }
    fields.update(overrides)
    return Deps(**fields)


def _run_injected(argv: list[str], deps) -> tuple[int, str, str]:
    """在进程内跑 CLI（注入 Deps，不读 .env、不联网），把 stdout/stderr 收回来。"""
    from project_rag.cli import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


def _run_cli(
    argv: list[str], extra_env: dict[str, str] | None = None, timeout: int = 90
):
    """跑一个**真子进程**（BuildRealDeps 那条路）。用于必须换环境变量的检查。"""
    env_map = dict(os.environ)
    env_map.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "project_rag", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(ROOT),
        env=env_map,
    )


def _kb_factories_used(world: _World) -> dict[str, int]:
    """知识库那半边的工厂各被调用了几次（0 = 没构造，也就没加载模型）。"""
    return {name: world.factory_calls.get(name, 0) for name in KB_FACTORIES}


def _seed_faq(rows: list[tuple[int, str, str]]) -> str:
    """建一张临时 FAQ 表并落库；返回表名（由调用方负责删除）。"""
    import pymysql

    table = f"faq_m5_{uuid.uuid4().hex[:8]}"
    target = f"{MYSQL_ENV['host']}:{MYSQL_ENV['port']}"
    try:
        conn = pymysql.connect(connect_timeout=5, autocommit=True, **MYSQL_ENV)
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题，不是契约失败
        raise SkipCheck(
            f"MySQL 连不上（{target}）：{type(exc).__name__}: {exc}；"
            "先看 Windows 侧 Docker Desktop 是否开着（docs/STATUS.md §5 第 10 条），"
            "再 docker compose up -d，或跑 check_env_services.py 定位"
        ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(FAQ_TABLE_DDL.format(table=table))
            cur.executemany(
                f"INSERT INTO {table} (id, question, answer) VALUES (%s, %s, %s)", rows
            )
    finally:
        conn.close()
    return table


def _drop_faq(table: str) -> None:
    import pymysql

    try:
        conn = pymysql.connect(connect_timeout=5, autocommit=True, **MYSQL_ENV)
    except Exception:  # noqa: BLE001 - 清理是尽力而为
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        conn.close()


def _gpu_invisible(device: str) -> str | None:
    """配置让用 GPU、但当前进程看不到 GPU 时，返回一句给人看的理由。

    沙箱挂的是私有 ``/dev``（``/dev/dxg`` 不在其中），那是**沙箱**、不是环境坏了 ——
    写成 SKIP 的理由而不是 FAIL，是为了不让后来的人把它当成 GPU 故障去修。
    """
    if not device.startswith("cuda"):
        return None
    import torch

    if torch.cuda.is_available():
        return None
    return (
        f"配置 device={device}，但当前进程看不到 GPU（沙箱的私有 /dev 里没有 /dev/dxg）。"
        "这条要在**自己的终端**里跑默认配置，或临时 RAG_EMBEDDING__DEVICE=cpu；"
        "owner：本地环境"
    )


def _milvus_count() -> int:
    """真实集合里的块数；连不上则 SKIP。"""
    from pymilvus import MilvusClient

    from project_rag.config import get_settings

    settings = get_settings()
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"
    try:
        client = MilvusClient(uri=target)
        rows = client.query(
            collection_name=settings.milvus.collection,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(rows[0]["count(*)"]) if rows else 0
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题
        raise SkipCheck(f"Milvus 连不上（{target}）：{type(exc).__name__}") from exc


# ── 0. 脚本自身 ───────────────────────────────────────────────────────────────
@check("脚本自身：检查不许引用后文定义的帮助函数（@check 在定义时就立即执行）")
def _() -> str:
    """``@check(name)`` 会在**定义时立即执行**函数，所以整份脚本的检查是 import 期
    按文件顺序跑完的。于是「A 检查用了后面才定义的帮助函数」在写的时候完全不报错，
    而是在跑到 A 的时候抛 ``NameError`` —— 报的是那个帮助函数的名字，看着像它写错了。
    """
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
            continue  # 不是 @check 装饰的（比如 main）
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
            "这些检查引用了**后文**才定义的帮助函数，跑起来会是 NameError：\n    "
            + "\n    ".join(problems)
            + "\n修法：把那个帮助函数移到**文件顶部**（所有 @check 之前）"
        )
    return f"{len(defined)} 个顶层定义全部先于引用它的检查"


# ── 1. 领域模型：合流通道的回答 ────────────────────────────────────────────────
@check("domain：QaAnswer 是 frozen+slots dataclass，字段名与默认值按契约")
def _() -> str:
    QaAnswer = _qa_answer()
    if not dataclasses.is_dataclass(QaAnswer):
        raise AssertionError("QaAnswer 不是 dataclass；见本脚本头部契约")
    if not getattr(QaAnswer, "__dataclass_params__").frozen:
        raise AssertionError("QaAnswer 应为 frozen=True（答案与证据链一旦定下就不该被改）")
    if not getattr(QaAnswer, "__slots__", None):
        raise AssertionError("QaAnswer 应为 slots=True（跟其他领域模型风格一致）")

    fields = {field.name: field for field in dataclasses.fields(QaAnswer)}
    want = ["answer", "channel", "citations", "faq_match", "refused", "truncated"]
    if list(fields) != want:
        raise AssertionError(f"字段应为 {want}，实际 {list(fields)}")
    for name, default in (("citations", ()), ("faq_match", None), ("refused", False)):
        actual = fields[name].default
        if actual != default:
            raise AssertionError(
                f"QaAnswer.{name} 的默认值应为 {default!r}，实际 {actual!r}"
            )
    return "字段与默认值一致"


@check("domain：channel 是 Literal['faq','rag']（写错了类型检查器要拦得住，不是裸 str）")
def _() -> str:
    QaAnswer, FaqMatch = _qa_answer(), _models()[1]
    hints = typing.get_type_hints(QaAnswer)

    if hints["answer"] is not str:
        raise AssertionError(f"answer 应为 str，实际 {hints['answer']!r}")
    if typing.get_args(hints["channel"]) != ("faq", "rag"):
        raise AssertionError(
            f"channel 应为 Literal['faq','rag']，实际 {hints['channel']!r}"
        )
    from project_rag.domain import Citation

    if typing.get_args(hints["citations"]) != (Citation, Ellipsis):
        raise AssertionError(f"citations 应为 tuple[Citation, ...]，实际 {hints['citations']!r}")
    if typing.get_args(hints["faq_match"]) != (FaqMatch, type(None)):
        raise AssertionError(f"faq_match 应为 FaqMatch | None，实际 {hints['faq_match']!r}")
    if hints["refused"] is not bool:
        raise AssertionError(f"refused 应为 bool，实际 {hints['refused']!r}")
    return "channel/answer/citations/faq_match/refused 类型齐全"


@check("domain 包入口导出 QaAnswer 与 Channel（漏一个导出会让 import 它的模块整个起不来）")
def _() -> str:
    module = _require("project_rag.domain", ("QaAnswer", "Channel"))
    exported = getattr(module, "__all__", [])
    missing = [n for n in ("QaAnswer", "Channel") if n not in exported]
    if missing:
        raise AssertionError(
            f"domain/__init__.py 的 __all__ 里缺 {missing}；"
            "import 列表和 __all__ 各要加一行（只加一处不够）"
        )
    if typing.get_args(module.Channel) != ("faq", "rag"):
        raise AssertionError(f"Channel 应为 Literal['faq','rag']，实际 {module.Channel!r}")
    return "QaAnswer / Channel 都能从包入口导入"


# ── 2. 用例：签名与分层 ────────────────────────────────────────────────────────
@check("application 包入口导出 answer_question 与 match_faq（import 路径本身是承诺）")
def _() -> str:
    _answer_question()
    _match_faq()
    exported = getattr(importlib.import_module("project_rag.application"), "__all__", [])
    missing = [n for n in ("answer_question", "match_faq") if n not in exported]
    if missing:
        raise AssertionError(f"application/__init__.py 的 __all__ 里缺 {missing}")
    return "两个入口都能从包入口导入"


@check("签名：answer_question 的参数名、关键字-only 与默认值按契约")
def _() -> str:
    params = inspect.signature(_answer_question()).parameters
    want_defaults = {
        "query": inspect.Parameter.empty,
        "faq_store": inspect.Parameter.empty,
        "matcher": inspect.Parameter.empty,
        "knowledge": inspect.Parameter.empty,
        "faq_threshold": 0.85,
        "session": None,
        # M5.8:思考档位的**静态标签**,只进延迟账;None = 账里给 '-'(注入式调用不该被编一个默认档位)
        "thinking": None,
    }
    if list(params) != list(want_defaults):
        raise AssertionError(f"参数名应为 {list(want_defaults)}，实际 {list(params)}")
    for name, default in want_defaults.items():
        param = params[name]
        if name == "query":
            if param.kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                raise AssertionError("query 应能按位置传（answer_question('问题', ...)）")
        elif param.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise AssertionError(
                f"{name} 必须是关键字参数（端口按位置传，调用点会认错）"
            )
        if param.default != default:
            raise AssertionError(
                f"{name} 默认值应为 {default!r}，实际 {param.default!r}；"
                f"契约签名：{ANSWER_QUESTION_SIGNATURE}"
            )
    return "参数与默认值一致"


@check("签名：match_faq 只回答「命中还是没命中」，不碰缓存也不调 LLM")
def _() -> str:
    params = inspect.signature(_match_faq()).parameters
    if list(params) != ["query", "store", "matcher", "threshold"]:
        raise AssertionError(
            f"参数名应为 ['query','store','matcher','threshold']，实际 {list(params)}；"
            f"契约签名：{MATCH_FAQ_SIGNATURE}"
        )
    for name in ("store", "matcher", "threshold"):
        if params[name].kind is not inspect.Parameter.KEYWORD_ONLY:
            raise AssertionError(f"{name} 必须是关键字参数")
    if params["threshold"].default != 0.85:
        raise AssertionError(f"threshold 默认应为 0.85，实际 {params['threshold'].default!r}")
    extra = sorted(set(params) - {"query", "store", "matcher", "threshold"})
    if extra:
        raise AssertionError(f"多出来的参数 {extra}：命中判定要能单独调，塞进缓存/LLM 就不纯了")
    return MATCH_FAQ_SIGNATURE


@check("签名：knowledge_callable 的入参按契约（测试要能整个替换掉知识库那半边）")
def _() -> str:
    params = inspect.signature(_knowledge_callable()).parameters
    want = ["deps", "query", "options", "rerank"]
    if list(params) != want:
        raise AssertionError(
            f"参数名应为 {want}，实际 {list(params)}；契约签名：{KNOWLEDGE_CALLABLE_SIGNATURE}"
        )
    for name in ("deps", "query", "options"):
        if params[name].kind is inspect.Parameter.KEYWORD_ONLY:
            raise AssertionError(f"{name} 不该是关键字-only（它是按位置传的三个位置参数）")
    if params["rerank"].kind is not inspect.Parameter.KEYWORD_ONLY:
        raise AssertionError("rerank 必须是关键字参数：真实/关闭重排是个开关，别按位置传")
    return KNOWLEDGE_CALLABLE_SIGNATURE


@check("分层：answer_question.py 只依赖 标准库 + domain/ports/observability")
def _() -> str:
    path = SRC / "application" / "answer_question.py"
    if not path.exists():
        raise AssertionError("src/project_rag/application/answer_question.py 还没落地")
    bad: list[str] = []
    for module, level in _imports_of(path):
        if level > 0 or not module:  # 同包相对导入，允许（下面另有一条专盯 adapters）
            continue
        root = module.split(".")[0]
        if root == "project_rag":
            if module != "project_rag" and not module.startswith(ALLOWED_LAYERS):
                bad.append(module)
        elif root not in sys.stdlib_module_names:
            bad.append(f"{module}（第三方 SDK 必须藏在适配器里）")
    if bad:
        raise AssertionError(
            f"合流用例违反依赖方向：{bad}。用例只认识端口 —— 否则换个向量库就要改用例"
        )
    return "只依赖 标准库 + domain/ports/observability"


@check("分层：answer_question.py 连相对导入 adapters 都不行（合流只借端口）")
def _() -> str:
    path = SRC / "application" / "answer_question.py"
    if not path.exists():
        raise AssertionError("src/project_rag/application/answer_question.py 还没落地")
    leaked = [
        f"from {'.' * level}{module} import ..."
        for module, level in _imports_of(path)
        if level > 0 and "adapters" in module
    ]
    if leaked:
        raise AssertionError(
            f"合流用例直接 import 了适配器：{leaked}；"
            "那样「换成别的向量库」就得改用例，而这个用例的全部意义就是只认端口"
        )
    sdks = sorted(
        {
            module.split(".")[0]
            for module, level in _imports_of(path)
            if level == 0 and module.split(".")[0] in FORBIDDEN_SDKS
        }
    )
    if sdks:
        raise AssertionError(f"这些 SDK 不许出现在合流层：{sdks}")
    return "没有 adapters、没有第三方 SDK"


# ── 3. 命中判定只有一处 ────────────────────────────────────────────────────────
@check("命中判定只有一处：同一分数下，match_faq / answer_faq / answer_question 结论一致")
def _() -> str:
    """钉的是「阈值语义只有一个真值来源」，不是「源码里有没有写 >=」。

    做法：拿同一个分数分别问三个入口，命中与不命中两侧都必须同进同退。
    谁要是把比较符写成 ``>``（或另抄一份阈值），这条立刻红。
    """
    from project_rag.domain import FaqAnswer

    for score, should_hit in ((DEFAULT_THRESHOLD, True), (DEFAULT_THRESHOLD - 0.01, False)):
        matched = (
            _match_faq()(
                FAQ_QUESTION,
                store=FakeFaqStore([_faq_entry()]),
                matcher=FakeMatcher(score),
                threshold=DEFAULT_THRESHOLD,
            )
            is not None
        )
        if matched != should_hit:
            raise AssertionError(
                f"match_faq 在 score={score} 时结论是 {matched}，应为 {should_hit}"
            )

        world = _World(score)
        single = _answer_faq()(
            FAQ_QUESTION,
            store=world.faq_store,
            matcher=world.matcher,
            llm=world.llm,
            threshold=DEFAULT_THRESHOLD,
        )
        if not isinstance(single, FaqAnswer):
            raise AssertionError(f"answer_faq 应返回 FaqAnswer，实际 {type(single)}")
        single_hit = single.source == "faq"
        fused_hit = world.call().channel == "faq"
        if single_hit != should_hit or fused_hit != should_hit:
            raise AssertionError(
                f"score={score}：match_faq={matched}，answer_faq 命中={single_hit}，"
                f"answer_question 命中={fused_hit} —— 三处对「算不算命中」的判断必须一致"
            )
    return "三个入口在阈值两侧同进同退"


# ── 4. 路由语义（用例层，全假，零网络零模型） ──────────────────────────────────
@check("FAQ 命中：channel=faq、答案就是标准答案、faq_match 带 entry 与分数、无引用")
def _() -> str:
    world = _World(0.97)
    result = world.call()
    if result.channel != "faq":
        raise AssertionError(f"0.97 ≥ 0.85 应走 faq 通道，实际 {result.channel!r}")
    if result.answer != FAQ_ANSWER:
        raise AssertionError(f"FAQ 通道应直返标准答案，实际 {result.answer!r}")
    if result.faq_match is None or result.faq_match.entry.id != 7:
        raise AssertionError(f"faq_match 应带命中的那条记录，实际 {result.faq_match!r}")
    if result.faq_match.score != 0.97:
        raise AssertionError(f"faq_match.score 应原样透传 0.97，实际 {result.faq_match.score!r}")
    if result.citations:
        raise AssertionError("FAQ 通道没有向量引用，citations 必须为空")
    if result.refused:
        raise AssertionError("FAQ 命中了就不可能是拒答")
    return "channel=faq，答案与匹配原样透传"


@check("FAQ 命中：knowledge() 一次都没被调用（嵌入/重排也就不会被构造）")
def _() -> str:
    world = _World(1.0)
    world.call()
    if world.knowledge_calls != 0:
        raise AssertionError(
            f"FAQ 命中却调了 {world.knowledge_calls} 次知识库通道 —— "
            "标准答案不需要检索,更不该把嵌入/重排模型拽起来"
        )
    if world.llm.calls:
        raise AssertionError("FAQ 命中的标准答案不该让模型参与")
    if world.faq_store.loads != 1 or world.matcher.calls != 1:
        raise AssertionError(
            f"FAQ 存储被读 {world.faq_store.loads} 次、匹配器被调 {world.matcher.calls} 次；应为各 1 次"
        )
    return "零知识库调用、零 LLM 调用"


@check("阈值边界：score == threshold 算命中（取等号，不是严格大于）")
def _() -> str:
    result = _World(DEFAULT_THRESHOLD).call()
    if result.channel != "faq":
        raise AssertionError(
            f"score 恰好等于阈值 {DEFAULT_THRESHOLD} 时应命中（边界取等号），"
            f"实际走了 {result.channel!r}"
        )
    return "边界取等号"


@check("阈值边界：score 略低于 threshold → 转知识库通道（不再是裸模型兜底）")
def _() -> str:
    world = _World(DEFAULT_THRESHOLD - 0.01)
    result = world.call()
    if result.channel != "rag":
        raise AssertionError(f"差一点点就该转知识库通道，实际 {result.channel!r}")
    if result.faq_match is not None:
        raise AssertionError("没命中 FAQ 时 faq_match 必须是 None（None ≠ 得了 0 分）")
    return f"{(DEFAULT_THRESHOLD - 0.01):.2f} < {DEFAULT_THRESHOLD} → rag"


@check("阈值来自参数：同一个 0.6 分的候选，阈值 0.5 算命中、阈值 0.85 不算")
def _() -> str:
    hit = _World(0.6).call(threshold=0.5)
    miss = _World(0.6).call(threshold=0.85)
    if hit.channel != "faq" or miss.channel != "rag":
        raise AssertionError(
            f"阈值没有从参数进来：threshold=0.5 → {hit.channel!r}，"
            f"threshold=0.85 → {miss.channel!r}；两个都该按传入值判定"
        )
    return "阈值是可注入参数，不是写死的 0.85"


@check("FAQ 未命中 → channel=rag：knowledge() 恰好调一次，答案与证据来自它")
def _() -> str:
    world = _World(0.2)
    result = world.call()
    if result.channel != "rag":
        raise AssertionError(f"未命中应转知识库通道，实际 {result.channel!r}")
    if result.answer != KB_ANSWER:
        raise AssertionError(f"答案应来自知识库通道，实际 {result.answer!r}")
    if world.knowledge_calls != 1:
        raise AssertionError(
            f"知识库通道应恰好被调 1 次，实际 {world.knowledge_calls} 次"
        )
    if result.citations != world.rag.citations:
        raise AssertionError("知识库通道的证据链应原样透传")
    return "转知识库通道并原样透传"


@check("FAQ 空表 / matcher 返回 None → 都不崩，转知识库通道")
def _() -> str:
    empty = _World(faq_empty=True)
    if empty.call().channel != "rag":
        raise AssertionError("FAQ 表是空的应转知识库通道")
    none = _World(None, faq_none=True)
    if none.call().channel != "rag":
        raise AssertionError("匹配器说「什么都不像」应转知识库通道")
    return "空表与无匹配都能落到知识库通道"


@check("知识库通道拒答：refused / 话术 / 无引用 原样透传（合流不自己编话术）")
def _() -> str:
    from project_rag.domain import RagAnswer

    refusal = RagAnswer(answer=_refusal_text(), refused=True)
    world = _World(0.1, rag=refusal)
    result = world.call()
    if not result.refused:
        raise AssertionError("知识库通道拒答了，合流必须把拒绝也透传出来")
    if result.answer != _refusal_text():
        raise AssertionError(
            f"拒答话术必须与知识库通道同源（answer_rag.REFUSAL_TEXT），实际 {result.answer!r}"
        )
    if result.channel != "rag":
        raise AssertionError(f"拒答发生在知识库通道，channel 应为 rag，实际 {result.channel!r}")
    if result.citations:
        raise AssertionError("拒答时不该有引用")
    if world.llm.calls:
        raise AssertionError("合流层根本没拿到 LLM 端口，一次都不该被调")
    return "拒答语义与 M3 的知识库通道一致"


@check("知识库通道正常：引用原样透传（序号从 1 连续、顺序没被改写）")
def _() -> str:
    hits = [_kb_hit("c1", "p1"), _kb_hit("c2", "p2"), _kb_hit("c3", "p3")]
    world = _World(0.1, hits=hits)
    result = world.call()
    if [citation.index for citation in result.citations] != [1, 2, 3]:
        raise AssertionError(f"引用编号应保持 1..3，实际 {[c.index for c in result.citations]}")
    if [citation.chunk.chunk_id for citation in result.citations] != ["c1", "c2", "c3"]:
        raise AssertionError("合流不该重排/重编引用 —— 那是知识库通道内部的事")
    return "三条引用原样透传，编号 1..3"


@check("故障：FAQ 存储抛错 → fail-fast（吞掉会把「库挂了」伪装成「知识库答的」）")
def _() -> str:
    world = _World(0.9, store_error=RuntimeError("MySQL 连不上"))
    try:
        world.call()
    except RuntimeError as exc:
        if "MySQL" not in str(exc):
            raise AssertionError(f"异常应原样上抛，实际 {exc!r}")
        if world.knowledge_calls:
            raise AssertionError(
                "FAQ 都还没问完就跑去走知识库通道了 —— 这不是降级，是换了条通道"
            )
        return "异常原样上抛，未改走知识库"
    raise AssertionError(
        "FAQ 存储抛错却被吞了 —— MySQL 是必需资源，连不上必须当场可见（同 M2）"
    )


@check("故障：匹配器抛错 → 同样 fail-fast（不许静默改走知识库通道）")
def _() -> str:
    world = _World(None, matcher_error=ValueError("分词器坏了"))
    try:
        world.call()
    except ValueError:
        if world.knowledge_calls:
            raise AssertionError("匹配器坏了却静默转去检索 —— 用户会以为这是知识库答的")
        return "异常原样上抛，未改走知识库"
    raise AssertionError("匹配器抛错被吞了")


@check("无副作用：同一输入连调两次结果一致（合流不做缓存）")
def _() -> str:
    world = _World(0.2)
    first = world.call()
    second = world.call()
    if first != second:
        raise AssertionError(f"两次结果不一致：{first!r} vs {second!r}")
    if world.knowledge_calls != 2:
        raise AssertionError(
            f"两次调用应各走一次知识库通道，实际 {world.knowledge_calls} 次 —— "
            "合流这一版刻意不做缓存（见脚本 docstring）"
        )
    return "两次结果相同，知识库各走一次"


# ── 5. 组合根：惰性那一半 ─────────────────────────────────────────────────────
@check("组合根：knowledge_callable 拿在手里不构造，调用时才建（惰性就落在这里）")
def _() -> str:
    world = _World()
    deps = _deps(world)
    call = _knowledge_callable()(
        deps, "装饰器是什么", _options(k=9, max_context=2, source=KB_SOURCE), rerank=True
    )
    if world.factory_calls:
        raise AssertionError(
            f"只是拿到那个闭包就构造了 {world.factory_calls} —— "
            "「造得晚」正是这个闭包存在的唯一理由"
        )
    events = list(call(()))
    if not events or events[-1].kind != "done":
        raise AssertionError(f"闭包应吐帧并以 done 收尾（帧协议），实际 {events!r}")
    used = _kb_factories_used(world)
    if any(count != 1 for count in used.values()):
        raise AssertionError(f"调用一次应各构造一次，实际 {used}")
    if world.vector_store.searches != [(9, KB_SOURCE)]:
        raise AssertionError(
            f"向量路应收到 (9, {KB_SOURCE!r})，实际 {world.vector_store.searches}"
        )
    if world.keyword_index.searches != [(9, KB_SOURCE)]:
        raise AssertionError(
            f"关键字路应收到 (9, {KB_SOURCE!r})，实际 {world.keyword_index.searches}"
        )
    return "闭包零构造；调用后两路都收到了 k=9 / source"


# ── 6. 命令行 ─────────────────────────────────────────────────────────────────
@check("--help 列出 qa；qa 缺 -q 退出码 2")
def _() -> str:
    helped = _run_cli(["--help"])
    if helped.returncode != 0:
        raise AssertionError(f"--help 退出码 {helped.returncode}：{helped.stderr[:200]}")
    if "qa" not in helped.stdout:
        raise AssertionError("--help 里没列出 qa 子命令（commands.ALL 里注册了吗？）")
    missing = _run_cli(["qa"])
    if missing.returncode != 2:
        raise AssertionError(
            f"qa 缺 -q 退出码应为 2（argparse 用法错误），实际 {missing.returncode}；"
            "别把 -q 写成可选，否则空问题会一路走到匹配层"
        )
    return "help 列出 qa；缺 -q → 2"


@check("CLI：FAQ 命中 → 首行标准答案 + 通道与分数可见，且知识库那半边一个都没构造")
def _() -> str:
    world = _World(0.97)
    code, out, err = _run_injected(["qa", "-q", FAQ_QUESTION], _deps(world))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stdout={out!r} stderr={err[:200]!r}")
    if not out.startswith(FAQ_ANSWER):
        raise AssertionError(f"首行应是答案（便于管道消费），实际 stdout={out!r}")
    if "faq" not in out.lower():
        raise AssertionError(
            f"没看出这是 FAQ 通道答的 —— 用户分不清「标准答案」和「生成的答案」。stdout={out!r}"
        )
    if "0.97" not in out:
        raise AssertionError(f"FAQ 通道该把命中分数打出来，实际 stdout={out!r}")
    used = _kb_factories_used(world)
    if any(used.values()) or world.vector_store.searches or world.llm.calls:
        raise AssertionError(
            f"FAQ 命中的问题却动了知识库那半边：工厂调用 {used}、检索 "
            f"{world.vector_store.searches}、LLM {world.llm.calls} 次。"
            "嵌入/重排是上 G 内存的重模型，FAQ 命中根本用不到；在配了 cuda 却没有 GPU 的"
            "机器上还会直接抛错，连标准答案都答不出来（这一条就是这么被发现的）"
        )
    return "qa 打印答案 + 通道 + 分数，知识库那半边零构造"


@check("CLI：FAQ 未命中 → 知识库通道，打印答案 + 引用清单（与 rag 同款渲染）")
def _() -> str:
    world = _World(0.2)
    code, out, err = _run_injected(["qa", "-q", "装饰器是什么"], _deps(world))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stdout={out!r} stderr={err[:200]!r}")
    if not out.startswith(KB_ANSWER):
        raise AssertionError(f"首行应是答案，实际 stdout={out!r}")
    if "引用" not in out or "[1]" not in out:
        raise AssertionError(
            f"知识库通道必须打印引用清单（那是给人核对的），实际 stdout={out!r}"
        )
    if KB_SOURCE not in out:
        raise AssertionError(f"引用清单里该有来源（{KB_SOURCE!r}），实际 stdout={out!r}")
    used = _kb_factories_used(world)
    if any(count != 1 for count in used.values()):
        raise AssertionError(f"未命中时应各构造一次，实际 {used}")
    return "qa 打印答案 + 引用清单，知识库那半边各构造一次"


@check("CLI：--k / --source / candidate_m 一路透传到检索（合流不许把旋钮吃掉）")
def _() -> str:
    from project_rag.config import get_settings

    hits = [_kb_hit("c1", "p1"), _kb_hit("c2", "p2"), _kb_hit("c3", "p3")]
    world = _World(0.2, hits=hits)
    code, out, err = _run_injected(
        ["qa", "-q", "装饰器是什么", "--k", "9", "--source", KB_SOURCE], _deps(world)
    )
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if world.vector_store.searches != [(9, KB_SOURCE)]:
        raise AssertionError(
            f"向量路应收到 (9, {KB_SOURCE!r})，实际 {world.vector_store.searches}"
        )
    if world.keyword_index.searches != [(9, KB_SOURCE)]:
        raise AssertionError(
            f"关键字路应收到 (9, {KB_SOURCE!r})，实际 {world.keyword_index.searches}"
        )
    # candidate_m 来自配置（不在命令行里）：3 条候选该被截到配置值
    cap = get_settings().rag.candidate_m
    expected = 3 if cap is None else min(3, cap)
    if f"[{expected}]" not in out or f"[{expected + 1}]" in out:
        raise AssertionError(
            f"引用清单应有 {expected} 条（candidate_m={cap}），实际 stdout={out!r}；"
            "说明命令行没把配置里的 max_context 传下去"
        )
    return f"k=9 / source={KB_SOURCE} 两路都收到，引用按 candidate_m={cap} 截断"


@check("CLI：缺必需工厂 → 退出码 1 并点名缺哪个（fail-fast，不许静默降级）")
def _() -> str:
    world = _World(0.97)
    code, out, err = _run_injected(
        ["qa", "-q", FAQ_QUESTION], _deps(world, embedder_factory=None, reranker_factory=None)
    )
    if code != 1:
        raise AssertionError(f"缺工厂应退出码 1，实际 {code}；stdout={out!r}")
    if "embedder_factory" not in err:
        raise AssertionError(f"错误信息要点名缺哪个工厂，实际 stderr={err!r}")
    return "缺工厂 → 1 且点名"


@check("CLI：知识库通道拒答 → 打印拒答话术、不打印引用清单、退出码 0")
def _() -> str:
    # 命令层走的是**真的** answer_rag，所以这里得让它检索为空（传 rag= 没用，
    # 那只换掉用例层那个假通道）
    world = _World(0.1, hits=[])
    code, out, err = _run_injected(["qa", "-q", "语料里没有的问题"], _deps(world))
    if code != 0:
        raise AssertionError(
            f"拒答不是错误，退出码应为 0，实际 {code}；stderr={err[:200]!r}"
        )
    if not out.startswith(_refusal_text()):
        raise AssertionError(f"拒答应给出同源话术，实际 stdout={out!r}")
    if "引用" in out:
        raise AssertionError(f"拒答时没有引用可列，实际 stdout={out!r}")
    return "拒答 → 0 且只说话术"


@check("回归：faq 的语义没被改（未命中仍是裸模型兜底）—— 合流是新增通道，不是替换")
def _() -> str:
    """M5.1 的决定：`qa` 是新增的合流入口，`faq` 保持 M2 的「FAQ 命中直答 / 未命中问模型」。

    这条钉住那个决定：谁要是把 faq 也改成转知识库，M2 那份契约会红，
    而这里会先把原因说清楚（比 M2 报「stdout 不是 LLM 兜底文本」好读）。
    （命令名 2026-09-23 从 `ask` 改成 `faq`：合流入口叫 `qa` 之后，`ask` 那种
    「我要提问」的口气喧宾夺主；`faq` / `rag` 成对，都点明走哪条通道。）
    """
    world = _World(0.2)
    world.llm.reply = BARE_ANSWER  # 让「模型兜底」和「知识库答案」区分得开
    code, out, err = _run_injected(["faq", "-q", "超纲问题"], _deps(world))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if BARE_ANSWER not in out:
        raise AssertionError(
            f"faq 未命中应让模型兜底并打印它的回答，实际 stdout={out!r}"
        )
    if not world.llm.calls:
        raise AssertionError("faq 未命中应调 LLM，实际一次都没调")
    if world.llm.prompts[0] != "超纲问题":
        raise AssertionError(
            f"faq 的兜底应把原问题直接发给模型，实际 prompt={world.llm.prompts[0]!r}"
        )
    if world.knowledge_calls:
        raise AssertionError("faq 走到了合流的那条路 —— 合流入口是 qa")
    return "faq 仍是 FAQ + 裸模型兜底（M2 语义未变）"


# ── 7. 真实链路（需要容器 / GPU；起不来则 SKIP 并说明原因和 owner） ────────────
@check("真实链路：真 FAQ 表 + 真 BM25 匹配器 → 原句走 faq 通道，不加载任何模型")
def _() -> str:
    table = _seed_faq([(1, FAQ_QUESTION, FAQ_ANSWER)])
    try:
        proc = _run_cli(
            ["qa", "-q", FAQ_QUESTION],
            {"RAG_FAQ__TABLE": table},
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"退出码 {proc.returncode}（应为 0）：stderr={proc.stderr[-300:]!r}"
            )
        if FAQ_ANSWER not in proc.stdout:
            raise AssertionError(
                f"原句在真表里没命中，实际 stdout={proc.stdout!r}；"
                "是不是路由没走 FAQ、或者阈值/口径对不上？"
            )
        if "faq" not in proc.stdout.lower():
            raise AssertionError(f"没打出通道名，实际 stdout={proc.stdout!r}")
        if "引用" in proc.stdout:
            raise AssertionError(
                f"FAQ 命中却打印了向量引用 —— 说明它绕到知识库通道去了。stdout={proc.stdout!r}"
            )
        if "Found no NVIDIA driver" in proc.stderr:
            raise AssertionError(
                "FAQ 命中的问题却去加载了嵌入/重排模型（配置 device=cuda 而本机没 GPU）—— "
                "知识库那半边的构造必须推迟到真正要走它的时候"
            )
        return f"真表命中（表 {table}），公众号道路由正确且零模型加载"
    finally:
        _drop_faq(table)


@check("真实链路：真语料 + 真嵌入 → 长尾问题走知识库通道并带引用（空库/无 GPU 则 SKIP）")
def _() -> str:
    from project_rag.adapters.embedder_bge_m3 import BGEM3Embedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.application import stream_rag
    from project_rag.config import get_settings

    settings = get_settings()
    reason = _gpu_invisible(settings.embedding.device)
    if reason:
        raise SkipCheck(reason)
    total = _milvus_count()
    if total == 0:
        raise SkipCheck(
            f"集合 {settings.milvus.collection} 是空的；先跑 "
            "uv run python -m project_rag ingest（owner：数据装载）"
        )

    # FAQ 侧给个空表：这一条只验「合流能把长尾问题送进知识库通道」，
    # 真 MySQL 那半边由上面那条负责。
    # ⚠️ knowledge 的契约是 **(history) -> 帧流**（M5.2 起收历史、M5.3b 起吐帧）：
    # 这里曾经写成 `lambda: answer_rag(...)`，两处都过时（漏参数 + 交了结果对象而不是帧流）。
    # 它被「无 GPU 则 SKIP」挡了一阵，签名/协议改了也没报错 ——
    # 教训见 STATUS.md §5 第 21 条：被 SKIP 挡住的检查等于没检查。
    world = _World(0.1, hits=[])
    world.faq_store = FakeFaqStore([])
    query = "装饰器到底有什么用,能举个场景吗"
    result = _answer_question()(
        query,
        faq_store=world.faq_store,
        matcher=world.matcher,
        knowledge=lambda history: stream_rag(
            query,
            embedder=BGEM3Embedder(settings.embedding),
            store=MilvusVectorStore(settings.milvus),
            llm=world.llm,
            keyword_index=None,
            reranker=None,
            options=_options(k=5, max_context=2, source=None),
            history=history,
        ),
        faq_threshold=DEFAULT_THRESHOLD,
    )
    if result.channel != "rag":
        raise AssertionError(f"长尾问题应走知识库通道，实际 {result.channel!r}")
    if not result.citations:
        raise AssertionError(
            f"{total} 块语料里一条都没召回；是不是没 ingest，或者查询词太偏？"
        )
    if result.refused:
        raise AssertionError("既然召回到了引用，就不该拒答")
    if len(result.citations) > 2:
        raise AssertionError(f"max_context=2 应截到 2 条，实际 {len(result.citations)} 条")
    sources = {citation.chunk.source for citation in result.citations}
    return f"库内 {total} 块 → 召回 {len(result.citations)} 条（来源 {sorted(sources)}）"


def main() -> int:
    return report("M5 融合路由（FAQ 优先 / 知识库兜底）验收")


if __name__ == "__main__":
    sys.exit(main())
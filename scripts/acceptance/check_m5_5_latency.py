#!/usr/bin/env python
"""验收：延迟观测（TTFT / tokens per second / 分段耗时）。

用法：

    uv run python scripts/acceptance/check_m5_5_latency.py

本脚本是这份契约的唯一权威**。
不需要容器、不花钱、不联网:全部用假端口 + 假 LLM 跑,日志用挂在
``project_rag.observability.latency`` 上的 handler 抓。

公开契约：

    from project_rag.observability import StreamTiming
    StreamTiming(label, channel=None)
        .mark(name) / .token(text) / .think(text) / .finish(channel=, refused=, truncated=)
        .usage(model=, input_tokens=, output_tokens=) / .line() / .emit()

    from project_rag.application.streaming import timed
    timed(events: Iterator[QaEvent], timing: StreamTiming) -> Iterator[QaEvent]

日志行（``INFO``，logger = ``project_rag.observability.latency``）：

    latency label=qa channel=rag thinking=auto faq_ms=108.2 sources_ms=330.5 \
retrieve_ms=- embed_ms=- vector_ms=- bm25_ms=- fuse_ms=- collapse_ms=- rerank_ms=- \
llm_think_ms=- llm_ttft_ms=- think_tokens=0 ttft_ms=930.9 tokens=173 gen_ms=3010.9 \
tps=57.5 total_ms=3944.2 refused=false truncated=false aborted=false

**字段与顺序固定**（30 个），拿不到的一律 ``-`` —— 「没量到」和「量到 0」必须分得开;
``awk``/``grep`` 才敢按列取。

计费口径的四个字段是（``llm_model`` / ``llm_input_tokens`` /
``llm_cached_input_tokens`` / ``llm_output_tokens``）:
provider 在流末尾回报的那一份用量,报表拿它乘单价算钱。它和 ``tokens``/``think_tokens``
**不许互相替代** —— 那两个是帧数（给 tps 用）,拿它们乘单价会漏掉思考帧与缓存命中的输入。
**缓存命中那一列单列**:它的单价与未命中差 50 倍(provider 的 prompt cache)。

口径（改口径 = 改契约）：

    检索分段     retrieve() 内部六处打点：embed / vector / bm25 / fuse / collapse / rerank
                 （六段**各自是自己的时长**;vector 与 bm25 那两路**并行**跑,
                 所以重叠时六段之和**可以大于** retrieve_ms,但绝不会小于它;
                 没装重排器时 rerank_ms 为）
    ttft_ms     从流开始到**第一帧非空 token** —— 用户看到第一个字的时刻
    gen_ms      第一帧 token → 最后一帧 token 的墙钟（不含 TTFT;不足两帧给 -,不给 0）
    tps         tokens / gen_ms;不足两帧给 -（单帧算不出速率,不编数）
    llm_think_ms ``llm_start`` → **最后一帧思考帧**;一个思考帧都没看到给 -
    think_tokens **思考帧数**（0 = 这一轮没看到思考;与 tokens 同样是帧数近似,不给）
    thinking     配置档位（auto/off/low/high/max）—— **静态标签,不是测量值**;没传给账本就给 -

**怎么读这三列**（最容易读错的是把「没调模型」当成「没想」）：

    llm_ttft_ms=-    think_tokens=0                 → 根本没调模型（FAQ 命中 / 检索为空拒答）
    llm_ttft_ms=578  think_tokens=0                 → 调了、没看到思考（如 thinking=off）
    llm_ttft_ms=2412 think_tokens=192 llm_think…    → 想了 ~2.4s,首字也等到 2.4s

``llm_think_ms`` 与 ``llm_ttft_ms`` **同一个原点**（``llm_start``）,所以两个值可以直接
相减:差值 = 「想完到吐出第一个字」之间的空档。这也意味着 ``llm_think_ms ≤ llm_ttft_ms``
该恒成立（唯一的例外是 provider 把思考帧和正文交错着发 —— DeepSeek 不这样）。

``tokens`` 数的是**非空 token 帧的个数**,``think_tokens`` 同理数思考帧 —— DeepSeek 的 SSE
一个 chunk 通常就是一个 token,够用的近似;精确值要引 tokenizer。

**思考内容不许混进答案**:适配器把 ``reasoning_content`` 装在 ``LlmDelta.reasoning_text``
上与 ``text`` **分开**（不是塞进 ``text`` 再打个标记）。这样「答案 = 所有 text 帧拼起来」
这条恒等式不用加任何过滤条件,漏加过滤也不会把思考写到用户脸上。

**五件刻意定下来的事**：

    1. **账本显式传递,不用 contextvars**。帧流是同步生成器,会在线程池的不同线程里被
       next()（api 的 _in_thread）,contextvar 的写入会随调用上下文丢失 —— 那会得到
       「有时有账、有时没账」的日志,比没有日志更坏。
    2. **只观察、不改写**。timed 包在帧流外面,内容/顺序/条数一个字节都不动（本脚本拿
       未包装的帧逐帧比对）。
    3. **逐帧透传、不许预取**。写成 list(events) 就等于「全部生成完再交出来」,
       「边生成边出」和 TTFT 一起没了（本脚本取走第一帧后断言上游只产出了一帧）。
    4. **中断也要留账**。客户端断开时生成器被 close(),走 GeneratorExit 分支:
       「慢到用户跑掉」的请求恰恰最该看;顺手把 close() 传给上游,让它照旧做
       「这一轮不入库」的清理（的语义不许被这层破坏）。
    5. **一次请求只有一条账**。``qa`` 那一层开账,并把**同一个** ``timing`` 交给知识库通道,
       检索/生成的打点记在同一行上（``stream_rag`` 拿到别人的 ``timing`` 就不再自己
       ``emit``、也不再包一层 ``timed``）。**两个各开一条的后果不是"多一行日志"**:
       报表的请求量、拒答率、通道计数会全部翻倍,而翻倍在报表上只表现为"量比想象的大"。
"""

from __future__ import annotations

import ast
import importlib
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from _harness import check, report, record
from _fakes import (
    FakeEmbedder,
    FakeFaqStore,
    FakeKeywordIndex,
    FakeMatcher,
    FakeVectorStore,
    PacedFakeLLM,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

FAQ_QUESTION = "退款流程怎么走？"
FAQ_ANSWER = "在订单页点「申请退款」，1-3 个工作日到账。"
KB_PARENT = "装饰器是一个接收函数并返回新函数的函数。"
KB_PARTS = ("装饰器是", "给函数加行为的语法 [1]")
REFUSAL = "信息不足,无法回答,请联系人工客服。"
LATENCY_LOGGER = "project_rag.observability.latency"

# 日志行的字段与顺序 —— 这张表是契约的一部分,产品改了这里必须同步改
_FIELD_ORDER = (
    "label",
    "request_id",
    "channel",
    "thinking",
    "cold",
    "builds",
    "faq_ms",
    # 查询改写那一段(开了改写才有值)。它与 retrieve_ms 并列:改写之后
    # embed/retrieve 从**改写结束**算起,所以「检索花了多久」不会被一次 LLM 改写污染。
    "rewrite_ms",
    "sources_ms",
    "retrieve_ms",
    "embed_ms",
    "vector_ms",
    "bm25_ms",
    "fuse_ms",
    "collapse_ms",
    "rerank_ms",
    "llm_think_ms",
    "llm_ttft_ms",
    "think_tokens",
    # provider 回报的**计费口径**:模型名 + 输入(含缓存命中)/ 输出 token。
    # 它和上面那两个「帧数」是两回事:帧数给 tps 用,这四个给报表算钱用。
    # **命中缓存那一列不能省**:它的单价与未命中差 50 倍(0.02 vs 1 元/百万)。
    "llm_model",
    "llm_input_tokens",
    "llm_cached_input_tokens",
    "llm_output_tokens",
    "ttft_ms",
    "tokens",
    "gen_ms",
    "tps",
    "total_ms",
    "refused",
    "truncated",
    "aborted",
)

# 渲染成**字符串**而不是数字的字段（其余字段要么是 '-'、要么是 true/false、要么能被 float() 吃掉）
_STRING_FIELDS = ("label", "request_id", "channel", "thinking", "llm_model")


def _require(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(f"{module_name} 缺少公开成员 {missing}")
    return module


def _stream_timing():
    return _require("project_rag.observability", ("StreamTiming",)).StreamTiming


def _timed():
    return _require("project_rag.application.streaming", ("timed",)).timed


def _stream_rag():
    return _require("project_rag.application", ("stream_rag",)).stream_rag


def _answer_rag():
    return _require("project_rag.application", ("answer_rag",)).answer_rag


def _stream_question():
    return _require("project_rag.application", ("stream_question",)).stream_question


def _answer_question():
    return _require("project_rag.application", ("answer_question",)).answer_question


def _qa_event():
    return _require("project_rag.domain", ("QaEvent",)).QaEvent


# ── 抓日志：只挂在 latency 这一个 logger 上（不碰全局配置） ───────────────────
class _Catcher(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []
        self.levels: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        self.levels.append(record.levelno)


@contextmanager
def _capture() -> Iterator[_Catcher]:
    logger = logging.getLogger(LATENCY_LOGGER)
    catcher = _Catcher()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(catcher)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 别把断言用的行漏进 root 的 handler（跑验收时不该刷屏）
    try:
        yield catcher
    finally:
        logger.removeHandler(catcher)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _parse(line: str) -> dict[str, str]:
    """输入:一条 latency 日志行。行为:拆成字段字典。输出:{字段名: 文本值}。"""
    tokens = line.split(" ")
    if tokens[0] != "latency":
        raise AssertionError(f"日志行必须以 'latency ' 开头,实际 {line!r}")
    fields: dict[str, str] = {}
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if not sep:
            raise AssertionError(f"字段 {token!r} 不是 key=value 形状:{line!r}")
        fields[key] = value
    return fields


def _num(fields: dict[str, str], key: str) -> float | None:
    value = fields[key]
    return None if value == "-" else float(value)


def _int(fields: dict[str, str], key: str) -> int:
    return int(fields[key])


def _only(catcher: _Catcher) -> dict[str, str]:
    """输入:抓到的日志。行为:断言恰好一条并解析。输出:字段字典。"""
    if len(catcher.lines) != 1:
        raise AssertionError(f"这一轮应该恰好一条账,实际 {len(catcher.lines)} 条:{catcher.lines}")
    return _parse(catcher.lines[0])


# ── 假件（带可控延迟，用来验证「真的在量时间」） ──────────────────────────────


def _kb_hit(source: str = "bagu_ai"):
    from project_rag.domain import Chunk, RetrievedChunk

    return RetrievedChunk(
        chunk=Chunk("c1", "装饰器 子块", source, "d1", "p1", KB_PARENT), score=0.91
    )


def _faq_entry():
    return _require("project_rag.domain", ("FaqEntry",)).FaqEntry(
        id=7, question=FAQ_QUESTION, answer=FAQ_ANSWER
    )


def _rag_stream(llm, *, hits=None, store=None, options=None, thinking=None, timing=None):
    """输入:假 LLM + 可选的假向量库/旋钮/档位/账本。行为:调一次 stream_rag。输出:帧流。

    ``timing`` 由合流入口交下来时**原样转交** —— 组合根就是这么做的
    (``knowledge_callable``),少了这一步一次请求会落两行账。
    """
    from project_rag.domain import RetrievalOptions

    return _stream_rag()(
        "装饰器有什么用",
        embedder=FakeEmbedder(),
        store=store or FakeVectorStore([_kb_hit()] if hits is None else hits),
        llm=llm,
        keyword_index=None,
        options=options or RetrievalOptions(k=3, max_context=2),
        thinking=thinking,
        timing=timing,
    )


def _question_stream(
    *, faq_score=None, knowledge=None, faq_delay=0.0, session=None, thinking=None
):
    return _stream_question()(
        "装饰器有什么用",
        faq_store=FakeFaqStore([_faq_entry()], delay=faq_delay),
        matcher=FakeMatcher(faq_score, delay=faq_delay),
        knowledge=knowledge or (lambda history: iter(())),
        session=session,
        thinking=thinking,
    )


def _drain(events) -> list:
    return list(events)


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


# ── 1. 形状与分层 ─────────────────────────────────────────────────────────────
@check("形状：StreamTiming 从 observability 包入口拿得到，是 slots dataclass，五个动作齐全")
def _() -> str:
    import dataclasses

    # 传**表达式**给 is_dataclass,不传变量:它是 TypeGuard,会把变量收窄成
    # DataclassInstance,后面 cls(label=...) / cls.finish 就全报「对象不可调用」(pyright 的已知行为)
    if not dataclasses.is_dataclass(_stream_timing()):
        raise AssertionError("StreamTiming 应是 dataclass")
    cls = _stream_timing()
    if "__slots__" not in vars(cls):
        raise AssertionError("StreamTiming 应声明 slots（它每请求一个实例，别给每条流都建 __dict__）")
    for name in ("mark", "token", "finish", "line", "emit"):
        if not callable(getattr(cls, name, None)):
            raise AssertionError(f"StreamTiming 缺少 {name}()")
    timing = cls(label="probe")
    if timing.aborted is not True or timing.tokens != 0:
        raise AssertionError("新账本应从「零 token、未收尾」开始")
    timing.finish(channel="rag", refused=False, truncated=False)
    if timing.aborted is not False or timing.channel != "rag":
        raise AssertionError("finish() 之后应记下结局与通道")
    return "dataclass + slots + mark/token/finish/line/emit"


@check("分层：latency.py 只 import 标准库 + 同包 logging_setup（不许碰 domain/adapters）")
def _() -> str:
    path = SRC / "observability" / "latency.py"
    # `uuid` 是 加的:账本自己生成 `request_id`(不靠调用方记得传)。
    # 它仍是**标准库**,不违反这条检查的本意(「账本不认识领域类型、不许碰 domain/adapters」);
    # 白名单是逐项过审的,所以这里显式加一项而不是放宽成"标准库都行"。
    allowed = {"__future__", "time", "dataclasses", "uuid"}
    pairs: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            pairs.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 同包相对导入（.logging_setup）
                continue
            pairs.append(node.module or "")
    bad = [name for name in pairs if name.split(".")[0] not in allowed]
    if bad:
        raise AssertionError(f"latency.py 不该 import 这些：{bad}（账本是纯计时的，不认识领域类型）")
    return f"只依赖 {sorted(allowed)} + 同包"


# ── 2. 口径（三条定义各钉一条） ───────────────────────────────────────────────
@check("口径 TTFT：空 token 帧不算第一个字（provider 的收尾空帧不许把 TTFT 提前）")
def _() -> str:
    timing = _stream_timing()("probe")
    timing.token("")
    timing.token("")
    if timing.first_token is not None or timing.ttft_ms is not None:
        raise AssertionError("空帧不该定下 TTFT")
    time.sleep(0.02)
    timing.token("甲")
    ttft = timing.ttft_ms
    if ttft is None or ttft < 15:
        raise AssertionError(f"非空帧才该定 TTFT，实际 {ttft}")
    if timing.tokens != 1:
        raise AssertionError(f"空帧不该计入 tokens，实际 {timing.tokens}")
    return f"3 帧里只有 1 帧非空 → tokens=1、ttft={ttft:.1f}ms"


@check("口径 tps：单帧不给速率（），两帧才有；gen_ms 不足两帧也不给 0")
def _() -> str:
    timing = _stream_timing()("probe")
    timing.token("甲")
    if timing.tps is not None or timing.gen_ms is not None:
        raise AssertionError(
            f"单帧算不出速率与窗口，应该都给 None（不许编 0），实际 {timing.tps} / {timing.gen_ms}"
        )
    time.sleep(0.02)
    timing.token("乙")
    tps, gen = timing.tps, timing.gen_ms
    if gen is None or gen < 15 or tps is None:
        raise AssertionError(f"两帧该有窗口与速率，实际 gen={gen} tps={tps}")
    if not 40 <= tps <= 4000:
        raise AssertionError(f"两帧间隔 ~20ms 时速率应落在几十~上千，实际 {tps}")
    return f"单帧 -；两帧 gen={gen:.1f}ms tps={tps:.1f}"


@check("口径 缺失：没量到的字段一律 '-'，但 tokens=0 / total_ms 是有值的（0 与没量到要分得开）")
def _() -> str:
    timing = _stream_timing()("probe")
    fields = _parse(f"latency {timing.line()}")
    for key in (
        "faq_ms",
        "sources_ms",
        "retrieve_ms",
        "llm_think_ms",
        "llm_ttft_ms",
        "ttft_ms",
        "gen_ms",
        "tps",
    ):
        if fields[key] != "-":
            raise AssertionError(f"没打过点/没有 token，{key} 应是 '-'，实际 {fields[key]!r}")
    if fields["tokens"] != "0" or fields["think_tokens"] != "0":
        raise AssertionError(
            f"tokens / think_tokens 该老实报 0，实际 {fields['tokens']!r} / {fields['think_tokens']!r}"
        )
    if _num(fields, "total_ms") is None:
        raise AssertionError("total_ms 永远量得到，不该是 '-'")
    if fields["aborted"] != "true":
        raise AssertionError("没收到 done 帧就该是 aborted=true")
    return "缺失字段用 '-'，tokens=0 / think_tokens=0 照实报"


# ── 2b. 思考口径 ─────────────────────────────────────────────────────
@check("口径 思考：空思考帧不算一帧；think_tokens 恒为整数（0 = 这一轮没看到思考，不是 '-'）")
def _() -> str:
    timing = _stream_timing()("probe")
    timing.think("")
    timing.think("")
    if timing.think_tokens != 0:
        raise AssertionError(f"空思考帧不该计数，实际 {timing.think_tokens}")
    time.sleep(0.02)
    timing.think("先想想")
    if timing.think_tokens != 1:
        raise AssertionError(f"非空思考帧才该计数，实际 {timing.think_tokens}")
    fields = _parse(f"latency {timing.line()}")
    if fields["think_tokens"] != "1":
        raise AssertionError(f"think_tokens 应渲染成整数，实际 {fields['think_tokens']!r}")
    return "空思考帧不计；think_tokens=1"


@check("口径 思考窗口：llm_think_ms 从 llm_start 到**最后一帧**思考；没思考帧给 '-'（不编 0）")
def _() -> str:
    timing = _stream_timing()("probe")
    timing.think("想了但还没 llm_start")
    if timing.llm_think_ms is not None:
        raise AssertionError(
            "没打 llm_start 就不该有思考窗口（原点都不知道在哪），"
            f"实际 {timing.llm_think_ms}"
        )
    timing = _stream_timing()("probe")
    timing.mark("llm_start")
    time.sleep(0.02)
    timing.think("第一帧")
    first = timing.llm_think_ms
    time.sleep(0.02)
    timing.think("第二帧")
    second = timing.llm_think_ms
    if first is None or first < 15:
        raise AssertionError(f"第一帧思考就该量到窗口（~20ms），实际 {first}")
    if second is None or second < first + 10:
        raise AssertionError(f"思考窗口该被**最后一帧**推后:{first} -> {second}")
    return f"最后一帧定窗口:{first:.1f}ms → {second:.1f}ms"


@check("口径 思考 没想：调了模型但一帧思考都没有 → llm_think_ms='-'、think_tokens=0（可与「没调模型」区分）")
def _() -> str:
    timing = _stream_timing()("probe")
    timing.mark("llm_start")
    timing.token("答")
    fields = _parse(f"latency {timing.line()}")
    if fields["llm_think_ms"] != "-" or fields["think_tokens"] != "0":
        raise AssertionError(f"没思考该是 '-' + 0，实际 {fields['llm_think_ms']!r} / {fields['think_tokens']!r}")
    if _num(fields, "llm_ttft_ms") is None:
        raise AssertionError("调过模型（有 llm_start）就该有 llm_ttft_ms —— 这才是「没调模型」与「没想」的分界")
    return "调了但没想:llm_think_ms='-' think_tokens=0 llm_ttft_ms 有值"


@check("口径 思考 ≤ 首字：llm_think_ms 与 llm_ttft_ms 同原点，所以前者不许大于后者")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS, think_pieces=("想", "想", "想"), first_delay=0.01, think_gap=0.01)
    with _capture() as catcher:
        _drain(_rag_stream(llm))
    fields = _only(catcher)
    think, ttft = _num(fields, "llm_think_ms"), _num(fields, "llm_ttft_ms")
    if think is None or ttft is None:
        raise AssertionError(f"这一轮既有思考又有正文，两个窗口都该有值，实际 {think} / {ttft}")
    if think > ttft:
        raise AssertionError(f"思考窗口不该比首字延迟还长（同原点）:{think} > {ttft}")
    return f"think={think:.1f} ≤ llm_ttft={ttft:.1f}"


@check("档位：账本按传入的 thinking 原样渲染；**没传给账本**就给 '-'（不替用户编一个 auto）")
def _() -> str:
    cls = _stream_timing()
    for level in ("auto", "off", "low", "high", "max"):
        fields = _parse(f"latency {cls('probe', thinking=level).line()}")
        if fields["thinking"] != level:
            raise AssertionError(f"档位该原样渲染 {level!r}，实际 {fields['thinking']!r}")
    fields = _parse(f"latency {cls('probe').line()}")
    if fields["thinking"] != "-":
        raise AssertionError(
            f"没传档位时账本不知道配置，必须给 '-'（编一个 auto 就是把猜的写成了量的），实际 {fields['thinking']!r}"
        )
    return "五个档位原样渲染；没传 → '-'"


# ── 3. 真在量时间（量级检查） ─────────────────────────────────────────────────
@check("真在量：给定的检索延迟/首字延迟/吐字间隔，都要出现在对应字段里（量级对得上）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS, first_delay=0.05, gap=0.01)
    with _capture() as catcher:
        _drain(_rag_stream(llm, store=FakeVectorStore([_kb_hit()], delay=0.03)))
    fields = _only(catcher)
    retrieve, llm_ttft, ttft = (
        _num(fields, "retrieve_ms"),
        _num(fields, "llm_ttft_ms"),
        _num(fields, "ttft_ms"),
    )
    if retrieve is None or not 20 <= retrieve <= 2000:
        raise AssertionError(f"检索延迟给了 30ms，retrieve_ms 应量到 ~30，实际 {retrieve}")
    if llm_ttft is None or not 40 <= llm_ttft <= 3000:
        raise AssertionError(f"首字延迟给了 50ms，llm_ttft_ms 应量到 ~60，实际 {llm_ttft}")
    if ttft is None or ttft < retrieve + llm_ttft - 5:
        raise AssertionError(f"ttft_ms 应 ≥ 检索 + 首字，实际 ttft={ttft} retrieve={retrieve} llm={llm_ttft}")
    if _int(fields, "tokens") != len(KB_PARTS):
        raise AssertionError(f"tokens 应等于非空 token 帧数 {len(KB_PARTS)}，实际 {fields['tokens']}")
    tps = _num(fields, "tps")
    if tps is None or not 20 <= tps <= 10000:
        raise AssertionError(f"两帧间隔 10ms 时 tps 应在几十~上千，实际 {tps}")
    return f"retrieve={retrieve:.1f} llm_ttft={llm_ttft:.1f} ttft={ttft:.1f} tps={tps:.1f}"


@check("真在量（分段）：stream_rag 的账带 label/channel，且 sources_ms ≈ 检索那一段")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS, first_delay=0.01)
    with _capture() as catcher:
        _drain(_rag_stream(llm, store=FakeVectorStore([_kb_hit()], delay=0.02)))
    fields = _only(catcher)
    if fields["label"] != "rag" or fields["channel"] != "rag":
        raise AssertionError(f"知识库那一段应是 label=rag channel=rag，实际 {fields}")
    sources = _num(fields, "sources_ms")
    if sources is None or sources < 15:
        raise AssertionError(f"sources_ms 应量到检索那 20ms，实际 {sources}")
    return f"label=rag sources={sources:.1f}ms"


# ── 4. timed 的三条纪律：不改写 / 不预取 / 中断留账 ───────────────────────────
@check("纪律 不改写：包装前后的帧完全一致（内容、顺序、条数一个字节都不动）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS)
    with _capture():
        wrapped = _drain(_rag_stream(llm))
    kinds = [event.kind for event in wrapped]
    if kinds != ["sources", "token", "token", "done"]:
        raise AssertionError(f"帧序列应原样透传，实际 {kinds}")
    texts = [event.text for event in wrapped if event.kind == "token"]
    if texts != list(KB_PARTS):
        raise AssertionError(f"token 帧内容应原样，实际 {texts}")
    done = wrapped[-1]
    # 注意 channel 是 None:补 channel 是合流层(stream_question)的活,rag 这一层不知道
    # 自己是「主路」还是「兜底」—— 那条账里的 channel=rag 来自账本初值,不是帧
    if done.answer != "".join(KB_PARTS) or done.channel is not None:
        raise AssertionError(f"收尾帧应原样，实际 answer={done.answer!r} channel={done.channel!r}")

    qa = _drain(
        _question_stream(
            knowledge=lambda history, timing: iter(
                [
                    _qa_event()(kind="sources", citations=()),
                    _qa_event()(kind="token", text="甲"),
                    _qa_event()(kind="done", answer="甲", channel=None),
                ]
            )
        )
    )
    got = [(event.kind, event.text, event.answer, event.channel) for event in qa]
    want = [
        ("sources", "", "", None),
        ("token", "甲", "", None),
        ("done", "", "甲", "rag"),  # done 由用例层补 channel，其余原样
    ]
    if got != want:
        raise AssertionError(f"合流那层应只补 channel，实际 {got}")
    return "帧序列逐帧一致"


@check("纪律 不预取：取走第一帧时上游只产出了一帧（写成 list() 就等于先算完再交）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS)
    with _capture():
        stream = _rag_stream(llm)
        first = next(stream)
        if first.kind != "sources":
            raise AssertionError(f"第一帧应是 sources，实际 {first.kind}")
        if llm.calls or llm.produced:
            raise AssertionError(
                f"拿到 sources 时还没检索完/没调模型，实际 calls={llm.calls} produced={llm.produced}"
            )
        second = next(stream)
        if second.kind != "token" or llm.produced != 1:
            raise AssertionError(
                f"取到第一帧 token 时上游只该产出 1 帧，实际 produced={llm.produced}"
            )
        stream.close()
    return "逐帧透传，没有预取"


@check("纪律 中断留账：close() 之后也有一条账，aborted=true，tokens 是已经出来的数")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS)
    with _capture() as catcher:
        stream = _rag_stream(llm)
        next(stream)  # sources
        next(stream)  # 第一帧 token
        stream.close()
    fields = _only(catcher)
    if fields["aborted"] != "true":
        raise AssertionError(f"中途断开应记 aborted=true，实际 {fields}")
    if _int(fields, "tokens") != 1:
        raise AssertionError(f"断开前只出了 1 帧 token，实际 {fields['tokens']}")
    if _num(fields, "ttft_ms") is None:
        raise AssertionError("断开前已经看到第一个字，ttft_ms 不该是 '-'")
    return "中断也有账:aborted=true，tokens=1"


@check("纪律 不吞异常：上游抛错时账照记（aborted=true），异常原样上抛")
def _() -> str:
    def boom(history, timing):
        yield _qa_event()(kind="token", text="半句")
        raise RuntimeError("上游炸了")

    with _capture() as catcher:
        try:
            _drain(_question_stream(knowledge=boom))
        except RuntimeError as exc:
            if "上游炸了" not in str(exc):
                raise AssertionError(f"异常应原样上抛，实际 {exc!r}") from exc
        else:
            raise AssertionError("上游抛的异常被吞了 —— 那会让「答了一半」看起来像「答完了」")
    fields = _only(catcher)
    if fields["aborted"] != "true" or _int(fields, "tokens") != 1:
        raise AssertionError(f"出错也该留半截账，实际 {fields}")
    return "异常上抛 + 半截账"


# ── 5. 各条链路的账 ───────────────────────────────────────────────────────────
@check("检索分段：五段分别量到，且和 ≥ retrieve_ms；没装重排器时 rerank_ms 是 '-'（不是 0.0）")
def _() -> str:
    with _capture() as catcher:
        _drain(_rag_stream(PacedFakeLLM(KB_PARTS, first_delay=0.01)))
    fields = _only(catcher)
    if fields["rerank_ms"] != "-":
        raise AssertionError(
            "没装重排器时 rerank_ms 该是 '-'（打个 0.0 会让人以为「重排很快」），"
            f"实际 {fields['rerank_ms']!r}"
        )
    legs = ("embed_ms", "vector_ms", "bm25_ms", "fuse_ms", "collapse_ms")
    values = {name: _num(fields, name) for name in legs}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise AssertionError(f"这几个分段字段没量到：{missing}")
    legs_sum = sum(value for value in values.values() if value is not None)
    total = _num(fields, "retrieve_ms")
    # 这一段的**关键字那一路是关着的**,所以两路没有重叠,和就该≈墙钟(差在返回与截断那一下)。
    # 两路都在跑的时候和会**大于**墙钟(重叠) —— 那条恒等式由下一条检查单独钉
    # (「两路召回并行」),这里只管"有没有哪一段没量到、有没有哪段把别人的时间算进来"。
    if total is None or not -2 <= total - legs_sum < 2:
        raise AssertionError(
            f"分段之和应 ≈ retrieve_ms（这一段没有并行,所以是≈而不是≤）,"
            f"实际 段和={legs_sum} retrieve={total}"
        )
    return f"五段和={legs_sum:.1f}ms ≈ retrieve={total:.1f}ms；没装重排 → rerank_ms='-'"


@check("两路召回并行：墙钟 ≈ max(两路) 而不是相加，融合段不吸「等另一路」的时间")
def _() -> str:
    """两路召回并行化之后的契约。两个场景各钉一件事：

    1. **两路一样慢**(各 0.4s)—— 并行 → 墙钟 ≈ 0.4s,串行 → 0.8s。判据取
       ``墙钟 < 0.7 × (两段之和)``,两侧各留 150ms 余量,不受机器负载影响;
    2. **一路快一路慢**(0.4s / 0.1s)—— ``bm25_ms`` 必须是**它自己的** 0.1s(不是 0、
       不是负数、更不是"从上一个打点到它"的 0.3s),而 ``fuse_ms`` 必须**不**把
       "等稠密那一路"的 0.3s 吸进来。这一条盯的是稀疏那一路先结束时最容易犯的错:
       融合段的起点取成了"先结束的那一路",于是账上写着"融合 300ms",而融合其实什么
       都没干 —— 下一个人会去优化一个不存在的问题。
    """
    from project_rag.domain import RetrievalOptions

    retrieve = _require("project_rag.application", ("retrieve",)).retrieve

    def run(vector_delay: float, keyword_delay: float):
        timing = _stream_timing()("probe")
        started = time.perf_counter()
        hits = retrieve(
            "装饰器有什么用",
            embedder=FakeEmbedder(),
            store=FakeVectorStore([_kb_hit()], delay=vector_delay),
            keyword_index=FakeKeywordIndex([_kb_hit("bagu_kb")], delay=keyword_delay),
            options=RetrievalOptions(k=3, max_context=2),
            timing=timing,
        )
        return timing.values(), (time.perf_counter() - started) * 1000.0, len(hits)

    fields, wall, found = run(0.4, 0.4)
    vector, keyword = _num(fields, "vector_ms"), _num(fields, "bm25_ms")
    if vector is None or keyword is None:
        raise AssertionError(f"两路都跑了,两格都该有值,实际 vector={vector} bm25={keyword}")
    if not 300 <= vector <= 900 or not 300 <= keyword <= 900:
        raise AssertionError(f"两段该各≈400ms（假端口各睡 0.4s），实际 {vector} / {keyword}")
    if wall > 0.7 * (vector + keyword):
        raise AssertionError(
            f"两路该并行:墙钟 {wall:.1f}ms 应 < 0.7×(两段之和 {vector + keyword:.1f}ms)≈"
            f"{(vector + keyword) * 0.7:.1f}ms —— 墙钟≈两段之和说明它们又串起来了"
        )
    if not found:
        raise AssertionError("并行召回把命中弄丢了(引用数 0)")

    fields, wall, _ = run(0.4, 0.1)
    vector, keyword = _num(fields, "vector_ms"), _num(fields, "bm25_ms")
    fuse = _num(fields, "fuse_ms")
    if keyword is None or not 60 <= keyword <= 400:
        raise AssertionError(
            f"bm25_ms 该报**关键字那一路自己**的 ≈100ms（假端口睡 0.1s），实际 {keyword} "
            "—— 负数或 300ms 量级的都是「用相邻打点之差」算出来的"
        )
    if fuse is None or fuse > 100:
        raise AssertionError(
            f"融合段该只有它自己的工作(≈0ms),实际 {fuse}ms —— 说明它的起点取成了"
            "「先结束的那一路」,把等另一路的 300ms 吸进来了"
        )
    return (
        f"两路各 0.4s → 墙钟 {wall:.0f}ms < 和 0.8s；0.4/0.1 → bm25={keyword:.0f}ms 且 fuse={fuse:.1f}ms"
    )


@check("只开关键字那一路：bm25_ms 报的是它自己的时长（并行之前这一格是 '-'，量到了却说没量到）")
def _() -> str:
    """口径修正的钉子：``vector_weight=0`` 时稠密那一路压根不跑,于是"相邻两打点之差"
    缺了起点,``bm25_ms`` 一直是 ``-`` —— 而那一路明明跑了、也明明量到了时间。
    改成各路自报之后它该有值,同时 ``vector_ms`` 仍是 ``-``（那一路确实没跑）。"""
    from project_rag.domain import RetrievalOptions

    retrieve = _require("project_rag.application", ("retrieve",)).retrieve
    timing = _stream_timing()("probe")
    retrieve(
        "装饰器有什么用",
        embedder=FakeEmbedder(),
        store=FakeVectorStore([_kb_hit()]),
        keyword_index=FakeKeywordIndex([_kb_hit("bagu_kb")], delay=0.1),
        options=RetrievalOptions(k=3, max_context=2, vector_weight=0.0),
        timing=timing,
    )
    fields = timing.values()
    keyword, vector = _num(fields, "bm25_ms"), fields["vector_ms"]
    if keyword is None or not 60 <= keyword <= 400:
        raise AssertionError(
            f"只跑关键字那一路时 bm25_ms 该是 ≈100ms 的真读数，实际 {fields['bm25_ms']!r}"
            "—— 那一格是 '-' 的话,「关键字那一路慢不慢」就永远查不出来"
        )
    if vector != "-":
        raise AssertionError(f"稠密那一路没跑,vector_ms 该是 '-'（不是 0.0），实际 {vector!r}")
    return f"只开关键字 → bm25_ms={keyword:.0f}ms、vector_ms='-'"


@check("链路 拒答：一条 rag 账，refused=true、tokens=1、tps='-'（没调模型就不该有首字/速率）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS)
    with _capture() as catcher:
        _drain(_rag_stream(llm, hits=[]))
    fields = _only(catcher)
    if fields["refused"] != "true" or fields["aborted"] != "false":
        raise AssertionError(f"拒答应 refused=true 且正常收尾，实际 {fields}")
    if _int(fields, "tokens") != 1 or fields["tps"] != "-":
        raise AssertionError(f"拒答只有一帧话术，tokens=1 且 tps='-'，实际 {fields}")
    if llm.calls:
        raise AssertionError("检索为空不该调 LLM")
    return "拒答也有账:refused=true tokens=1 tps=-"


@check("链路 FAQ 命中：一条 qa 账，channel=faq、faq_ms 有值、ttft≈faq_ms、knowledge 零调用")
def _() -> str:
    seen: list[int] = []

    def knowledge(history, timing):
        seen.append(1)
        return iter(())

    with _capture() as catcher:
        _drain(_question_stream(faq_score=0.97, knowledge=knowledge, faq_delay=0.03))
    fields = _only(catcher)
    if fields["label"] != "qa" or fields["channel"] != "faq":
        raise AssertionError(f"应是 label=qa channel=faq，实际 {fields}")
    faq, ttft = _num(fields, "faq_ms"), _num(fields, "ttft_ms")
    if faq is None or faq < 20:
        raise AssertionError(f"FAQ 匹配那一段给了 30ms，faq_ms 应量到，实际 {faq}")
    if ttft is None or abs(ttft - faq) > 30:
        raise AssertionError(f"命中 FAQ 时 ttft 应≈faq_ms，实际 ttft={ttft} faq={faq}")
    if seen:
        raise AssertionError("命中 FAQ 时 knowledge 一次都不该被调")
    if fields["retrieve_ms"] != "-" or fields["sources_ms"] != "-":
        raise AssertionError(f"没走检索就不该有检索字段，实际 {fields}")
    return f"channel=faq faq_ms={faq:.1f} ttft={ttft:.1f}"


@check("链路 合流走知识库：**一条**账（label=qa），检索/首字的分段都落在同一行上")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS, first_delay=0.01)
    with _capture() as catcher:
        events = _drain(_question_stream(knowledge=lambda history, timing: _rag_stream(llm, timing=timing)))
    if len(catcher.lines) != 1:
        raise AssertionError(
            f"一次请求只能有一条账（多了就是请求量翻倍），实际 {len(catcher.lines)} 条：{catcher.lines}"
        )
    fields = _parse(catcher.lines[0])
    if fields["label"] != "qa" or fields["channel"] != "rag":
        raise AssertionError(f"应是 label=qa channel=rag，实际 {fields['label']} / {fields['channel']}")
    # 内层的打点必须真的落在这一行上 —— 「合成一条」最坏的做法是外层只留个总时长,
    # 那样报表还在,只是"检索花了多久"永远查不出来
    for name in ("faq_ms", "retrieve_ms", "sources_ms", "llm_ttft_ms"):
        if _num(fields, name) is None:
            raise AssertionError(f"合流走知识库时 {name} 该有值（它记在同一行上），实际 {fields[name]}")
    frames = sum(1 for event in events if event.kind == "token" and event.text)
    if _int(fields, "tokens") != frames:
        raise AssertionError(
            f"tokens 该等于正文帧数 {frames}（多包一层 timed 会数两遍），实际 {fields['tokens']}"
        )
    return f"一条 qa 账：faq→检索→首字的分段齐全，tokens={frames}"


@check("链路 帧数只数一遍：合流那一行的 tokens 等于正文帧数（同一个账本被观察两次就翻倍）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS, first_delay=0.01)
    with _capture() as catcher:
        events = _drain(
            _question_stream(knowledge=lambda history, timing: _rag_stream(llm, timing=timing))
        )
    fields = _only(catcher)
    frames = sum(1 for event in events if event.kind == "token" and event.text)
    if _int(fields, "tokens") != frames:
        raise AssertionError(
            f"tokens 该等于正文帧数 {frames}，实际 {fields['tokens']} —— 同一个 timing 被包了两层 timed 就会数两遍"
        )
    return f"一个正文帧记一个 token（{frames}）"


@check("链路 非流式入口：answer_question 收拢后照样有账（收拢器不改计时语义）")
def _() -> str:
    llm = PacedFakeLLM(KB_PARTS)
    with _capture() as catcher:
        result = _answer_question()(
            "装饰器有什么用",
            faq_store=FakeFaqStore([_faq_entry()]),
            matcher=FakeMatcher(None),
            knowledge=lambda history, timing: _rag_stream(llm, timing=timing),
            faq_threshold=0.85,
        )
    if result.answer != "".join(KB_PARTS):
        raise AssertionError(f"答案不对，实际 {result.answer!r}")
    if len(catcher.lines) != 1:
        raise AssertionError(f"非流式也只该有一条账，实际 {[l.split()[0] for l in catcher.lines]}")
    fields = _parse(catcher.lines[0])
    if fields["label"] != "qa" or fields["aborted"] != "false" or _int(fields, "tokens") != len(KB_PARTS):
        raise AssertionError(f"收拢完应是 label=qa + 正常收尾 + 数到全部 token，实际 {fields}")
    return "answer_question 也只有一条账"


@check("链路 思考入账：思考帧进 think_tokens / llm_think_ms，**一个字都不许混进答案与 tokens**")
def _() -> str:
    think = ("9.11 与 9.8 比大小要先对齐小数位,", "对齐后 9.11 < 9.80,")
    llm = PacedFakeLLM(KB_PARTS, think_pieces=think, first_delay=0.01, think_gap=0.01, gap=0.01)
    with _capture() as catcher:
        events = _drain(_rag_stream(llm))
    fields = _only(catcher)
    if _int(fields, "think_tokens") != len(think):
        raise AssertionError(f"有 {len(think)} 帧思考，think_tokens 应数得一样，实际 {fields['think_tokens']}")
    # 先查最坏的那种:思考有没有混进用户看到的东西(反向测试 RT-1 就是靠这条抓的 ——
    # 它要是排在后面,先炸的是别的断言,报告出来的「罪名」就不是真的那一条)
    answer = "".join(event.text for event in events if event.kind == "token")
    if answer != "".join(KB_PARTS):
        raise AssertionError(f"思考混进了答案 —— 这是最坏的一种:用户会看到模型的草稿。实际 {answer!r}")
    if any(part in answer for part in think):
        raise AssertionError(f"答案里出现了思考原文:{answer!r}")
    if _int(fields, "tokens") != len(KB_PARTS):
        raise AssertionError(
            f"tokens 只该数**正文**帧 {len(KB_PARTS)} 个，把思考算进来就是口径串了，实际 {fields['tokens']}"
        )
    think_ms, ttft_ms = _num(fields, "llm_think_ms"), _num(fields, "llm_ttft_ms")
    if think_ms is None or not 20 <= think_ms <= 3000:
        raise AssertionError(f"思考间隔给了 2×10ms（+首字延迟），llm_think_ms 应量到，实际 {think_ms}")
    if ttft_ms is None:
        raise AssertionError("既有思考又有正文，llm_ttft_ms 该有值")
    # 两个口径同原点，所以差值就是「想完 → 吐出第一个正文帧」之间那一个 gap（10ms）
    gap = ttft_ms - think_ms
    if not 5 <= gap <= 200:
        raise AssertionError(
            f"llm_ttft_ms - llm_think_ms 应≈正文帧前的 10ms gap（同原点才敢相减），实际 {gap}（{ttft_ms} - {think_ms}）"
        )
    return f"think_tokens={len(think)} llm_think_ms={think_ms:.1f} → +{gap:.1f}ms → 首字；答案干净"


@check("档位 传递：stream_rag / answer_rag / stream_question 的 thinking= 落到账里（账本不认识配置）")
def _() -> str:
    from project_rag.domain import RetrievalOptions

    rag_kwargs = dict(
        embedder=FakeEmbedder(),
        store=FakeVectorStore([_kb_hit()]),
        llm=PacedFakeLLM(KB_PARTS),
        options=RetrievalOptions(k=3, max_context=2),
    )
    with _capture() as catcher:
        _drain(_rag_stream(PacedFakeLLM(KB_PARTS), thinking="low"))
    streamed = _only(catcher)["thinking"]
    if streamed != "low":
        raise AssertionError(f"stream_rag(thinking='low') 该写进账里，实际 {streamed!r}")

    with _capture() as catcher:
        _answer_rag()("装饰器有什么用", **rag_kwargs, thinking="high")
    collapsed = _only(catcher)["thinking"]
    if collapsed != "high":
        raise AssertionError(f"answer_rag(thinking='high') 该写进账里（它是 stream_rag 的收拢器），实际 {collapsed!r}")

    with _capture() as catcher:
        _drain(_question_stream(thinking="max", knowledge=lambda history, timing: _rag_stream(PacedFakeLLM(KB_PARTS), timing=timing)))
    outer = _parse(catcher.lines[-1])
    if outer["label"] != "qa" or outer["thinking"] != "max":
        raise AssertionError(f"合流外层也要带上档位（FAQ 命中时账里只有这一条），实际 {outer}")

    # 没传 = 账本不知道配置 → '-':注入式调用（测试/直调）不会被编一个假档位
    with _capture() as catcher:
        _drain(_rag_stream(PacedFakeLLM(KB_PARTS)))
    if _only(catcher)["thinking"] != "-":
        raise AssertionError("没传档位时该是 '-'，不是编出来的默认值")
    return "stream_rag/answer_rag/stream_question 都携带档位；没传 → '-'"


# ── 6. 行格式（契约的机械部分） ───────────────────────────────────────────────
@check("用量：provider 回报的 token 落进账本；没回报时是 '-' 而不是 0（两件事不许混）")
def _() -> str:
    """消耗报表算钱靠的就是这三个字段,所以"没量到"必须是 ``-``。

    三种情形各断言一次:

    1. **报了** —— 模型名与两个数原样进账(账本不换算、不加工);
    2. **没报** —— 三个字段都是 ``-``。折成 0 会被报表读成"这次不要钱",而真相是"不知道";
    3. **只报了模型名**(provider 说了是哪一次调用、没给 token 数) —— 模型名照记,
       两个 token 仍是 ``-``;这一档在消耗报表里算「调了但没量到」。
    """
    timing = _stream_timing()("probe")
    timing.usage(
        model="deepseek-flash", input_tokens=120, output_tokens=34, cached_input_tokens=100
    )
    values = timing.values()
    if (
        values["llm_model"],
        values["llm_input_tokens"],
        values["llm_cached_input_tokens"],
        values["llm_output_tokens"],
    ) != ("deepseek-flash", "120", "100", "34"):
        raise AssertionError(f"用量的四个字段没照实进账：{values}")

    silent = _stream_timing()("probe").values()
    if (
        silent["llm_model"],
        silent["llm_input_tokens"],
        silent["llm_cached_input_tokens"],
        silent["llm_output_tokens"],
    ) != ("-", "-", "-", "-"):
        raise AssertionError(
            f"没回报用量时四个字段都该是 '-'（折成 0 会让报表把它读成『不要钱』）：{silent}"
        )

    named = _stream_timing()("probe")
    named.usage(model="deepseek-flash")
    partial = named.values()
    if (partial["llm_model"], partial["llm_input_tokens"]) != ("deepseek-flash", "-"):
        raise AssertionError(f"只报模型名时不该编出 token 数：{partial}")

    # 真链路:用量挂在收尾帧上 → 账本上必须有数(适配器与用例层共用这一个约定)
    with _capture() as catcher:
        _drain(_rag_stream(PacedFakeLLM(KB_PARTS, usage=("deepseek-flash", 88, 12))))
    fields = _parse(catcher.lines[-1])
    if (fields["llm_input_tokens"], fields["llm_output_tokens"]) != ("88", "12"):
        raise AssertionError(f"真链路的用量没落账：{fields}")
    if fields["llm_cached_input_tokens"] != "-":
        raise AssertionError(
            f"这一轮没回报缓存明细,那一列必须是 '-'（不是 0 —— 0 会被按命中价计价）：{fields}"
        )
    return "报了照记 / 没报是 '-' / 只报模型名不编数；链路 88+12 落账"


@check("格式：字段与顺序固定 30 个，值要么是 '-' 要么可解析（awk 能按列取）")
def _() -> str:
    timing = _stream_timing()("probe", thinking="off")
    timing.mark("faq")
    timing.token("甲")
    timing.think("想")
    timing.finish(channel=None, refused=True, truncated=True)
    line = timing.line()
    names = [token.partition("=")[0] for token in line.split(" ")]
    if tuple(names) != _FIELD_ORDER:
        raise AssertionError(f"字段或顺序变了：\n  期望 {_FIELD_ORDER}\n  实际 {tuple(names)}")
    fields = _parse(f"latency {line}")
    for key, value in fields.items():
        if value == "-" or key in _STRING_FIELDS:
            continue
        if value in ("true", "false"):
            continue
        float(value)  # 不是数字就抛 ValueError，由 @check 记成 FAIL
    if fields["refused"] != "true" or fields["truncated"] != "true":
        raise AssertionError(f"布尔字段应照实，实际 {fields}")
    if fields["channel"] != "-":
        raise AssertionError("finish 没给 channel 时应保持 '-'（别编一个）")
    return f"30 个字段顺序固定；{len(fields)} 个值全部可解析"


@check("格式 级别：账走 INFO，且这个 logger 不在 NOISY_LOGGERS 里（否则被抬到 WARNING 就看不见）")
def _() -> str:
    noisy = _require("project_rag.observability.logging_setup", ("NOISY_LOGGERS",)).NOISY_LOGGERS
    if any(LATENCY_LOGGER.startswith(name) for name in noisy):
        raise AssertionError(f"{LATENCY_LOGGER} 被降噪规则抬到了 WARNING，INFO 的账会被丢掉")
    with _capture() as catcher:
        _drain(_rag_stream(PacedFakeLLM(KB_PARTS)))
    if catcher.levels != [logging.INFO]:
        raise AssertionError(f"账应是 INFO，实际 {catcher.levels}")
    return "INFO + 不在降噪名单"


def main() -> int:
    return report("延迟观测（TTFT / tokens per second）验收")


if __name__ == "__main__":
    sys.exit(main())

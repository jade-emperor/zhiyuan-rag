#!/usr/bin/env python
"""验收：让 LLM 的结束原因可见（截断不再被吞）—— 流式入口的前置。

用法：

    uv run python scripts/acceptance/check_m5_stream.py

本脚本是这份契约的唯一权威**。它只管一件事：**模型的「为什么结束」必须传到用户面前**。
流式入口（生成器版 `query` + `qa --stream`）与接口层各有一份契约，见那两份脚本。

留下的缺口：

    实测到 provider 偶发返回**半截答案**（「…同时用向量」就没了），而
    `adapters/llm_deepseek.py` 只 yield chunk.content、把 finish_reason 丢了 ——
    用户拿到半句话却没有任何提示，比明说「答不出来」更误导。

公开契约：

    from project_rag.domain import LlmDelta
    LlmDelta(text: str = "", reasoning_text: str = "",
             finish_reason: str | None = None, last: bool = False)
    LlmDelta.truncated -> bool          # finish_reason 不是 None/"stop" 时为 True
    LlmDelta.reasoning -> bool          # reasoning_text 非空时为 True

    from project_rag.ports import LLMClient
    LLMClient.stream(messages) -> Iterator[LlmDelta]      # 原来吐 str，现在吐帧

    RagAnswer / FaqAnswer / QaAnswer 各多一个字段
    truncated: bool = False             # True = 这句答案是被砍断的

**第 4 条约定 —— 思考与正文分成两个字段**：

    思考模型（DeepSeek-V4 系列）在正文之前先吐 ``reasoning_content``。它**不许进 text**:
    ``"".join(d.text for d in deltas)`` 是全项目对「答案」的定义,混进去就等于把模型的
    草稿印给用户,而且每个消费方都得记得过滤 —— 漏一处就漏一处。所以思考走
    ``reasoning_text``,``text`` 拼起来**永远只有答案**。延迟观测靠它数
    「这一轮想没想、想了多久」(见 `check_m5_5_latency.py`)。

**适配器的三条约定**（后两条是 实测出来的，不是猜的）：

    1. 恰好一帧 ``last=True``，且是最后一帧 —— 消费方只看那一帧就知道「怎么结束的」，
       不用自己去猜哪一帧带 finish_reason。provider 没说原因时该帧 finish_reason=None。
    2. ``finish_reason`` 在 ``chunk.response_metadata`` 里，**不在** ``chunk.finish_reason``
       （langchain 的 AIMessageChunk 没有这个属性；实测 DeepSeek 的帧里
       ``response_metadata={'finish_reason': 'length', ...}``）。
    3. provider 在带 finish_reason 的那帧**之后**还会发一个空帧（无内容、无元数据）。
       那种帧没有信息，适配器直接丢掉；否则消费方会以为「最后一帧没有结束原因」。
    另外 ``chunk.content`` 可能是 ``None``，直接 ``str()`` 会得到字符串 ``"None"`` 混进答案 ——
    这一条没有实测到真实触发，但成本为零，一并钉住。

**为什么 ``None`` 按「没被截断」处理**：拿不到元数据不等于被砍断。宁可漏报（日志里其实能看出
provider 没给原因），也不要每句答案都报「可能截断」—— 那会让真的截断提示失效。

命令：``chat`` / ``rag`` / ``qa`` 在拿到截断的流时都要给用户一句提示。**输出契约**：
提示写到 **stderr**、以 ``[注意]`` 开头（stdout 只放答案本身，便于管道消费）。
⚠️ 断言必须认这个 ``[注意]`` 前缀，不能只搜「截断」二字 —— 用例层的
``_logger.warning`` 里也有「截断」，只搜那两个字的话，**把 CLI 的提示整段删掉检查照样绿**
（这条检查第一次就是这么被骗过去的，反向测试才发现）。``qa --stream``（流式打印）的那份契约见流式入口那条。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import io
import subprocess
import sys
import typing
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _harness import check, report, SkipCheck
from _fakes import FakeConversationStore, FakeEmbedder, FakeFaqStore, FakeKeywordIndex, FakeLLM, FakeMatcher, FakeReranker, FakeVectorStore

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

FAQ_QUESTION = "退款流程怎么走？"
FAQ_ANSWER = "在订单页点「申请退款」，1-3 个工作日到账。"
KB_PARENT = "装饰器是一个接收函数并返回新函数的函数。"
KB_HALF = "装饰器是给函数加行为的语法"  # 半截答案：没有句号和 [1]


def _require(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(
            f"{module_name} 缺少公开成员 {missing}；"
            f"请在 {module_name.replace('.', '/')}/__init__.py 里显式 re-export"
        )
    return module


def _llm_delta():
    return _require("project_rag.domain", ("LlmDelta",)).LlmDelta


def _answer_rag():
    return _require("project_rag.application", ("answer_rag",)).answer_rag


def _answer_question():
    return _require("project_rag.application", ("answer_question",)).answer_question


def _stream_question():
    return _require("project_rag.application", ("stream_question",)).stream_question


def _stream_rag():
    return _require("project_rag.application", ("stream_rag",)).stream_rag


def _done_of():
    return _require("project_rag.application.streaming", ("done_of",)).done_of


def _answer_faq():
    return _require("project_rag.application", ("answer_faq",)).answer_faq


def _imports_of(path: Path) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            pairs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            pairs.append((node.module or "", node.level))
    return pairs


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _FakeChunk:
    """冒充 langchain 的 AIMessageChunk：content / response_metadata / additional_kwargs 三个属性。

    ``additional_kwargs`` 就是 provider 放思考内容的地方（``reasoning_content``）。
    """

    def __init__(self, content, metadata=None, additional=None) -> None:
        self.content = content
        self.response_metadata = metadata if metadata is not None else {}
        self.additional_kwargs = additional if additional is not None else {}


class _FakeSdkClient:
    """冒充 ChatDeepSeek：`.stream()` 吐出一串假 chunk。"""

    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.calls = 0

    def stream(self, messages):
        self.calls += 1
        return iter(self.chunks)


def _qa_event() -> typing.Any:
    return _require("project_rag.domain", ("QaEvent",)).QaEvent


def _refusal_text() -> str:
    return str(_require("project_rag.application.answer_rag", ("REFUSAL_TEXT",)).REFUSAL_TEXT)


def _session(store, session_id: str = "s1", rounds: int = 5):
    Session = _require("project_rag.application", ("Session",)).Session
    return Session(store=store, session_id=session_id, rounds=rounds)


def _kb_store():
    return FakeVectorStore([_kb_hit()])


def _rag_stream(llm, *, hits=None, history=()):
    return _stream_rag()(
        "装饰器有什么用",
        embedder=FakeEmbedder(),
        store=FakeVectorStore([_kb_hit()] if hits is None else hits),
        llm=llm,
        keyword_index=FakeKeywordIndex(),
        history=history,
    )


def _kb_hit():
    from project_rag.domain import Chunk, RetrievedChunk

    return RetrievedChunk(
        chunk=Chunk("c1", "装饰器 子块", "ai", "d1", "p1", KB_PARENT), score=0.91
    )


def _faq_entry():
    FaqEntry = _require("project_rag.domain", ("FaqEntry",)).FaqEntry
    return FaqEntry(id=7, question=FAQ_QUESTION, answer=FAQ_ANSWER)


def _rag_call(llm, *, hits=None, query="装饰器有什么用", history=()):
    """用假端口跑一次 answer_rag。"""
    return _answer_rag()(
        query,
        embedder=FakeEmbedder(),
        store=FakeVectorStore([_kb_hit()] if hits is None else hits),
        llm=llm,
        keyword_index=FakeKeywordIndex(),
        history=history,
    )


def _run_cli(argv: list[str], deps) -> tuple[int, str, str]:
    from project_rag.cli import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


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
            "这些检查引用了**后文**才定义的帮助函数，跑起来会是 NameError：\n    "
            + "\n    ".join(problems)
            + "\n修法：把那个帮助函数移到**文件顶部**（所有 @check 之前）"
        )
    return f"{len(defined)} 个顶层定义全部先于引用它的检查"


# ── 1. 领域类型 ───────────────────────────────────────────────────────────────
@check("domain：LlmDelta 是 frozen+slots dataclass，字段与默认值按契约，且包入口可导入")
def _() -> str:
    LlmDelta = _llm_delta()
    # is_dataclass / fields 都传表达式:它们是 TypeGuard,会把变量收窄成
    # 「DataclassInstance | type」,于是后面 LlmDelta(...) 会被判成「实例不可调用」
    if not dataclasses.is_dataclass(_llm_delta()):
        raise AssertionError("LlmDelta 不是 dataclass")
    if not getattr(_llm_delta(), "__dataclass_params__").frozen:
        raise AssertionError("LlmDelta 应为 frozen=True（一帧一旦产出就不该被改）")
    if not getattr(_llm_delta(), "__slots__", None):
        raise AssertionError("LlmDelta 应为 slots=True")
    names = [field.name for field in dataclasses.fields(_llm_delta())]
    # `usage` 是计费口径加的（provider 在收尾帧回报 token 用量，消耗报表的唯一来源）。
    # **这条断言以前漏了它**：check_m5_stream 在 LOCAL_ONLY（要真 key），
    # 所以 `--set ci` 永远照不到这一条 —— 契约跟着 domain 变了它却没人改。
    if names != ["text", "reasoning_text", "finish_reason", "last", "usage"]:
        raise AssertionError(
            f"字段应为 ['text','reasoning_text','finish_reason','last','usage']，实际 {names}"
        )
    empty = LlmDelta()
    if (empty.text, empty.reasoning_text, empty.finish_reason, empty.last) != ("", "", None, False):
        raise AssertionError(
            f"默认值应为 ('', '', None, False)，实际 "
            f"{(empty.text, empty.reasoning_text, empty.finish_reason, empty.last)}"
        )
    if empty.usage is not None:
        raise AssertionError(
            f"usage 默认应为 None（没量到 ≠ 0），实际 {empty.usage!r} —— "
            "折成 0 会让报表把「不知道多少钱」显示成 0.00"
        )
    if empty.reasoning or LlmDelta(text="答").reasoning or not LlmDelta(reasoning_text="想").reasoning:
        raise AssertionError("reasoning 应只看 reasoning_text：空帧/正文帧 False，思考帧 True")
    exported = getattr(importlib.import_module("project_rag.domain"), "__all__", [])
    if "LlmDelta" not in exported:
        raise AssertionError("domain/__init__.py 的 __all__ 里缺 LlmDelta")
    return "字段与默认值一致，且从包入口可导入"


@check("domain：LlmDelta.truncated 只认「非 None 且非 stop」（拿不到原因不算截断）")
def _() -> str:
    LlmDelta = _llm_delta()
    want = {
        None: False,
        "stop": False,
        "length": True,
        "content_filter": True,
        "tool_calls": True,
    }
    for reason, expected in want.items():
        got = LlmDelta(finish_reason=reason).truncated
        if got != expected:
            raise AssertionError(f"finish_reason={reason!r} 时 truncated 应为 {expected}，实际 {got}")
    # 文本帧（还没结束）永远不是截断
    if LlmDelta(text="半句").truncated:
        raise AssertionError("没有结束原因的文本帧不该被当成截断")
    return "五种结束原因判定正确"


@check("domain：三个回答类型都多了 truncated 字段（默认 False），FAQ 命中的那种也不例外")
def _() -> str:
    module = _require(
        "project_rag.domain", ("FaqAnswer", "RagAnswer", "QaAnswer")
    )
    for name in ("FaqAnswer", "RagAnswer", "QaAnswer"):
        fields = {field.name: field for field in dataclasses.fields(getattr(module, name))}
        if "truncated" not in fields:
            raise AssertionError(f"{name} 缺 truncated 字段")
        if fields["truncated"].default is not False:
            raise AssertionError(f"{name}.truncated 默认值应为 False")
    return "FaqAnswer / RagAnswer / QaAnswer 都有 truncated=False"


@check("端口：LLMClient.stream 现在吐 LlmDelta（原来是 str）")
def _() -> str:
    port = _require("project_rag.ports", ("LLMClient",)).LLMClient
    annotation = inspect.signature(port.stream).return_annotation
    text = str(annotation)
    if "LlmDelta" not in text:
        raise AssertionError(f"stream 的返回注解应含 LlmDelta，实际 {text!r}；契约：Iterator[LlmDelta]")
    if "Iterator" not in text:
        raise AssertionError(f"stream 应返回迭代器，实际 {text!r}")
    return f"return annotation = {text}"


# ── 2. 适配器：把 provider 的元数据如实映射出来（不联网） ─────────────────────
def _adapter_with(chunks):
    """造一个 DeepSeekLLM，并把里面真正的 langchain 客户端换成假件（不联网、不花钱）。"""
    from pydantic import SecretStr

    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.config import LLMConfig

    adapter = DeepSeekLLM(LLMConfig(deepseek_api_key=SecretStr("test-key")))
    fake = _FakeSdkClient(chunks)
    # 只换掉发请求那一层，映射逻辑仍是真的。用 setattr 是因为 _client 的静态类型是
    # ChatDeepSeek，而这里要的正是「换成一个假件」——pyright 对这种刻意替换只能用 Any 放行。
    setattr(adapter, "_client", fake)
    return adapter, fake


@check("适配器：思考帧走 reasoning_text，正文走 text —— **思考不许混进 text**")
def _() -> str:
    adapter, _fake = _adapter_with(
        [
            _FakeChunk("", additional={"reasoning_content": "先想一步,"}),
            _FakeChunk("", additional={"reasoning_content": "再想一步。"}),
            _FakeChunk("答案"),
            _FakeChunk(None, {"finish_reason": "stop"}),
        ]
    )
    deltas = list(adapter.stream([]))
    thoughts = [d.reasoning_text for d in deltas if d.reasoning]
    if thoughts != ["先想一步,", "再想一步。"]:
        raise AssertionError(f"思考帧该走 reasoning_text，实际 {thoughts!r}（全部帧：{[d.text for d in deltas]!r}）")
    answer = "".join(d.text for d in deltas)
    if answer != "答案":
        raise AssertionError(
            f"text 拼起来必须**只有答案**（这是全项目对「答案」的定义），实际 {answer!r}"
        )
    if any(d.reasoning and d.text for d in deltas):
        raise AssertionError("同一帧不该既算思考又算正文（会把两者混在一起）")
    # 顺序：思考在前、正文在后 —— provider 本来就是这么发的，换成两帧也不该颠倒
    kinds = ["think" if d.reasoning else "text" for d in deltas if d.reasoning or d.text]
    if kinds != ["think", "think", "text"]:
        raise AssertionError(f"帧序应是先思考后正文，实际 {kinds}")
    return "思考 → reasoning_text；text 拼起来只剩答案"


@check("适配器：有内容、无原因的帧 → 文本帧 + finish_reason=None + last=False")
def _() -> str:
    adapter, _fake = _adapter_with([_FakeChunk("你好"), _FakeChunk(None, {"finish_reason": "stop"})])
    deltas = list(adapter.stream([]))
    if [d.text for d in deltas if d.text] != ["你好"]:
        raise AssertionError(f"文本帧不对：{[d.text for d in deltas]!r}")
    if deltas[0].finish_reason is not None or deltas[0].last:
        raise AssertionError(f"文本帧不该带结束原因：{deltas[0]!r}")
    return "文本帧干净"


@check("适配器：恰好一帧 last=True 且在最后，带上 provider 给的结束原因")
def _() -> str:
    adapter, _fake = _adapter_with(
        [_FakeChunk("半"), _FakeChunk(None, {"finish_reason": "stop"})]
    )
    deltas = list(adapter.stream([]))
    last_frames = [i for i, d in enumerate(deltas) if d.last]
    if last_frames != [len(deltas) - 1]:
        raise AssertionError(f"last=True 的帧应恰好是最后一帧，实际下标 {last_frames}（共 {len(deltas)} 帧）")
    if deltas[-1].finish_reason != "stop":
        raise AssertionError(f"收尾帧应带 finish_reason='stop'，实际 {deltas[-1].finish_reason!r}")
    return f"{len(deltas)} 帧，最后一帧 last=True / reason=stop"


@check("适配器：finish_reason='length' → 收尾帧 truncated=True（那次半截答案就是这个）")
def _() -> str:
    adapter, _fake = _adapter_with(
        [_FakeChunk(KB_HALF), _FakeChunk(None, {"finish_reason": "length"})]
    )
    deltas = list(adapter.stream([]))
    if not deltas[-1].truncated:
        raise AssertionError(f"收尾帧应判为截断，实际 {deltas[-1]!r}")
    return "length → truncated"


@check("适配器：收尾之后的空帧要丢掉（实测 DeepSeek 会再发一个空元数据帧）")
def _() -> str:
    adapter, _fake = _adapter_with(
        [
            _FakeChunk("好"),
            _FakeChunk(None, {"finish_reason": "stop"}),
            _FakeChunk(""),  # 真实 provider 会来这么一帧：无内容、无元数据
            _FakeChunk(None, {}),
        ]
    )
    deltas = list(adapter.stream([]))
    if len(deltas) != 2:
        raise AssertionError(f"应只剩「文本帧 + 收尾帧」两帧，实际 {deltas!r}")
    if not deltas[-1].last or deltas[-1].finish_reason != "stop":
        raise AssertionError(f"最后一帧应是收尾帧，实际 {deltas[-1]!r}")
    return "空帧被丢掉，收尾帧仍是最后一帧"


@check("适配器：content=None 不能变成字符串 'None'（旧代码 str(None) 就会）")
def _() -> str:
    adapter, _fake = _adapter_with(
        [_FakeChunk("答案"), _FakeChunk(None, {"finish_reason": "stop"})]
    )
    joined = "".join(d.text for d in adapter.stream([]))
    if "None" in joined or joined != "答案":
        raise AssertionError(f"拼接结果应为 '答案'，实际 {joined!r}")
    return "None 被吃掉了"


@check("适配器：上游抛错原样上抛（异常不在这里吞 —— 翻译成人话是 cli 层的事）")
def _() -> str:
    class _Boom:
        def stream(self, messages):
            raise RuntimeError("provider 挂了")

    adapter, _fake = _adapter_with([])
    setattr(adapter, "_client", _Boom())
    try:
        list(adapter.stream([]))
    except RuntimeError as exc:
        if "provider" not in str(exc):
            raise AssertionError(f"异常应原样上抛，实际 {exc!r}")
        return "异常原样上抛"
    raise AssertionError("上游抛错被吞了")


# ── 3. 非流式链路：把 truncated 透传到答案对象 ───────────────────────────────
@check("answer_rag：收尾是 length → RagAnswer.truncated=True，且**保留半截答案**（不换成拒答话术）")
def _() -> str:
    result = _rag_call(FakeLLM((KB_HALF,), finish_reason="length"))
    if not result.truncated:
        raise AssertionError("RagAnswer.truncated 应为 True")
    if result.answer != KB_HALF:
        raise AssertionError(f"半截答案要照原样返回（用户已经看见了），实际 {result.answer!r}")
    if result.refused:
        raise AssertionError("截断不是拒答 —— 不能把「答了一半」说成「信息不足」")
    return f"truncated=True，答案仍是半截（{len(result.answer)} 字）"


@check("answer_rag：收尾是 stop → truncated=False；空回答仍按拒答（回归）")
def _() -> str:
    ok = _rag_call(FakeLLM(("完整答案 [1]",), finish_reason="stop"))
    if ok.truncated or ok.refused:
        raise AssertionError(f"正常结束不该有任何异常标记：truncated={ok.truncated} refused={ok.refused}")
    empty = _rag_call(FakeLLM((), finish_reason="stop"))
    if not empty.refused or empty.truncated:
        raise AssertionError("空回答要按拒答处理，且这不是截断")
    return "stop → 正常；空回答仍拒答"


@check("answer_faq：未命中走模型兜底时，截断也透传（含『截断了也照常进负缓存』这条）")
def _() -> str:
    from project_rag.domain import ChatMessage

    class _Cache:
        def __init__(self) -> None:
            self.sets: list[tuple] = []

        def get(self, key):
            return None

        def set(self, key, value, ttl):
            self.sets.append((key, value, ttl))

    cache = _Cache()
    result = _answer_faq()(
        "超纲问题",
        store=FakeFaqStore([_faq_entry()]),
        matcher=FakeMatcher(None),
        llm=FakeLLM((KB_HALF,), finish_reason="length"),
        cache=cache,
    )
    if result.source != "llm":
        raise AssertionError(f"未命中应走模型兜底，实际 {result.source!r}")
    if not result.truncated or result.answer != KB_HALF:
        raise AssertionError(f"截断要透传且保留半截，实际 {result!r}")
    if not cache.sets:
        raise AssertionError("兜底答案（哪怕被截断）仍要写缓存 —— 否则同一问题每次都烧 token")
    del ChatMessage
    return "answer_faq 也透传 truncated 且照常缓存"


@check("answer_question：知识库通道的 truncated 透传到 QaAnswer；FAQ 命中时是 False")
def _() -> str:
    def ask(*, faq_score=None, finish_reason="stop"):
        return _answer_question()(
            "装饰器有什么用",
            faq_store=FakeFaqStore([_faq_entry()]),
            matcher=FakeMatcher(faq_score),
            knowledge=lambda _history, _timing: _stream_rag()(
                "装饰器有什么用",
                embedder=FakeEmbedder(),
                store=FakeVectorStore([_kb_hit()]),
                llm=FakeLLM((KB_HALF,), finish_reason=finish_reason),
                keyword_index=FakeKeywordIndex(),
            ),
            faq_threshold=0.85,
        )

    result = ask(finish_reason="length")
    if result.channel != "rag" or not result.truncated:
        raise AssertionError(f"知识库通道的截断应透传，实际 {result!r}")
    if ask(faq_score=0.97).truncated:
        raise AssertionError("FAQ 命中的标准答案不可能被截断")
    return "知识库通道截断透传；FAQ 命中永远是 False"


@check("分层：llm_deepseek 仍是唯一 import langchain 的地方；application 只认 LlmDelta")
def _() -> str:
    leaks = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "llm_deepseek.py"
        for module, level in _imports_of(path)
        if level == 0 and module.split(".")[0] in ("langchain", "langchain_core", "langchain_deepseek", "openai")
    ]
    if leaks:
        raise AssertionError(f"这些文件也 import 了模型 SDK：{leaks}")
    return "模型 SDK 只在 adapters/llm_deepseek.py"


# ── 4. 命令行：截断必须让人看见 ───────────────────────────────────────────────
def _deps(llm, *, rag_hits=None, conversation=None):
    from project_rag.cli import Deps

    chat_store = conversation if conversation is not None else FakeConversationStore()
    return Deps(
        llm_factory=lambda: llm,
        faq_store_factory=lambda: FakeFaqStore([_faq_entry()]),
        matcher_factory=lambda: FakeMatcher(None),
        cache_factory=None,
        faq_threshold=0.85,
        embedder_factory=lambda: FakeEmbedder(),
        vector_store_factory=lambda: FakeVectorStore([_kb_hit()] if rag_hits is None else rag_hits),
        keyword_index_factory=lambda: FakeKeywordIndex(),
        reranker_factory=lambda: FakeReranker(),
        conversation_store_factory=lambda: chat_store,
    )


@check("CLI：chat 收到截断的流 → stdout 是半截答案本身，stderr 有提示（管道消费不受影响）")
def _() -> str:
    from project_rag.cli import Deps

    deps = Deps(llm_factory=lambda: FakeLLM(("半", "截"), finish_reason="length"))
    code, out, err = _run_cli(["chat", "-p", "你好"], deps)
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if out.strip() != "半截":
        raise AssertionError(f"stdout 应只有答案本身，实际 {out!r}")
    if "[注意]" not in err or "截断" not in err:
        raise AssertionError(f"stderr 要有以 [注意] 开头的截断提示，实际 {err!r}")
    return f"stdout={out.strip()!r}，stderr 有截断提示"


@check("CLI：rag 注入假 Deps + 截断 → 答案照打，另有截断提示；正常时没有提示")
def _() -> str:
    half = FakeLLM((KB_HALF,), finish_reason="length")
    code, out, err = _run_cli(["rag", "-q", "装饰器有什么用"], _deps(half))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if KB_HALF not in out:
        raise AssertionError(f"半截答案也要打出来，实际 {out!r}")
    if "[注意]" not in err or "截断" not in err:
        raise AssertionError(f"要有以 [注意] 开头的截断提示（stderr），实际 stdout={out!r} stderr={err!r}")

    code, out, err = _run_cli(
        ["rag", "-q", "装饰器有什么用"], _deps(FakeLLM(("完整 [1]",), finish_reason="stop"))
    )
    if "[注意]" in (out + err):
        raise AssertionError("正常结束不该有截断提示（否则真截断时提示就没人看了）")
    return "截断有提示、正常没提示"


@check("CLI：qa 同样要给截断提示（合流入口不例外）")
def _() -> str:
    half = FakeLLM((KB_HALF,), finish_reason="length")
    code, out, err = _run_cli(["qa", "-q", "装饰器有什么用"], _deps(half))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if "[注意]" not in err or "截断" not in err:
        raise AssertionError(f"要有以 [注意] 开头的截断提示（stderr），实际 stdout={out!r} stderr={err!r}")
    return "qa 也提示截断"


@check("真实链路：真 DeepSeek 流式调用一次，最后一帧必须带 finish_reason（花钱但极少）")
def _() -> str:
    """这一条**会调用真实模型**（一个问题、上限 16 token）。

    为什么值得留在契约里：`response_metadata` 里有没有 finish_reason 是**provider 的行为**，
    假件永远证明不了。实测确认有；哪天 DeepSeek 改了协议，这条会先红。
    没配密钥就 SKIP。
    """
    import os

    if not os.environ.get("RAG_LLM__DEEPSEEK_API_KEY") and not (ROOT / ".env").exists():
        raise SkipCheck("没有 .env / 密钥：真链路需要 provider 凭证（owner：本地环境）")

    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.config import get_settings
    from project_rag.domain import ChatMessage

    deltas = list(
        DeepSeekLLM(get_settings().llm).stream(
            [ChatMessage(role="user", content="只回一个字：好")]
        )
    )
    if not deltas:
        raise AssertionError("一帧都没有")
    if not deltas[-1].last:
        raise AssertionError(f"最后一帧应带 last=True，实际 {deltas[-1]!r}")
    if deltas[-1].finish_reason is None:
        raise AssertionError(
            "provider 没给 finish_reason —— 截断检测就无从谈起（字段位置可能变了，"
            f"去看 response_metadata 里还有什么：实测过的是 response_metadata['finish_reason']）"
        )
    return f"{len(deltas)} 帧，收尾 finish_reason={deltas[-1].finish_reason!r}"


@check("思考模式：配置意图 -> provider 参数（auto 一个都不发；off/low/high/max 发 reasoning_effort）")
def _() -> str:
    """纯假件:把适配器里的 ChatDeepSeek 换成记录器,看构造参数。

    为什么要钉这条:字段名与取值是**provider/SDK 知识**(`reasoning_effort` 的值 `none`
    而不是 `thinking={"type":"disabled"}`),拼错了要么静默失效(extra="ignore"),
    要么在真调用时才炸 —— 这条检查让它在离线就能红。
    """
    from pydantic import SecretStr

    from project_rag.adapters import llm_deepseek as module
    from project_rag.config import LLMConfig

    seen: list[dict] = []
    real = module.ChatDeepSeek

    class _Recorder:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def stream(self, messages):  # 端口形状;这条检查不消费
            return iter(())

    setattr(module, "ChatDeepSeek", _Recorder)
    try:
        for value in ("auto", "off", "low", "high", "max"):
            module.DeepSeekLLM(
                LLMConfig(deepseek_api_key=SecretStr("k"), thinking=value)
            )
    finally:
        setattr(module, "ChatDeepSeek", real)

    labels = ("auto", "off", "low", "high", "max")
    got = [kw.get("reasoning_effort", "-") for kw in seen]
    want = ["-", "none", "low", "high", "max"]
    if got != want:
        raise AssertionError(
            f"映射不对：{dict(zip(labels, got))}，期望 {dict(zip(labels, want))}"
        )
    return f"auto 不发；off->none；low/high/max 原样（{len(seen)} 次构造）"


@check("真实链路：thinking=off 真的不思考（0 个思考帧），auto 仍思考 —— provider 行为**且适配器如实交出**")
def _() -> str:
    """**会真调两次**（每次上限 32 token）。

    为什么值得留在契约里:开关是 provider 行为,假件证明不了;而它是「省掉那 2~15 秒」的
    唯一开关 —— 哪天 provider 改了字段语义,这条要先红。没配密钥就 SKIP。

    注意 `auto` 那一半也在断言**默认没被悄悄改掉**:它必须仍在思考。

    改走**我们自己的适配器**(以前是绕过它直接看 SDK 的 chunk,因为那时适配器
    按设计把思考丢了):现在「思考帧有没有交出来」本身就是契约的一部分 ——
    延迟账里的 think_tokens / llm_think_ms 全靠它,只测 SDK 就测不到这一段。
    """
    import os

    if not os.environ.get("RAG_LLM__DEEPSEEK_API_KEY") and not (ROOT / ".env").exists():
        raise SkipCheck("没有 .env / 密钥：真链路需要 provider 凭证（owner：本地环境）")

    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.config import get_settings
    from project_rag.domain import ChatMessage

    base = get_settings().llm

    def probe(thinking: str) -> tuple[int, bool, str, str]:
        """输入:思考模式。行为:真调一次,分开收思考与正文。输出:(思考帧数, 有正文?, 正文, 思考全文)。"""
        client = DeepSeekLLM(base.model_copy(update={"thinking": thinking, "max_tokens": 32}))
        frames = 0
        answer = ""
        thought = ""
        for delta in client.stream(
            [ChatMessage(role="user", content="9.11 和 9.8 哪个大？只回答数字")]
        ):
            if delta.reasoning:
                frames += 1
                thought += delta.reasoning_text
            answer += delta.text
        return frames, bool(answer.strip()), answer, thought

    off_frames, off_content, _off_answer, _off_thought = probe("off")
    if off_frames:
        raise AssertionError(f"thinking=off 仍有 {off_frames} 个思考帧 —— 开关没生效")
    if not off_content:
        raise AssertionError("thinking=off 却一个内容字都没出（32 token 回一个字绰绰有余）")
    auto_frames, _, auto_answer, auto_thought = probe("auto")
    if auto_frames == 0:
        raise AssertionError(
            "auto（= 加开关之前的行为）应该仍在思考,实际 0 个 reasoning 帧 —— 默认被悄悄改了"
        )
    # 真 provider 才能证明这件事:**SDK 有没有把思考也塞进 chunk.content**。
    # 假件证明不了(它按我们以为的样子造的);真塞了的话,适配器的 text 里就会有思考,
    # 而 text 拼起来正是用户看到的答案。取思考的前 8 个字当指纹去找。
    if len(auto_thought) >= 8 and auto_thought[:8] in auto_answer:
        raise AssertionError(f"思考混进正文了（text 里出现了思考的开头）:{auto_answer!r}")
    return (
        f"off：{off_frames} 思考帧 + 有内容；auto：{auto_frames} 思考帧、"
        f"思考 {len(auto_thought)} 字不进正文（正文 {len(auto_answer)} 字）"
    )


@check("脚本能被 pyright 之外的方式独立跑起来（--help 只是语法自检）")
def _() -> str:
    proc = subprocess.run(
        [sys.executable, "-c", "import ast,pathlib;ast.parse(pathlib.Path('scripts/acceptance/check_m5_stream.py').read_text(encoding='utf-8'))"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-300:])
    return "语法自检通过"


# ── 5. 流式入口：帧协议,以及「流式 = 非流式的管道」 ────────────────────
@check("domain：QaEvent 是 frozen+slots，kind 只有 sources/token/done，包入口可导入")
def _() -> str:
    QaEvent = _qa_event()
    if not dataclasses.is_dataclass(_qa_event()):
        raise AssertionError("QaEvent 不是 dataclass")
    if not getattr(_qa_event(), "__dataclass_params__").frozen:
        raise AssertionError("QaEvent 应为 frozen=True")
    names = [field.name for field in dataclasses.fields(_qa_event())]
    want = ["kind", "text", "answer", "channel", "citations", "faq_match", "refused", "truncated"]
    if names != want:
        raise AssertionError(f"字段应为 {want}，实际 {names}")
    args = typing.get_args(typing.get_type_hints(_qa_event())["kind"])
    if args != ("sources", "token", "done"):
        raise AssertionError(f"kind 应为 Literal['sources','token','done']，实际 {args}")
    event = QaEvent(kind="token", text="x")
    if (event.answer, event.citations, event.refused, event.truncated) != ("", (), False, False):
        raise AssertionError(f"默认值不对：{event!r}")
    exported = getattr(importlib.import_module("project_rag.domain"), "__all__", [])
    if "QaEvent" not in exported:
        raise AssertionError("domain/__init__.py 的 __all__ 里缺 QaEvent")
    return "字段、kind 取值与导出都对"


@check("帧协议：恰好一帧 done 时收拢到它；流里没有 done 帧 → 当场报错（不许当完整答案）")
def _() -> str:
    QaEvent = _qa_event()
    done = _done_of()([QaEvent(kind="token", text="半"), QaEvent(kind="done", answer="半")])
    if done.answer != "半":
        raise AssertionError(f"收拢结果不对：{done!r}")
    try:
        _done_of()([QaEvent(kind="token", text="半")])
    except RuntimeError as exc:
        if "收尾帧" not in str(exc):
            raise AssertionError(f"错误信息要说清缺什么，实际 {exc!r}")
        return "取到 done；缺 done 时报错"
    raise AssertionError("没有 done 帧却当成正常 —— 半截答案会被当成完整答案")


@check("stream_rag：检索为空 → token(拒答话术) + done(refused)，且不调 LLM")
def _() -> str:
    llm = FakeLLM(("不该被调用",))
    events = list(_rag_stream(llm, hits=[]))
    kinds = [event.kind for event in events]
    if kinds != ["token", "done"]:
        raise AssertionError(f"帧序列应为 [token, done]，实际 {kinds}")
    if not events[-1].refused or events[-1].answer != _refusal_text():
        raise AssertionError(f"拒答语义不对：{events[-1]!r}")
    if events[0].text != _refusal_text():
        raise AssertionError("拒答话术也必须走 token 帧（消费方才不用写分支）")
    if llm.calls:
        raise AssertionError("检索为空不该调 LLM")
    return "两帧：话术 + 收尾"


@check("stream_rag：sources 在最前、token 拼接 == done.answer、引用原样透传")
def _() -> str:
    events = list(_rag_stream(FakeLLM(("装饰器是", "给函数加行为的语法 [1]"))))
    kinds = [event.kind for event in events]
    if kinds[0] != "sources" or kinds[-1] != "done" or set(kinds[1:-1]) != {"token"}:
        raise AssertionError(f"帧顺序应为 sources → token* → done，实际 {kinds}")
    joined = "".join(event.text for event in events if event.kind == "token").strip()
    if joined != events[-1].answer:
        raise AssertionError(f"恒等式破了：token 拼接 {joined!r} ≠ done.answer {events[-1].answer!r}")
    sources = [event for event in events if event.kind == "sources"][0]
    if not events[-1].citations or events[-1].citations != sources.citations:
        raise AssertionError("收尾帧的引用应与 sources 那帧一致且非空")
    return f"{len(events)} 帧：sources → 2 token → done"


@check("stream_rag：截断时 done.truncated=True，token 里仍是那半截（不换拒答话术）")
def _() -> str:
    events = list(_rag_stream(FakeLLM((KB_HALF,), finish_reason="length")))
    done = events[-1]
    if not done.truncated or done.refused or done.answer != KB_HALF:
        raise AssertionError(f"截断语义不对：{done!r}")
    return "truncated=True，半截照发"


@check("answer_rag 就是 stream_rag 的收拢：正常/截断/拒答三种情形逐字一致")
def _() -> str:
    cases = [
        ("正常", FakeLLM(("装饰器是", "给函数加行为的语法 [1]")), [_kb_hit()]),
        ("截断", FakeLLM((KB_HALF,), finish_reason="length"), [_kb_hit()]),
        ("拒答", FakeLLM(("x",)), []),
    ]
    for name, llm, hits in cases:
        streamed = _done_of()(_rag_stream(llm, hits=hits))
        folded = _rag_call(llm, hits=hits)
        got = (streamed.answer, streamed.citations, streamed.refused, streamed.truncated)
        want = (folded.answer, folded.citations, folded.refused, folded.truncated)
        if got != want:
            raise AssertionError(f"{name}：流式 {got!r} ≠ 非流式 {want!r}")
    return "三种情形都逐字一致"


@check("stream_question：FAQ 命中 → token + done(channel=faq)；不读历史、knowledge 零调用、记一轮")
def _() -> str:
    store = FakeConversationStore()
    calls: list = []

    def knowledge(history, timing):
        calls.append(tuple(history))
        return iter(())

    events = list(
        _stream_question()(
            FAQ_QUESTION,
            faq_store=FakeFaqStore([_faq_entry()]),
            matcher=FakeMatcher(0.97),
            knowledge=knowledge,
            session=_session(store),
        )
    )
    kinds = [event.kind for event in events]
    if kinds != ["token", "done"]:
        raise AssertionError(f"帧序列应为 [token, done]，实际 {kinds}")
    done = events[-1]
    if done.channel != "faq" or done.faq_match is None or done.answer != FAQ_ANSWER:
        raise AssertionError(f"收尾帧不对：{done!r}")
    if store.reads:
        raise AssertionError("FAQ 命中不该读历史")
    if calls:
        raise AssertionError("FAQ 命中不该调知识库通道")
    if len(store.records) != 1 or store.records[0][1].answer != FAQ_ANSWER:
        raise AssertionError(f"这一轮要记下来，实际 {store.records}")
    return "标准答案以 token 帧交付；不读历史但记一轮"


@check("stream_question：知识库 → 转发 sources/token、自己补 done(channel=rag)；读历史并记一轮")
def _() -> str:
    store = FakeConversationStore()
    events = list(
        _stream_question()(
            "装饰器有什么用",
            faq_store=FakeFaqStore([_faq_entry()]),
            matcher=FakeMatcher(None),
            knowledge=lambda history, timing: _rag_stream(FakeLLM(("装饰器是", "给函数加行为的语法 [1]")), history=history),
            session=_session(store, rounds=3),
        )
    )
    kinds = [event.kind for event in events]
    if kinds[0] != "sources" or kinds[-1] != "done":
        raise AssertionError(f"知识库通道的帧应被转发（sources 在最前、done 收尾），实际 {kinds}")
    done = events[-1]
    if done.channel != "rag" or done.faq_match is not None:
        raise AssertionError(f"收尾帧应标明「知识库通道答的」：{done!r}")
    joined = "".join(event.text for event in events if event.kind == "token").strip()
    if joined != done.answer:
        raise AssertionError("转发之后恒等式仍要成立")
    if store.reads != [("s1", 3)]:
        raise AssertionError(f"应读一次历史（limit=rounds=3），实际 {store.reads}")
    if len(store.records) != 1 or store.records[0][1].answer != done.answer:
        raise AssertionError(f"这一轮（含完整答案）要记下来，实际 {store.records}")
    return "转发帧 + 补 channel + 读历史 + 记一轮"


@check("stream_question：客户端中途断开 → 这一轮不入库（半截答案进历史比没有更糟）")
def _() -> str:
    store = FakeConversationStore()
    stream = _stream_question()(
        "装饰器有什么用",
        faq_store=FakeFaqStore([_faq_entry()]),
        matcher=FakeMatcher(None),
        knowledge=lambda history, timing: _rag_stream(
            FakeLLM(("装饰器是", "完整的后半句 [1]")), history=history
        ),
        session=_session(store),
    )
    first = next(stream)  # 拿到 sources 就断开
    if first.kind != "sources":
        raise AssertionError(f"第一帧应是 sources，实际 {first!r}")
    stream.close()
    if store.records:
        raise AssertionError(f"中途断开却入库了：{store.records}")
    return "断开后历史里没有这一轮"


@check("answer_question 就是 stream_question 的收拢：FAQ 命中 / 知识库 / 拒答 三种情形一致")
def _() -> str:
    pieces = ("装饰器是", "给函数加行为的语法 [1]")

    def knowledge(hits, history):
        return _rag_stream(FakeLLM(pieces), hits=hits, history=history)

    for name, faq_score, hits in (
        ("FAQ 命中", 0.97, [_kb_hit()]),
        ("知识库", None, [_kb_hit()]),
        ("拒答", None, []),
    ):
        folded = _answer_question()(
            "装饰器有什么用",
            faq_store=FakeFaqStore([_faq_entry()]),
            matcher=FakeMatcher(faq_score),
            knowledge=lambda history, timing: knowledge(hits, history),
            faq_threshold=0.85,
        )
        streamed = _done_of()(
            _stream_question()(
                "装饰器有什么用",
                faq_store=FakeFaqStore([_faq_entry()]),
                matcher=FakeMatcher(faq_score),
                knowledge=lambda history, timing: knowledge(hits, history),
                faq_threshold=0.85,
            )
        )
        got = (
            streamed.answer, streamed.channel, streamed.citations,
            streamed.refused, streamed.truncated,
        )
        want = (
            folded.answer, folded.channel, folded.citations,
            folded.refused, folded.truncated,
        )
        if got != want:
            raise AssertionError(f"{name}：流式 {got!r} ≠ 非流式 {want!r}")
    return "三种情形都逐字一致"


@check("两个流式入口都是生成器函数（边生成边出，而不是先算完再交出来）")
def _() -> str:
    for fn, name in ((_stream_rag(), "stream_rag"), (_stream_question(), "stream_question")):
        if not inspect.isgeneratorfunction(fn):
            raise AssertionError(f"{name} 不是生成器函数 —— 「边生成边出」就不成立")
    return "stream_rag / stream_question 都是生成器"


@check("CLI：qa --help 有 --stream；--stream 与不加的答案逐字相同（非流式就是收拢）")
def _() -> str:
    helped = subprocess.run(
        [sys.executable, "-m", "project_rag", "qa", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if helped.returncode != 0 or "--stream" not in helped.stdout:
        raise AssertionError(f"qa --help 里没有 --stream：{helped.stdout[-200:]!r}")

    llm = FakeLLM(("装饰器是", "给函数加行为的语法 [1]"))
    stream_code, stream_out, stream_err = _run_cli(
        ["qa", "-q", "装饰器有什么用", "--stream"], _deps(llm)
    )
    fold_code, fold_out, fold_err = _run_cli(["qa", "-q", "装饰器有什么用"], _deps(llm))
    if stream_code != 0 or fold_code != 0:
        raise AssertionError(
            f"退出码 {stream_code}/{fold_code}；{stream_err[:200]!r} {fold_err[:200]!r}"
        )
    first_stream = stream_out.splitlines()[0] if stream_out else ""
    first_fold = fold_out.splitlines()[0] if fold_out else ""
    if first_stream != first_fold or "装饰器是给函数加行为的语法 [1]" not in first_stream:
        raise AssertionError(f"--stream 与不加的答案不一致：{first_stream!r} vs {first_fold!r}")
    return "两条路首行答案逐字相同"


@check("CLI：qa --stream --session 记一轮；截断时 stderr 有 [注意]")
def _() -> str:
    store = FakeConversationStore()
    code, out, err = _run_cli(
        ["qa", "-q", "装饰器有什么用", "--stream", "--session", "s1"],
        _deps(FakeLLM(("装饰器是", "给函数加行为的语法 [1]")), conversation=store),
    )
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if len(store.records) != 1:
        raise AssertionError(f"--stream --session 应记一轮，实际 {store.records}")

    half = FakeLLM((KB_HALF,), finish_reason="length")
    code, out, err = _run_cli(["qa", "-q", "装饰器有什么用", "--stream"], _deps(half))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if KB_HALF not in out:
        raise AssertionError(f"逐字打印的答案要在 stdout，实际 {out!r}")
    if "[注意]" not in err or "截断" not in err:
        raise AssertionError(f"截断要有以 [注意] 开头的提示，实际 stderr={err!r}")
    return "流式也记历史、也提示截断"


def main() -> int:
    return report("流式（结束原因可见 + 流式入口）验收")


if __name__ == "__main__":
    sys.exit(main())

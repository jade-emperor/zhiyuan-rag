#!/usr/bin/env python
"""验收：**查询改写** —— 合流通道里「FAQ 未命中 → 向量检索」之间那一步。

用法：

    uv run python scripts/acceptance/check_m8_rewrite.py

本脚本是这份契约的**唯一权威**。

公开契约（import 路径本身也是承诺）：

    from project_rag.domain import (QueryRewrite, RewriteStrategy, STRATEGIES,
                                    STRATEGY_NAMES, StrategyPrediction,
                                    direct_rewrite, label_of, strategy_of)
    RewriteStrategy = Literal["direct", "hypothetical", "subquery", "denoise"]
    STRATEGIES = ("direct", "hypothetical", "subquery", "denoise")   # 索引即标签

    from project_rag.ports import QueryRewriter, StrategyClassifier
    StrategyClassifier.classify(query) -> StrategyPrediction
    QueryRewriter.rewrite(query)      -> QueryRewrite

    from project_rag.application import plan_rewrite, retrieve_many
    plan_rewrite(query, *, classifier, llm=None, max_subqueries=3,
                 max_query_chars=400, confidence_threshold=0.0) -> QueryRewrite
    retrieve_many(queries, *, embedder, store, keyword_index=None, reranker=None,
                  options=None, timing=None, rerank_query=None) -> tuple[Citation, ...]

两条不变式（``plan_rewrite`` 负责保证，本脚本逐条盯着）：

    queries 永不为空；失败一律退回 (原话,)
    strategy == "direct"  ⟺  queries == (original,)

**这一份在堵什么洞**：

1. **改写失败不许让问答失败**。分类器是可选件（权重不在版本库里，换台机器就可能没有），
   LLM 会说胡话（不吐 json、吐半截）。这些情况的正确表现是「退回原话检索 + 日志留痕」，
   而不是抛异常、也不是静默产出空检索语句 —— **空 queries 会让检索召空、伪装成
   「知识库里没有」**，那是最坏的一种失败形状。
2. **改写只改检索、不改回答**。生成答案用的必须仍是用户原话：拿假设答案的正文去当
   prompt 里的「问题」，模型会开始回答那个假设答案，用户看到的就是自问自答。
3. **多句检索的合并口径**。子查询分解会带回好几句，各自召回后按 ``chunk_id`` 去重，
   排序交给「用原话重排一次」—— 没装重排器时那是近似的，这一份把两种情形都钉住。
4. **账本口径**：``rewrite_ms`` 与 ``retrieve_ms`` 并列（改写之后 embed/retrieve 从
   改写结束算起）。否则一次 LLM 改写的耗时会被算进「检索花了多久」，那个数就废了。
5. **重依赖不许在 import 期拉起来**：``adapters/strategy_lora.py`` 顶层的 import 里
   不许出现 torch / transformers / peft，``adapters/__init__.py`` 也不许 re-export 它 ——
   否则 ``--help`` 要替几百兆的包付时间（那条纪律见 ``adapters/__init__.py``）。

**不需要容器、不需要模型、不需要网络**：分类器、LLM、向量库这一份里全是假件。
真模型那条路由 ``tools/lora/`` 自己的命令量（``infer.py --test``）。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from _fakes import FakeLLM, FakeVectorStore
from _harness import check, report

if TYPE_CHECKING:
    from collections.abc import Iterator

    from project_rag.domain import LlmDelta, RewriteStrategy

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

QUESTION = "人工智能有哪些应用？"
# 四种策略各一条判定结果:标签顺序就是 domain.STRATEGIES 的顺序
LABEL_OF: dict[RewriteStrategy, int] = {
    "direct": 0,
    "hypothetical": 1,
    "subquery": 2,
    "denoise": 3,
}


def _require(module_name: str, names: tuple[str, ...]):
    import importlib

    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(f"{module_name} 缺少公开成员 {missing}")
    return module


def _domain():
    return _require(
        "project_rag.domain",
        (
            "STRATEGIES",
            "STRATEGY_NAMES",
            "QueryRewrite",
            "StrategyPrediction",
            "direct_rewrite",
            "label_of",
            "strategy_of",
        ),
    )


def _plan_rewrite():
    return _require("project_rag.application", ("plan_rewrite",)).plan_rewrite


def _retrieve_many():
    return _require("project_rag.application", ("retrieve_many",)).retrieve_many


# ── 假件（只在需要"看到调了什么"的地方自建;通用的那几个用 _fakes）─────────────
class _FixedClassifier:
    """按给定标签回答的分类器。``error`` = 它坏掉（模型没加载 / 输出越界都走这条）。"""

    def __init__(self, label: int, *, confidence: float = 0.9, error: Exception | None = None):
        self.label = label
        self.confidence = confidence
        self.error = error
        self.calls = 0

    def classify(self, query: str):
        self.calls += 1
        if self.error is not None:
            raise self.error
        from project_rag.domain import StrategyPrediction

        return StrategyPrediction(
            label=self.label, confidence=self.confidence, decided_by="model"
        )


class _FixedRewriter:
    """定了结论的改写器。用来单独验检索侧（不让 LLM 掺进来）。"""

    def __init__(self, queries, *, strategy: RewriteStrategy = "subquery"):
        self.queries: tuple[str, ...] = tuple(queries)
        self.strategy: RewriteStrategy = strategy
        self.calls: list[str] = []

    def rewrite(self, query: str):
        from project_rag.domain import QueryRewrite

        self.calls.append(query)
        return QueryRewrite(
            original=query, strategy=self.strategy, queries=self.queries
        )


class _RecordingEmbedder:
    """假嵌入器:**记下每一次嵌入的文本** —— 「检索用的是改写后那句话」就是靠它证的。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    @property
    def dim(self) -> int:
        return 2

    def embed(self, texts):
        from project_rag.domain import Embedding

        self.texts.extend(texts)
        return [Embedding(dense=(1.0, 0.0)) for _ in texts]


class _BatchStore(FakeVectorStore):
    """按**第几次检索**给不同命中的假向量库（子查询那几条要用）。

    继承 ``FakeVectorStore`` 而不是只实现 ``search``:端口是完整的 ``VectorStore``,
    少一个方法就通不过类型检查 —— 而"为了省事只实现用到的那一个"正是让假件
    与真端口悄悄分家的写法。
    """

    def __init__(self, batches) -> None:
        super().__init__()
        self.batches = [list(batch) for batch in batches]
        self.calls = 0

    def search(self, query_embedding, *, k, source=None):
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        return list(batch)


def _hit(chunk_id: str, score: float):
    """造一条命中（与 check_m3_rag 同一套写法）。``parent_id`` 留空 = 不回父块。"""
    from project_rag.domain import Chunk, RetrievedChunk

    return RetrievedChunk(chunk=Chunk(chunk_id, f"文本{chunk_id}", "ai", "d1"), score=score)


def _reply(queries) -> str:
    return json.dumps({"queries": list(queries)}, ensure_ascii=False)


class _Catcher(logging.Handler):
    """收账本那一行 INFO。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 由 logging 调
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def _capture():
    logger = logging.getLogger("project_rag.observability.latency")
    catcher = _Catcher()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(catcher)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 断言用的行不该漏进 root 的 handler
    try:
        yield catcher
    finally:
        logger.removeHandler(catcher)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _fields_of(lines: list[str], label: str) -> dict[str, str]:
    for line in lines:
        if not line.startswith("latency "):
            continue
        fields: dict[str, str] = {}
        for token in line.split(" ")[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                raise AssertionError(f"字段 {token!r} 不是 key=value 形状:{line!r}")
            fields[key] = value
        if fields.get("label") == label:
            return fields
    raise AssertionError(f"没有 label={label} 的账本行,只收到 {lines!r}")


def _cli(argv: list[str], deps):
    """输入:命令行 + 依赖。行为:进程内跑一次 CLI 并抓输出。输出:(退出码, 标准输出)。"""
    from project_rag.cli.main import cli

    buffer = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli(argv, deps=deps)
        except SystemExit as exc:  # argparse 的用法错误/--help 走这条
            code = int(exc.code or 0)
    return code, buffer.getvalue()


def _toplevel_imports(path: Path) -> list[str]:
    """输入:源码路径。行为:只取**模块级** import。输出:模块名列表。

    只扫模块级是刻意的:这条检查要证的是"重依赖被推迟到了函数体里",
    函数体里的 import 不算数。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            out.append(node.module or "")
    return out


def _all_absolute_imports(path: Path) -> list[str]:
    """输入:源码路径。行为:取全部绝对 import(含函数体内的)。输出:模块名列表。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            out.append(node.module or "")
    return out


# ══ 1. 领域类型 ═══════════════════════════════════════════════════════════════
@check("领域：4 个策略名与标签编号一一对应（索引即标签），顺序就是训练数据的顺序")
def _() -> str:
    domain = _domain()
    want = ("direct", "hypothetical", "subquery", "denoise")
    if tuple(domain.STRATEGIES) != want:
        raise AssertionError(
            f"STRATEGIES 应为 {want}，实际 {domain.STRATEGIES}；"
            "它同时是模型输出 logits 的列序 —— 改了顺序必须重训"
        )
    if set(domain.STRATEGY_NAMES) != set(want):
        raise AssertionError(f"STRATEGY_NAMES 的键应覆盖 4 个策略，实际 {domain.STRATEGY_NAMES}")
    for label, name in enumerate(want):
        if domain.strategy_of(label) != name:
            raise AssertionError(f"strategy_of({label}) 应为 {name}，实际 {domain.strategy_of(label)}")
        if domain.label_of(name) != label:
            raise AssertionError(f"label_of({name}) 应为 {label}，实际 {domain.label_of(name)}")
    return f"{want} ↔ 0~3 双向可达"


@check("领域：越界标签当场抛，不夹到 0（夹一下会把「模型输出 7」表现成「直接检索」）")
def _() -> str:
    domain = _domain()
    for bad in (-1, 4, 99):
        try:
            domain.strategy_of(bad)
        except ValueError:
            continue
        raise AssertionError(f"strategy_of({bad}) 该抛 ValueError，却给了 {domain.strategy_of(bad)!r}")
    try:
        domain.label_of("不存在的策略")
    except ValueError:
        return "越界与未知策略都抛 ValueError"
    raise AssertionError("label_of('不存在的策略') 该抛 ValueError")


@check("领域：direct_rewrite 造出的对象满足两条不变式，且能带上置信度")
def _() -> str:
    domain = _domain()
    plan = domain.direct_rewrite("  你好  ", note="空问题", confidence=0.42)
    if plan.queries != ("  你好  ",):
        raise AssertionError(f"改写的原话必须逐字保留，实际 {plan.queries!r}")
    if plan.strategy != "direct" or plan.label != 0:
        raise AssertionError(f"direct_rewrite 应给出 direct/0，实际 {plan.strategy}/{plan.label}")
    if plan.changed:
        raise AssertionError("direct_rewrite 的结论是「没改」，changed 该是 False")
    if plan.confidence != 0.42 or plan.note != "空问题":
        raise AssertionError("降级时已有的置信度与原因不该被丢掉")
    return "逐字保留 + direct/0 + changed=False + 置信度透传"


@check("领域：QueryRewrite 的不变式在类型上成立（direct ⟺ queries == (original,)）")
def _() -> str:
    domain = _domain()
    direct = domain.QueryRewrite(original="q", strategy="direct", queries=("q",))
    if not (direct.strategy == "direct" and direct.queries == (direct.original,)):
        raise AssertionError("direct 的定义就是「一个字都不改」")
    changed = domain.QueryRewrite(original="q", strategy="denoise", queries=("q 去噪后",))
    if not changed.changed:
        raise AssertionError("换了句子就该报 changed=True")
    return "direct/changed 两个语义位都按定义工作"


# ══ 2. 提示词 ═════════════════════════════════════════════════════════════════
@check("提示词：三种策略各渲染一次，带策略名、要求与用户原话；json 的大括号没被 format 吃掉")
def _() -> str:
    module = _require("project_rag.application.prompts", ("rewrite_prompt",))
    for strategy, name in (("hypothetical", "假设答案"), ("subquery", "子查询分解"), ("denoise", "去噪改写")):
        text = module.rewrite_prompt(strategy, QUESTION, max_subqueries=2)
        if name not in text:
            raise AssertionError(f"{strategy} 的提示词里该出现「{name}」，实际没有")
        if QUESTION not in text:
            raise AssertionError(f"{strategy} 的提示词里没带用户原话")
        if '{"queries"' not in text:
            raise AssertionError(
                f"{strategy} 的提示词里 json 示例的大括号被 .format 吃掉了：{text[-160:]!r}"
            )
    # 子问题的条数上限必须真的渲染进去,否则模型不知道能拆几句
    if "2" not in module.rewrite_prompt("subquery", QUESTION, max_subqueries=2):
        raise AssertionError("max_subqueries 没渲染进子查询那条提示词")
    return "三条策略都能渲染，json 示例与条数上限都在"


@check("提示词：direct 没有提示词，且当场抛（直接检索不该调模型）")
def _() -> str:
    module = _require("project_rag.application.prompts", ("rewrite_prompt",))
    try:
        module.rewrite_prompt("direct", QUESTION)
    except ValueError:
        return "direct → ValueError"
    raise AssertionError("direct 该抛 ValueError：那条路的定义就是「一个字都不改」")


# ══ 3. 用例 plan_rewrite ══════════════════════════════════════════════════════
@check("用例 直接检索：一个字都不改，而且**一次模型都不调**")
def _() -> str:
    from _fakes import FakeLLM

    llm = FakeLLM(reply="不该被调用")
    plan = _plan_rewrite()(QUESTION, classifier=_FixedClassifier(LABEL_OF["direct"]), llm=llm)
    if plan.queries != (QUESTION,):
        raise AssertionError(f"直接检索应原样返回，实际 {plan.queries!r}")
    if llm.calls:
        raise AssertionError(f"直接检索多调了 {llm.calls} 次模型 —— 白花钱还会把原话改脏")
    if plan.decided_by != "model" or plan.confidence is None:
        raise AssertionError("判成 0 类时决策者与置信度都该保留")
    return "queries == (原话,)，llm.calls == 0"


@check("用例 假设答案：用模型给的假设答案当检索语句，且只取一句")
def _() -> str:
    from _fakes import FakeLLM

    llm = FakeLLM(reply=_reply(["机器学习在图像、语音与自然语言处理上的典型应用", "第二句应当被丢掉"]))
    plan = _plan_rewrite()(QUESTION, classifier=_FixedClassifier(LABEL_OF["hypothetical"]), llm=llm)
    if plan.strategy != "hypothetical":
        raise AssertionError(f"策略应是 hypothetical，实际 {plan.strategy}")
    if plan.queries != ("机器学习在图像、语音与自然语言处理上的典型应用",):
        raise AssertionError(f"假设答案那条路只该取一句，实际 {plan.queries!r}")
    if llm.calls != 1:
        raise AssertionError(f"该调一次模型，实际 {llm.calls}")
    if "假设答案" not in llm.prompts[0]:
        raise AssertionError("提示词里没有「假设答案」四个字，八成发错了模板")
    return "一句假设答案 + 一次调用"


@check("用例 子查询分解：重复句去掉，且截到 max_subqueries 为止")
def _() -> str:
    from _fakes import FakeLLM

    # ① 去重:4 条里第 1、2 条一模一样 → 只该留 3 条
    dedup = _plan_rewrite()(
        QUESTION,
        classifier=_FixedClassifier(LABEL_OF["subquery"]),
        llm=FakeLLM(reply=_reply(["装饰器和闭包有什么区别？", "装饰器和闭包有什么区别？", "Milvus 支持哪些索引？"])),
        max_subqueries=3,
    )
    if dedup.queries != ("装饰器和闭包有什么区别？", "Milvus 支持哪些索引？"):
        raise AssertionError(
            f"重复句该被去掉，实际 {dedup.queries!r}；"
            "重复句会让同一路检索白跑一次、融合分还算重"
        )
    # ② 上限:4 条互不相同 → 只该留前 2 条(句数直接乘在检索耗时上)
    capped = _plan_rewrite()(
        QUESTION,
        classifier=_FixedClassifier(LABEL_OF["subquery"]),
        llm=FakeLLM(reply=_reply(["一？", "二？", "三？", "四？"])),
        max_subqueries=2,
    )
    if capped.queries != ("一？", "二？"):
        raise AssertionError(f"该截到上限 2 条，实际 {capped.queries!r}")
    return "去重 + 限 2 条都生效"


@check("用例 去噪改写：只取一句（这一步做减法，不摊薄检索）")
def _() -> str:
    from _fakes import FakeLLM

    llm = FakeLLM(reply=_reply(["Python 的 GIL 是什么", "顺带一句多余的"]))
    plan = _plan_rewrite()(QUESTION, classifier=_FixedClassifier(LABEL_OF["denoise"]), llm=llm)
    if plan.queries != ("Python 的 GIL 是什么",):
        raise AssertionError(f"去噪该只留一句，实际 {plan.queries!r}")
    return "一句干净语句"


@check("用例 容错：模型把 json 包在 ``` 里、或前后带客套话，都还能解析出来")
def _() -> str:
    from _fakes import FakeLLM

    fenced = "好的，这是改写结果：\n```json\n" + _reply(["去噪后的句子"]) + "\n```\n希望有帮助！"
    plan = _plan_rewrite()(
        QUESTION, classifier=_FixedClassifier(LABEL_OF["denoise"]), llm=FakeLLM(reply=fenced)
    )
    if plan.queries != ("去噪后的句子",):
        raise AssertionError(f"围栏与客套话该被剥掉，实际 {plan.queries!r}")
    # 裸数组也要认(模型图省事时不带 queries 键)
    bare = _plan_rewrite()(
        QUESTION, classifier=_FixedClassifier(LABEL_OF["denoise"]), llm=FakeLLM(reply='["裸数组也认"]')
    )
    if bare.queries != ("裸数组也认",):
        raise AssertionError(f"裸数组该被认出来，实际 {bare.queries!r}")
    return "```json 围栏 + 客套话 + 裸数组都能解析"


class _BrokenLLM(FakeLLM):
    """``stream`` 直接抛 —— 模拟 provider 连不上。"""

    def stream(self, messages) -> Iterator[LlmDelta]:
        raise ConnectionError("provider 连不上")


@check("用例 降级（五种坏法）：一律退回原话，queries 永不为空，并留下原因")
def _() -> str:
    cases = [
        ("分类器抛错", _FixedClassifier(0, error=RuntimeError("权重没加载")), FakeLLM(reply="x")),
        ("越界标签", _FixedClassifier(7), FakeLLM(reply=_reply(["不该用到"]))),
        ("模型调用抛错", _FixedClassifier(LABEL_OF["denoise"]), _BrokenLLM()),
        ("模型吐了一堆非 json", _FixedClassifier(LABEL_OF["denoise"]), FakeLLM(reply="我觉得可以这样问")),
        ("模型给了空数组", _FixedClassifier(LABEL_OF["denoise"]), FakeLLM(reply=_reply([]))),
    ]
    for name, classifier, llm in cases:
        plan = _plan_rewrite()(QUESTION, classifier=classifier, llm=llm)
        if plan.queries != (QUESTION,):
            raise AssertionError(f"{name}：该退回原话，实际 {plan.queries!r}")
        if plan.strategy != "direct" or plan.decided_by != "fallback":
            raise AssertionError(
                f"{name}：该给出 direct/fallback，实际 {plan.strategy}/{plan.decided_by}"
            )
        if not plan.note:
            raise AssertionError(f"{name}：降级必须留原因（note 为空），否则线上查不出改写没生效")
    return f"{len(cases)} 种坏法都退回原话且留了原因"


@check("用例 没配 LLM：非 0 类也只能退回原话（而不是抛给调用方）")
def _() -> str:
    plan = _plan_rewrite()(QUESTION, classifier=_FixedClassifier(LABEL_OF["hypothetical"]), llm=None)
    if plan.queries != (QUESTION,) or plan.note != "没配 LLM":
        raise AssertionError(f"该退回原话并说明原因，实际 {plan.queries!r} / {plan.note!r}")
    if plan.confidence is None:
        raise AssertionError("判定已经做过了，置信度不该因为改写那步没做而丢掉")
    return "退回原话 + 保留置信度"


@check("用例 模型把原话原样吐回来：归成直接检索（让那条不变式**无条件**成立）")
def _() -> str:
    plan = _plan_rewrite()(
        QUESTION,
        classifier=_FixedClassifier(LABEL_OF["denoise"]),
        llm=FakeLLM(reply=_reply([QUESTION])),
    )
    if plan.strategy != "direct" or plan.queries != (QUESTION,):
        raise AssertionError(
            f"改写结果与原话相同时该归成 direct，实际 {plan.strategy}/{plan.queries!r}；"
            "否则账上写着「去噪改写」而检索用的是原话 —— 账与行为不一致最难查"
        )
    if plan.decided_by != "model" or plan.confidence is None:
        raise AssertionError("它确实判过也调过模型，decided_by/置信度该留着")
    return "原样吐回 → direct（不变式无条件成立）"


@check("用例 空白与超长：原话压成单空格；单句截到 max_query_chars")
def _() -> str:
    from _fakes import FakeLLM

    plan = _plan_rewrite()("  装饰器  是什么\n？ ", classifier=_FixedClassifier(0))
    if plan.original != "装饰器 是什么 ？":
        raise AssertionError(f"原话该压成单空格，实际 {plan.original!r}")
    long_one = _plan_rewrite()(
        "问题",
        classifier=_FixedClassifier(LABEL_OF["denoise"]),
        llm=FakeLLM(reply=_reply(["啊" * 500])),
        max_query_chars=50,
    )
    if len(long_one.queries[0]) != 50:
        raise AssertionError(f"超长句该截到 50 字，实际 {len(long_one.queries[0])}")
    empty = _plan_rewrite()("   ", classifier=_FixedClassifier(0))
    if empty.queries != ("",):
        raise AssertionError("空问题也该给一条（空串），而不是给空元组")
    return "空白压缩 + 截断 + 空问题不产出空元组"


@check("用例 低置信阈值：默认 0 = 不兜底；开起来时退回原话（实测回退会掉点，所以默认关）")
def _() -> str:
    from _fakes import FakeLLM

    classifier = _FixedClassifier(LABEL_OF["denoise"], confidence=0.3)
    llm = FakeLLM(reply=_reply(["去噪后的句子"]))
    default = _plan_rewrite()(QUESTION, classifier=classifier, llm=llm)
    if default.strategy != "denoise" or default.queries != ("去噪后的句子",):
        raise AssertionError(
            f"阈值默认 0 时不该兜底 —— 低置信回退是实测有害的，默认必须关，"
            f"实际 {default.strategy}/{default.queries!r}"
        )
    gated = _plan_rewrite()(
        QUESTION, classifier=classifier, llm=llm, confidence_threshold=0.5
    )
    if gated.strategy != "direct" or gated.queries != (QUESTION,):
        raise AssertionError(f"阈值 0.5 时 0.3 该退回原话，实际 {gated.strategy}/{gated.queries!r}")
    if gated.confidence != 0.3:
        raise AssertionError("兜底时原判定置信度该留着（排查要看「模型其实判得挺准」这条线索）")
    return "默认不兜底；开了就退回原话并保留置信度"


# ══ 4. 检索侧 ═════════════════════════════════════════════════════════════════
@check("检索 retrieve_many([q]) == retrieve(q)：开不开改写不改变「这一句本来会召回什么」")
def _() -> str:
    from _fakes import FakeKeywordIndex, FakeReranker, FakeVectorStore

    from project_rag.application import retrieve
    from project_rag.domain import RetrievalOptions

    hits = [_hit("c1", 0.9), _hit("c2", 0.5), _hit("c3", 0.3)]
    options = RetrievalOptions(k=3, max_context=2)
    kwargs = {
        "embedder": _RecordingEmbedder(),
        "store": FakeVectorStore(hits),
        "keyword_index": FakeKeywordIndex(hits),
        "reranker": FakeReranker(),
        "options": options,
    }
    one = retrieve("装饰器是什么", **kwargs)
    many = _retrieve_many()(["装饰器是什么"], **kwargs)
    shape = lambda items: [(c.chunk.chunk_id, round(c.score, 6), c.routes) for c in items]  # noqa: E731
    if shape(one) != shape(many):
        raise AssertionError(f"单句两边必须逐字等价：retrieve={shape(one)}，retrieve_many={shape(many)}")
    return f"单句等价：{shape(one)}"


@check("检索 多句：按 chunk_id 去重留最高分，routes 取并集，降序返回")
def _() -> str:
    from project_rag.domain import RetrievalOptions

    store = _BatchStore(
        [
            [_hit("c1", 0.9), _hit("c2", 0.5)],
            [_hit("c2", 0.8), _hit("c3", 0.7)],
        ]
    )
    citations = _retrieve_many()(
        ["第一句", "第二句"],
        embedder=_RecordingEmbedder(),
        store=store,
        options=RetrievalOptions(k=2),
    )
    got = [(c.chunk.chunk_id, round(c.score, 6), c.routes, c.index) for c in citations]
    want = [("c1", 0.9, ("vector",), 1), ("c2", 0.8, ("vector",), 2), ("c3", 0.7, ("vector",), 3)]
    if got != want:
        raise AssertionError(
            f"合并结果该是 {want}（c2 被两句都召回到 → 留 0.8 且只出现一次），实际 {got}"
        )
    if store.calls != 2:
        raise AssertionError(f"两句该各检索一次，实际 {store.calls} 次")
    return "c2 去重留高分；编号从 1 连续"


@check("检索 空入参：给空表就给空结果，不拿原话顶上（调用方给了空表就是它那边出了问题）")
def _() -> str:
    from project_rag.domain import RetrievalOptions

    for queries in ([], ["", "   "], None):
        citations = _retrieve_many()(
            queries,
            embedder=_RecordingEmbedder(),
            store=_BatchStore([[_hit("c1", 1.0)]]),
            options=RetrievalOptions(),
        )
        if citations != ():
            raise AssertionError(f"queries={queries!r} 时该返回空序列，实际 {citations!r}")
    return "空表 / 空白 / None 都返回空序列"


@check("链路 改写只改检索：嵌入用的是改写后的话，**回答用的仍是用户原话**")
def _() -> str:
    from _fakes import FakeLLM, FakeReranker

    from project_rag.application import stream_rag
    from project_rag.domain import RetrievalOptions

    embedder = _RecordingEmbedder()
    llm = FakeLLM(reply="答案 [1]")
    rewriter = _FixedRewriter(["拆开后的子问题一", "拆开后的子问题二"])
    events = list(
        stream_rag(
            QUESTION,
            embedder=embedder,
            store=_BatchStore([[_hit("c1", 0.9)], [_hit("c2", 0.8)]]),
            llm=llm,
            reranker=FakeReranker(),
            options=RetrievalOptions(k=1),
            rewriter=rewriter,
        )
    )
    if rewriter.calls != [QUESTION]:
        raise AssertionError(f"改写器该收到用户原话，实际 {rewriter.calls}")
    if embedder.texts != ["拆开后的子问题一", "拆开后的子问题二"]:
        raise AssertionError(f"两句改写都该被检索，实际嵌入的是 {embedder.texts}")
    prompt = llm.prompts[0]
    if QUESTION not in prompt:
        raise AssertionError("生成答案的提示词里必须是**用户原话**，否则模型会去回答假设答案")
    if "拆开后的子问题" in prompt:
        raise AssertionError("改写后的句子不该进生成答案的提示词")
    if events[-1].kind != "done":
        raise AssertionError("帧协议：恰好一帧 done 收尾")
    return "两份子问题都检索了；prompt 里只有原话"


@check("链路 不配改写器：一次都不碰改写，检索用原话（关掉它 == 没有这个功能）")
def _() -> str:
    from _fakes import FakeLLM, FakeReranker

    from project_rag.application import stream_rag
    from project_rag.domain import RetrievalOptions

    embedder = _RecordingEmbedder()
    streamed = list(
        stream_rag(
            QUESTION,
            embedder=embedder,
            store=_BatchStore([[_hit("c1", 0.9)]]),
            llm=FakeLLM(reply="答案 [1]"),
            reranker=FakeReranker(),
            options=RetrievalOptions(k=1),
        )
    )
    if embedder.texts != [QUESTION]:
        raise AssertionError(f"没配改写时该直接检索原话，实际 {embedder.texts}")
    if streamed[-1].citations == ():
        raise AssertionError("这条路上检索该照常命中")
    return "embedder 收到原话"


@check("链路 改写器回了空表：兜最后一道，退回原话（空表一路传下去就是「假装知识库没有」）")
def _() -> str:
    from _fakes import FakeLLM, FakeReranker

    from project_rag.application import stream_rag
    from project_rag.domain import RetrievalOptions

    embedder = _RecordingEmbedder()
    list(
        stream_rag(
            QUESTION,
            embedder=embedder,
            store=_BatchStore([[_hit("c1", 0.9)]]),
            llm=FakeLLM(reply="答案 [1]"),
            reranker=FakeReranker(),
            options=RetrievalOptions(k=1),
            rewriter=_FixedRewriter([]),
        )
    )
    if embedder.texts != [QUESTION]:
        raise AssertionError(f"违约的空 queries 该退回原话，实际检索了 {embedder.texts}")
    return "空 queries → 原话"


# ══ 5. 账本口径 ═══════════════════════════════════════════════════════════════
@check("账本：有 rewrite_ms 这一格；没改写时是 '-'（没量到与量到 0 必须分得开）")
def _() -> str:
    timing = _require("project_rag.observability", ("StreamTiming",)).StreamTiming(label="probe")
    if timing.values().get("rewrite_ms") != "-":
        raise AssertionError(f"没打过点该是 '-'，实际 {timing.values().get('rewrite_ms')!r}")
    return "字段在，默认 '-'"


@check("账本：改写之后 embed/retrieve 从**改写结束**算起（否则一次 LLM 改写会污染检索耗时）")
def _() -> str:
    timing = _require("project_rag.observability", ("StreamTiming",)).StreamTiming(label="probe")
    base = timing.started
    timing.marks["rewrite"] = base + 0.10
    timing.marks["embed"] = base + 0.30
    timing.marks["retrieve"] = base + 0.35
    values = timing.values()
    if values["rewrite_ms"] != "100.0":
        raise AssertionError(f"rewrite_ms 该是从开账到改写结束的 100.0，实际 {values['rewrite_ms']}")
    if values["embed_ms"] != "200.0":
        raise AssertionError(f"embed_ms 该从改写结束算起 = 200.0，实际 {values['embed_ms']}")
    if values["retrieve_ms"] != "250.0":
        raise AssertionError(f"retrieve_ms 该从改写结束算起 = 250.0，实际 {values['retrieve_ms']}")
    return "rewrite=100 / embed=200 / retrieve=250（都从改写结束算）"


@check("账本 链路：真的开了改写时，那条 rag 账上 rewrite_ms 有值")
def _() -> str:
    from _fakes import FakeLLM, FakeReranker

    from project_rag.application import stream_rag
    from project_rag.domain import RetrievalOptions

    with _capture() as catcher:
        list(
            stream_rag(
                QUESTION,
                embedder=_RecordingEmbedder(),
                store=_BatchStore([[_hit("c1", 0.9)]]),
                llm=FakeLLM(reply="答案 [1]"),
                reranker=FakeReranker(),
                options=RetrievalOptions(k=1),
                rewriter=_FixedRewriter(["改写后的一句"]),
            )
        )
    fields = _fields_of(catcher.lines, "rag")
    if fields["rewrite_ms"] == "-":
        raise AssertionError("开了改写却量不到 rewrite_ms —— 打点没落在 _rag_frames 里")
    return f"rewrite_ms={fields['rewrite_ms']}"


# ══ 6. 配置与组装 ═════════════════════════════════════════════════════════════
@check("配置：rewrite 段默认关；三个旋钮的边界当场抛")
def _() -> str:
    from project_rag.config import RewriteConfig

    default = RewriteConfig()
    if default.enabled:
        raise AssertionError("默认必须关：每一步都要多花一次 LLM 调用，收益取决于语料")
    if default.confidence_threshold != 0.0:
        raise AssertionError("低置信回退实测有害，默认阈值必须是 0")
    for name, build in (
        ("max_subqueries=0", lambda: RewriteConfig(max_subqueries=0)),
        ("max_query_chars=8", lambda: RewriteConfig(max_query_chars=8)),
        ("confidence_threshold=1.0", lambda: RewriteConfig(confidence_threshold=1.0)),
    ):
        try:
            build()
        except ValueError:
            continue
        raise AssertionError(f"{name} 该被配置层拦下")
    return "默认关 + 三个越界值都抛"


@check("配置：Settings 读得到 rewrite 段（config.yaml 与配置类两边都在）")
def _() -> str:
    from project_rag.config import get_settings

    settings = get_settings()
    rewrite = settings.rewrite
    if rewrite.adapter_dir != "logs/lora/run1/lora_adapter":
        raise AssertionError(f"adapter_dir 默认值不对：{rewrite.adapter_dir}")
    if rewrite.classifier not in ("lora", "rule"):
        raise AssertionError(f"classifier 只该是 lora / rule，实际 {rewrite.classifier!r}")
    if rewrite.max_length != 64:
        raise AssertionError("max_length 必须与训练时一致（tools/lora/train.py 是 64）")
    return f"enabled={rewrite.enabled} classifier={rewrite.classifier} device={rewrite.device}"


@check("组合根：配置关着时不装配改写器（不接 = 与「没这功能」逐字等价）")
def _() -> str:
    from project_rag.cli import Deps

    if Deps().query_rewriter_factory is not None:
        raise AssertionError("Deps 默认必须是 None：注入式调用不该被塞一个改写器")
    from project_rag.cli.deps import build_real_deps

    built = build_real_deps()
    if built.query_rewriter_factory is not None:
        raise AssertionError(
            "config.yaml 里 rewrite.enabled 是 false，build_real_deps 不该接上改写器"
        )
    return "Deps() 与 build_real_deps() 都是 None"


@check("组合根：装配失败退化成原样检索（而不是抛出去把问答搞挂），并且留着原因")
def _() -> str:
    from project_rag.cli.deps import build_query_rewriter
    from project_rag.config import RewriteConfig

    def broken():
        raise FileNotFoundError("找不到 LoRA 适配器目录")

    plan = build_query_rewriter(broken, lambda: None, RewriteConfig()).rewrite(QUESTION)
    if plan.queries != (QUESTION,):
        raise AssertionError(f"装配失败该退回原话，实际 {plan.queries!r}")
    if plan.decided_by != "fallback" or "FileNotFoundError" not in plan.note:
        raise AssertionError(
            f"该留下 fallback 与原因（否则「改写一直没生效」看起来就是「效果一般」），"
            f"实际 {plan.decided_by}/{plan.note!r}"
        )
    # 装配成功那条路也要走一遍:它必须真的调分类器,而不是也退化成原样检索
    from project_rag.adapters.strategy_rule import RuleStrategyClassifier

    ok = build_query_rewriter(RuleStrategyClassifier, lambda: None, RewriteConfig())
    if ok.rewrite("Python 装饰器有什么用？").decided_by == "fallback":
        raise AssertionError("装配成功时不该是 fallback —— 那样检查就永远绿")
    return "装配失败 → 原话 + 原因；装配成功 → 真的判了类"


@check("形状：组合根交出来的东西**真的是** QueryRewriter，且 stream_rag 能直接吃下去")
def _() -> str:
    # 端口 QueryRewriter 声明的是一**方法**（rewrite(query)），不是一个可调用对象。
    # 这两者在 Python 里都"能调一下试试"，所以写错了不会当场报错 ——
    # 只会在 rewriter.rewrite(q) 那一行炸，而且炸在请求路径上（写这一条时真踩了）。
    from _fakes import FakeLLM, FakeReranker

    from project_rag.adapters.strategy_rule import RuleStrategyClassifier
    from project_rag.application import stream_rag
    from project_rag.cli.deps import build_query_rewriter
    from project_rag.config import RewriteConfig
    from project_rag.domain import RetrievalOptions

    def broken():
        raise RuntimeError("没有权重")

    made = {
        "装配成功": build_query_rewriter(RuleStrategyClassifier, lambda: None, RewriteConfig()),
        "装配失败": build_query_rewriter(broken, lambda: None, RewriteConfig()),
    }
    for name, rewriter in made.items():
        if not callable(getattr(rewriter, "rewrite", None)):
            raise AssertionError(
                f"{name}：交出来的东西没有 .rewrite 方法（形状是 {type(rewriter).__name__}）—— "
                "端口声明的是方法，偏函数/闭包不算"
            )
    # 端到端把组合根的产物喂给用例:形状不对的话这里会 AttributeError
    embedder = _RecordingEmbedder()
    events = list(
        stream_rag(
            QUESTION,
            embedder=embedder,
            store=_BatchStore([[_hit("c1", 0.9)]]),
            llm=FakeLLM(reply="答案 [1]"),
            reranker=FakeReranker(),
            options=RetrievalOptions(k=1),
            rewriter=made["装配成功"],
        )
    )
    if embedder.texts != [QUESTION]:
        raise AssertionError(f"规则兜底把这条判成直接检索，检索该用原话，实际 {embedder.texts}")
    if events[-1].kind != "done":
        raise AssertionError("帧协议：恰好一帧 done 收尾")
    return "两种装配结果都有 .rewrite，且 stream_rag 能直接消费"


# ══ 7. 分层与命令 ═════════════════════════════════════════════════════════════
@check("分层：application/query_rewrite.py 只依赖 标准库 + domain/ports/observability")
def _() -> str:
    path = SRC / "application" / "query_rewrite.py"
    if not path.exists():
        raise AssertionError("src/project_rag/application/query_rewrite.py 还没落地")
    bad: list[str] = []
    for module in _all_absolute_imports(path):
        root = module.split(".")[0]
        if root == "project_rag":
            if not module.startswith(("project_rag.domain", "project_rag.ports", "project_rag.observability")):
                bad.append(module)
        elif root not in sys.stdlib_module_names:
            bad.append(f"{module}（第三方 SDK 必须藏在适配器里）")
    if bad:
        raise AssertionError(f"用例违反依赖方向：{bad}")
    return "只依赖 标准库 + domain/ports/observability"


@check("分层：domain/rewrite.py 不 import 任何第三方（domain 是依赖图的叶子）")
def _() -> str:
    path = SRC / "domain" / "rewrite.py"
    if not path.exists():
        raise AssertionError("src/project_rag/domain/rewrite.py 还没落地")
    bad = [
        module
        for module in _all_absolute_imports(path)
        if module.split(".")[0] not in sys.stdlib_module_names
    ]
    if bad:
        raise AssertionError(f"domain 里出现了第三方依赖：{bad}")
    return "只用标准库"


@check("分层：torch / transformers / peft 只在函数体里 import（--help 不该替它们付时间）")
def _() -> str:
    path = SRC / "adapters" / "strategy_lora.py"
    if not path.exists():
        raise AssertionError("src/project_rag/adapters/strategy_lora.py 还没落地")
    heavy = {"torch", "transformers", "peft"}
    top = {module.split(".")[0] for module in _toplevel_imports(path)}
    leaked = sorted(top & heavy)
    if leaked:
        raise AssertionError(
            f"模块级 import 了 {leaked} —— 它们该待在 __init__ 里（秒级 + 上 G 内存）"
        )
    used = {module.split(".")[0] for module in _all_absolute_imports(path)}
    if not (used & heavy):
        raise AssertionError("这个适配器压根没用到 torch/transformers/peft —— 文件是不是放错了")
    return "重依赖全部推迟到 __init__ 内"


@check("分层：adapters/__init__.py 不 re-export 微调分类器（它和 embedder_bge_m3 同一档重量）")
def _() -> str:
    text = (SRC / "adapters" / "__init__.py").read_text(encoding="utf-8")
    if "strategy_lora" in text or "LoraStrategyClassifier" in text:
        raise AssertionError(
            "adapters/__init__.py 里出现了 strategy_lora —— 它要按完整路径 import，"
            "否则每一次 `from project_rag.adapters import ...` 都得先加载 torch"
        )
    return "包入口里没有它"


@check("命令：--help 列出 rewrite；缺 -q 退出码 2；配置关着时退出码 1 并点名缺哪个工厂")
def _() -> str:
    from project_rag.cli import Deps

    code, _out = _cli(["rewrite", "--help"], Deps())
    if code != 0:
        raise AssertionError(f"rewrite --help 退出码该是 0，实际 {code}")
    missing, _ = _cli(["rewrite"], Deps())
    if missing != 2:
        raise AssertionError(f"缺 -q 该是用法错误退出码 2，实际 {missing}")
    offline, _ = _cli(["rewrite", "-q", QUESTION], Deps())
    if offline != 1:
        raise AssertionError(f"没装配改写器时该 fail-fast 退出码 1，实际 {offline}")
    return "help 0 / 缺参 2 / 没装配 1"


@check("命令：装配了改写器时打印策略、置信度、原话与每一条检索语句")
def _() -> str:
    from project_rag.cli import Deps

    deps = Deps(query_rewriter_factory=lambda: _FixedRewriter(["拆开一", "拆开二"]))
    code, out = _cli(["rewrite", "-q", QUESTION], deps)
    if code != 0:
        raise AssertionError(f"退出码该是 0，实际 {code}")
    for needle in ("subquery", "子查询分解", "标签 2", QUESTION, "拆开一", "拆开二"):
        if needle not in out:
            raise AssertionError(f"输出里少了 {needle!r}：\n{out}")
    return "策略 / 标签 / 原话 / 检索语句都在"


def main() -> int:
    return report(
        "验收：查询改写（FAQ 未命中 → 向量检索之间那一步）",
        hint=(
            "这一份零容器零模型零网络。真模型那条路要另跑：\n"
            "  docker run --rm --gpus all -v \"$PWD\":/work -w /work --entrypoint python \\\n"
            "    project-rag:0.1.0 tools/lora/infer.py --adapter logs/lora/run1/lora_adapter --test"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

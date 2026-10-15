#!/usr/bin/env python
"""M2 验收：FAQ 通道（MySQL 存问答对 → 相似匹配 → 命中直答 / 未命中 LLM 兜底）。

用法::

    uv run python scripts/acceptance/check_m2_faq.py

本脚本是 M2 契约的**唯一权威**（`docs/STATUS.md` §8 只留设计理由，不重复规格）。

公开契约（import 路径本身也是承诺）::

    from project_rag.cli import cli, Deps
    cli(argv=None, *, deps: Deps | None = None) -> int

``deps=None`` = 真实装配（读 Settings、装日志、连 MySQL/Redis）；传入 ``deps`` =
不读 .env、不装日志、不联网，所以整条 CLI 链路可测。Deps 字段全是**工厂**，保持
「用不到就不造」的惰性；注入时命令只准调用自己需要的工厂，需要而没给 → fail-fast 并点名::

    llm_factory:       Callable[[], LLMClient]    | None = None
    faq_store_factory: Callable[[], FaqStore]     | None = None
    matcher_factory:   Callable[[], TextMatcher]  | None = None
    cache_factory:     Callable[[], Cache | None] | None = None   # 返回 None = 无缓存
    faq_threshold:     float = 0.85
    # M3 起:知识库通道的四个工厂。字段名也是契约 —— 往 Deps 加字段就得同步本脚本。
    embedder_factory:      Callable[[], Embedder]     | None = None
    vector_store_factory:  Callable[[], VectorStore]  | None = None
    keyword_index_factory: Callable[[], KeywordIndex] | None = None
    reranker_factory:      Callable[[], Reranker]     | None = None
    # M5.2 起:会话历史（`--session` 时才用,不注入就不造）
    conversation_store_factory: Callable[[], ConversationStore] | None = None

命令：``faq -q/--question`` 必填（缺了退出码 2）。刻意不复用 ``-p/--prompt``：
chat 是「把这句话发给模型」，faq 是「拿这个问题去检索」。

> 命令名 （M5.1）从 `ask` 改成 **`faq`**：合流入口叫 `qa` 之后，
> `ask` 那种「我要提问」的口气听起来像总入口，喧宾夺主；`faq` / `rag` 成对，
> 都点明这条命令走的是哪条通道。**行为一个字没变**（本脚本只改了命令名）。

领域模型、端口、用例::

    from project_rag.domain import FaqEntry, FaqMatch, FaqAnswer
    FaqEntry(id: int, question: str, answer: str)
    FaqMatch(entry: FaqEntry, score: float)     # 0~1 的**置信度**，越大越可信
    FaqAnswer(answer: str, source: Literal["faq","llm"], score: float | None = None,
              truncated: bool = False)      # M5.3a:模型没说完时为 True

``score`` 的口径归适配器，用例层只认 ``score >= threshold``。当前 ``matcher_bm25.py``
给的是 **top1 原始分 ÷「该条问题问自己」的原始分**（换的口径，此前是
``(top-second)/top``）。为什么换：旧 margin 口径度量的是**区分度**而不是相似度，分数随
**语料冗余度**衰减——库里只要有一条近似重复问法，完全一致的问题也会掉到 0.14；而且
条目越多分数越低，阈值被迫随语料规模漂移，标不出来（实测 N=3 时真命中 0.411 →
N=50 时 0.335，同期无关问法最高 0.481，阈值区间是空集）。新口径分子分母同源，
实测一字不差 = 1.000（与语料规模无关）、改写问法 0.2~0.7、完全无关 ≤ 0.303
（463 条真实语料；N=50 时 ≤ 0.274），所以 0.85 有明确含义：**只答近乎一字不差的问题**。

**改写问法不归本通道管**：FAQ 通道只直返人写的标准答案，改写由知识库通道
（`rag` / `search`）负责。本脚本最后一条用例只覆盖「加礼貌语」级别的改写；
真正的改写评估集在 `data/eval/`，判定标准由 M4 的尺子定。契约只保证三件事：0~1、越大越可信、判定用 ``>=``。

    from project_rag.ports import FaqStore, TextMatcher, Cache
    FaqStore.load_all() -> Sequence[FaqEntry]
    TextMatcher.best_match(query, candidates) -> FaqMatch | None
    Cache.get(key) -> str | None / Cache.set(key, value, ttl) -> None
    缓存 key 怎么构造是实现自由（当前 ``faq:v1:{去首尾空白后的问题}``）；
    **value 的格式是契约**：``"<source>\\x1f<answer>"``（\\x1f = ASCII 单元分隔符）。
    来源跟着值一起存 —— 命中缓存时才不用一律当 ``faq``，负缓存里的模型答案
    不会被冒充成标准答案。编解码只在用例层（``answer_faq._encode/_decode``）。

    from project_rag.application import answer_faq
    answer_faq(query, *, store, matcher, llm, cache=None, threshold=0.85, cache_ttl=300) -> FaqAnswer

顺序：先查缓存 → 命中直接返回（``source`` 从 value 解出来）；否则 ``store.load_all()``
+ ``matcher.best_match()``；``score >= threshold`` 直接返回并写缓存；否则 LLM 兜底
（``source="llm"``）。阈值来自 ``RAG_FAQ__THRESHOLD``，经 ``Deps.faq_threshold`` 注入；
边界取等号。缓存异常一律吞掉（记 warning）。

**穿透防护也在契约内**：LLM 兜底答案同样写缓存（同一把 key，这就是负缓存），
同一句超纲问题第二次问不再花钱；但**空答案不写**（否则一次调用失败会变成一条
"有效"的空白答案，被固化到 TTL 结束）。命中负缓存时 ``source`` 仍是 ``llm``。

故障语义：Redis 是纯加速，**装配期**（``cache_factory`` 抛错）和**调用期**
（``cache.get/set`` 抛错）都必须降级，答案与退出码不受影响。MySQL 相反：
连不上必须 fail-fast，错误信息点名连接目标（host:port）且不吐堆栈。

种子数据：FaqStore 只有 ``load_all()``（写入/增量更新见 deferred.md），端到端用例
由本脚本用 pymysql 建表 + 插入 + 清理。表结构是契约的一部分::

    CREATE TABLE {table} (
        id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        question VARCHAR(512) NOT NULL,
        answer   TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci

表名由 ``RAG_FAQ__TABLE`` 配置；``load_all()`` 按 id 升序返回。

分层（静态扫描，不靠自觉）：``application/`` 只准依赖 标准库 + ``project_rag.domain`` /
``project_rag.ports`` / ``project_rag.observability``；``pymysql`` / ``redis`` / ``jieba`` /
``rank_bm25`` 只准出现在 ``adapters/``。（**不要求**适配器显式继承端口：Protocol 就是结构化子类型，
符合性由 pyright 断 —— 删掉了那条 AST 检查。）

缓存必须测**短路**（第二次不再查库、不再匹配），只测「写进去了」等于没测。
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

from _harness import check, report, env, SkipCheck

SRC = Path(__file__).resolve().parents[2] / "src" / "project_rag"

# 契约默认值（与 config/config.yaml / docker-compose.yaml 必须一致）
# 0.85 是「自身分归一化」口径下的默认值：一字不差 = 1.000、完全无关 ≤ 0.274(N=50)，
# 所以 0.85 = 只答近乎一字不差的问题。口径定义见 matcher_bm25.py 与本脚本 docstring。
DEFAULT_THRESHOLD = 0.85
EXPECTED_MYSQL = {"host": "127.0.0.1", "port": 10093, "user": "rag", "database": "rag"}
EXPECTED_REDIS = {"host": "127.0.0.1", "port": 10087, "db": 0}
# faq 段的**类默认值**（= 没有 config.yaml 时的兜底，也是"这段代码一直以来的行为"）。
# 两个覆盖率门槛是这次作者报障后加的（推导见 adapters/matcher_bm25.py）：它们**必须与适配器里的
# DEFAULT_* 常量一致** —— 两处各写一个数就是两处真相，改一处不会有人发现。
# （曾经还有一道"上界"，量出来它随语料规模漂、且战果与覆盖率逐项相同，已删掉。）
EXPECTED_FAQ = {
    "threshold": 0.85,
    "min_query_coverage": 0.5,
    "min_entry_coverage": 0.8,
    # 数据侧那两条下限（装载脚本用；它们**在配置里**，不在脚本里 —— 有契约盯着）
    "min_question_tokens": 2,
    "min_question_chars": 8,
    "table": "faq",
}

# 缓存 value 的编码分隔符（ASCII 单元分隔符）。契约的一部分，必须与
# application/answer_faq.py 的 _CACHE_SEP 一致 —— 这里刻意写死字面量而不是 import，
# 契约脚本不该依赖被测实现。
CACHE_SEP = "\x1f"

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

# M2 新引入的第三方 SDK：只准出现在 adapters/（required 里的必须有实际使用点，
# 否则下面的分层扫描会在「一条都没用到」时假装通过）。
# 新增任何 SDK 都要往这里加一个名字 —— 名单是手写的，漏了就等于这条纪律对它失效。
M2_SDK_ROOTS = ("pymysql", "redis", "jieba", "rank_bm25")
M2_SDK_REQUIRED = ("pymysql", "redis", "rank_bm25")

# 端到端用例的中文语料
CHINESE_Q = "退款流程怎么走？"
CHINESE_A = "在订单页点「申请退款」，1-3 个工作日到账。"


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
    module = _require("project_rag.domain", ("FaqEntry", "FaqMatch", "FaqAnswer"))
    return module.FaqEntry, module.FaqMatch, module.FaqAnswer


def _cli():
    module = _require("project_rag.cli", ("cli",))
    return module.cli


def _deps_cls():
    module = _require("project_rag.cli", ("Deps",))
    return module.Deps


def _answer_faq():
    module = _require("project_rag.application", ("answer_faq",))
    return module.answer_faq


def _settings_cls():
    module = _require("project_rag.config", ("Settings",))
    return module.Settings


def _faq_cls():
    """输入:无。行为:取 FaqConfig（用来读**类默认值**，不带 config.yaml 的覆盖）。输出:类。"""
    module = _require("project_rag.config", ("FaqConfig",))
    return module.FaqConfig


ANSWER_FAQ_SIGNATURE = (
    "answer_faq(query, *, store, matcher, llm, cache=None, "
    "threshold=0.85, cache_ttl=300) -> FaqAnswer"
)


def _answer(query: str, **kwargs):
    """按契约调用 answer_faq，签名不符时给人话而不是裸 TypeError。"""
    try:
        return _answer_faq()(query, **kwargs)
    except TypeError as exc:
        raise AssertionError(
            f"answer_faq 调用签名与契约不符：{exc}；期望 {ANSWER_FAQ_SIGNATURE}"
        ) from exc


def _plain(value) -> str:
    """兼容 str 与 SecretStr 两种密码写法。"""
    getter = getattr(value, "get_secret_value", None)
    # str(...) 包一层:getter() 的返回类型是 Any,直接 return 会被推成 object | str
    return str(getter()) if callable(getter) else str(value)


# ── 测试替身 ──────────────────────────────────────────────────────────────────
class FakeLLM:
    """记录调用次数，然后按可预测顺序吐字。"""

    def __init__(self, chunks=("这是", "LLM", "兜底答案")) -> None:
        self.chunks = tuple(chunks)
        self.calls = 0
        self.seen: list[tuple[str, str]] = []

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def stream(self, messages):
        # 故意写成普通函数返回迭代器：调用发生在这里，而不是在被迭代时，
        # 否则「调了但没消费」会被误判成「没调」
        self.calls += 1
        self.seen = [(m.role, m.content) for m in messages]
        LlmDelta = _require("project_rag.domain", ("LlmDelta",)).LlmDelta
        frames = [LlmDelta(text=piece) for piece in self.chunks]
        frames.append(LlmDelta(finish_reason="stop", last=True))
        return iter(frames)


class FakeStore:
    def __init__(self, entries=()) -> None:
        self.entries = list(entries)
        self.loads = 0

    def load_all(self):
        self.loads += 1
        return list(self.entries)


class FakeMatcher:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple[str, list]] = []

    def best_match(self, query, candidates):
        self.calls.append((query, list(candidates)))
        if self.result is None or not candidates:
            return None
        return self.result


class FakeCache:
    """内存缓存，可注入 get/set 故障。"""

    def __init__(self, seed=None, get_error=None, set_error=None) -> None:
        self.data = dict(seed or {})
        self.get_error = get_error
        self.set_error = set_error
        self.get_keys: list[str] = []
        self.sets: list[tuple] = []

    def get(self, key):
        self.get_keys.append(key)
        if self.get_error is not None:
            raise self.get_error
        return self.data.get(key)

    def set(self, key, value, ttl):
        self.sets.append((key, value, ttl))
        if self.set_error is not None:
            raise self.set_error
        self.data[key] = value


def _entry():
    FaqEntry, _, _ = _models()
    return FaqEntry(id=1, question=CHINESE_Q, answer=CHINESE_A)


def _hit(score: float):
    _, FaqMatch, _ = _models()
    entry = _entry()
    return FakeMatcher(FaqMatch(entry=entry, score=score))


# ── 1. 领域模型与端口形状 ──────────────────────────────────────────────────────
@check("domain：FaqEntry / FaqMatch / FaqAnswer（frozen+slots，字段名与类型按契约）")
def _() -> str:
    FaqEntry, FaqMatch, FaqAnswer = _models()
    classes = {"FaqEntry": FaqEntry, "FaqMatch": FaqMatch, "FaqAnswer": FaqAnswer}
    for name, cls in classes.items():
        if not dataclasses.is_dataclass(cls):
            raise AssertionError(f"{name} 不是 dataclass；领域模型统一 dataclass(frozen=True, slots=True)")
        if not getattr(cls, "__dataclass_params__").frozen:
            raise AssertionError(f"{name} 应为 frozen=True（领域值对象不可变，可安全当字典键）")
        if not getattr(cls, "__slots__", None):
            raise AssertionError(f"{name} 应为 slots=True（跟 ChatMessage 保持一致）")

    hints = {name: typing.get_type_hints(cls) for name, cls in classes.items()}
    fields = {
        name: tuple(f.name for f in dataclasses.fields(cls))
        for name, cls in classes.items()
    }

    if fields["FaqEntry"] != ("id", "question", "answer"):
        raise AssertionError(f"FaqEntry 字段应为 ('id','question','answer')，实际 {fields['FaqEntry']}")
    for field, want in (("id", int), ("question", str), ("answer", str)):
        if hints["FaqEntry"][field] is not want:
            raise AssertionError(f"FaqEntry.{field} 应为 {want.__name__}，实际 {hints['FaqEntry'][field]}")

    if fields["FaqMatch"] != ("entry", "score"):
        raise AssertionError(f"FaqMatch 字段应为 ('entry','score')，实际 {fields['FaqMatch']}")
    if hints["FaqMatch"]["entry"] is not FaqEntry:
        raise AssertionError(f"FaqMatch.entry 应为 FaqEntry，实际 {hints['FaqMatch']['entry']}")
    if hints["FaqMatch"]["score"] is not float:
        raise AssertionError(f"FaqMatch.score 应为 float（0~1），实际 {hints['FaqMatch']['score']}")

    if fields["FaqAnswer"] != ("answer", "source", "score", "truncated"):
        raise AssertionError(
            f"FaqAnswer 字段应为 ('answer','source','score','truncated')，实际 {fields['FaqAnswer']}"
        )
    if hints["FaqAnswer"]["answer"] is not str:
        raise AssertionError(f"FaqAnswer.answer 应为 str，实际 {hints['FaqAnswer']['answer']}")
    source_args = set(typing.get_args(hints["FaqAnswer"]["source"]))
    if source_args != {"faq", "llm"}:
        raise AssertionError(
            f"FaqAnswer.source 应为 Literal['faq','llm']，实际 {hints['FaqAnswer']['source']}"
        )
    score_args = set(typing.get_args(hints["FaqAnswer"]["score"]))
    if score_args != {float, type(None)}:
        raise AssertionError(
            f"FaqAnswer.score 应为 float | None（兜底时没有分数），实际 {hints['FaqAnswer']['score']}"
        )
    if FaqAnswer("x", "faq").score is not None:
        raise AssertionError("FaqAnswer.score 应有默认值 None，否则兜底路径每次都要编一个分数")
    return "3 个模型字段/类型/默认值全对"


@check("ports：FaqStore / TextMatcher / Cache 都是 Protocol，方法名与参数按契约")
def _() -> str:
    module = _require("project_rag.ports", ("FaqStore", "TextMatcher", "Cache"))
    specs = {
        "FaqStore": {"load_all": ()},
        "TextMatcher": {"best_match": ("query", "candidates")},
        "Cache": {"get": ("key",), "set": ("key", "value", "ttl")},
    }
    for port_name, methods in specs.items():
        port = getattr(module, port_name)
        if not getattr(port, "_is_protocol", False):
            raise AssertionError(f"{port_name} 不是 typing.Protocol（端口只声明能力，不含实现）")
        for method_name, want_params in methods.items():
            fn = getattr(port, method_name, None)
            if fn is None:
                raise AssertionError(f"{port_name} 缺方法 {method_name}")
            got = tuple(p for p in inspect.signature(fn).parameters if p != "self")
            if got != want_params:
                raise AssertionError(
                    f"{port_name}.{method_name} 参数应为 {want_params}，实际 {got}"
                )
    return "load_all / best_match / get+set 签名一致"


@check("组合根：四个重资源工厂是进程级单例（lru_cache 且真的接进 Deps）—— M5.6")
def _() -> str:
    """为什么是 AST 而不是「调两次看是不是同一个对象」:

    `_embedder()` / `_reranker()` 会真加载 2G 模型,而 `_vector_store()` 要连 Milvus ——
    验收脚本不该为了验「缓存」把这两份钱都付了。所以只看两件结构事实:
    ① 这几个工厂被 `@lru_cache` 装饰;② `Deps(...)` 里接的**就是这几个函数对象**,
    而不是又写了一个新 lambda(装饰了却没用上,是最容易犯的错)。
    行为侧的账由 M5.6 的延迟日志与 `check_m5_api` 的「预热」那条兜底。
    """
    tree = ast.parse((SRC / "cli" / "deps.py").read_text(encoding="utf-8"))
    build = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "build_real_deps"
        ),
        None,
    )
    if build is None:
        raise AssertionError("cli/deps.py 里找不到 build_real_deps")

    cached: set[str] = set()
    for node in build.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "lru_cache"
            ):
                cached.add(node.name)

    want = {"_vector_store", "_embedder", "_reranker", "_keyword_index"}
    missing = want - cached
    if missing:
        raise AssertionError(
            f"这些工厂没有 lru_cache：{sorted(missing)} —— 它们各抱一份重资源"
            "（Milvus 连接 / 嵌入模型 2G / 重排模型 2.2G / 全量分词索引）。"
            "CLI 一进程一命令无所谓,但 project-rag-api 是常驻进程:"
            "不缓存就是每个走 RAG 的请求重载一遍模型（M5.6 实测这一个坑 4.4s/请求）"
        )

    wired = {
        keyword.arg: keyword.value.id
        for call in ast.walk(build)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "Deps"
        for keyword in call.keywords
        if isinstance(keyword.value, ast.Name)
    }
    for field, helper in (
        ("embedder_factory", "_embedder"),
        ("reranker_factory", "_reranker"),
        ("keyword_index_factory", "_keyword_index"),
        ("vector_store_factory", "_vector_store"),
    ):
        if wired.get(field) != helper:
            raise AssertionError(
                f"Deps 的 {field} 接的不是 {helper}（实际 {wired.get(field)!r}）—— "
                "装饰了 lru_cache 却没接上去,等于没缓存"
            )
    return f"四个工厂都是进程级单例并接进 Deps：{sorted(cached)}"


# ── 组合根 Deps 的形状契约 ─────────────────────────────────────────────────────
# **已搬到 `check_deps_root.py`**：这一段原来在这里，而本脚本属 `--set local`
# （要真 FAQ 语料），于是常规回归根本不跑它 —— 同一个洞咬过两次（稀疏列那次、
# M8 查询改写那次）。搬出去之后它进 `--set ci`，加 Deps 字段漏改就当场红。

# ── 2. 配置 ───────────────────────────────────────────────────────────────────
@check("配置：mysql / redis / faq 三段嵌套配置齐全，默认值与草案一致")
def _() -> str:
    from pydantic import BaseModel

    settings = _settings_cls()()
    for section in ("mysql", "redis", "faq"):
        if not hasattr(settings, section):
            raise AssertionError(
                f"Settings 缺 {section} 段；照抄第 8.5 节加进 config.yaml 与 Settings"
            )
        if not isinstance(getattr(settings, section), BaseModel):
            raise AssertionError(
                f"Settings.{section} 应是嵌套模型对象（像 llm 那样），"
                f"实际 {type(getattr(settings, section)).__name__}"
            )

    for field, want in EXPECTED_MYSQL.items():
        if getattr(settings.mysql, field, None) != want:
            raise AssertionError(
                f"mysql.{field} 期望 {want!r}，实际 {getattr(settings.mysql, field, None)!r}"
            )
    for field, want in EXPECTED_REDIS.items():
        if getattr(settings.redis, field, None) != want:
            raise AssertionError(
                f"redis.{field} 期望 {want!r}，实际 {getattr(settings.redis, field, None)!r}"
            )
    if not _plain(settings.mysql.password) or not _plain(settings.redis.password):
        raise AssertionError("mysql.password / redis.password 不该为空")
    # **比类默认值,不比生效值**:生效值会被 config.yaml / 环境变量覆盖（那是部署旋钮，
    # 作者可以调），"生效值还在不在可用区间里"由 check_p0_4_ab 那条契约负责。
    defaults = _faq_cls()()
    for field, want in EXPECTED_FAQ.items():
        actual = getattr(defaults, field, "<缺这个字段>")
        if isinstance(want, bool) or not isinstance(want, (int, float)):
            same = actual == want
        else:
            same = isinstance(actual, (int, float)) and abs(actual - want) <= 1e-9
        if not same:
            raise AssertionError(
                f"faq.{field} 的**类默认值**期望 {want!r}，实际 {actual!r}"
                "（config.yaml 里可以不一样，但类默认值是这个项目的口径基线）"
            )
    if settings.faq.table != defaults.table:
        raise AssertionError(
            f"生效的 faq.table={settings.faq.table!r} 与类默认值 {defaults.table!r} 不一致"
        )
    return (
        f"mysql={settings.mysql.host}:{settings.mysql.port} "
        f"redis={settings.redis.host}:{settings.redis.port} "
        f"faq 默认值 threshold={defaults.threshold} "
        f"覆盖 {defaults.min_query_coverage}/{defaults.min_entry_coverage}"
        f"（生效 threshold={settings.faq.threshold}）"
    )


@check("配置可被环境变量覆盖（RAG_FAQ__THRESHOLD / RAG_MYSQL__HOST / RAG_REDIS__PORT）")
def _() -> str:
    Settings = _settings_cls()
    with env(
        RAG_FAQ__THRESHOLD="0.7",
        RAG_MYSQL__HOST="db.internal",
        RAG_REDIS__PORT="6399",
    ):
        settings = Settings()
    got = (settings.faq.threshold, settings.mysql.host, settings.redis.port)
    want = (0.7, "db.internal", 6399)
    if got != want:
        raise AssertionError(
            f"期望 {want}，实际 {got}（检查 env_prefix='RAG_' 与嵌套分隔符 '__'，"
            "新配置段必须是 Settings 上的嵌套模型）"
        )
    return f"threshold={got[0]} mysql.host={got[1]} redis.port={got[2]}"


# ── 3. 分层纪律（静态扫描） ────────────────────────────────────────────────────
def _imports_of(path: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


@check("application/ 只依赖 标准库 + domain/ports/observability（用例层不许碰适配器）")
def _() -> str:
    app = SRC / "application"
    if not app.exists():
        raise AssertionError(
            "src/project_rag/application/ 不存在；M2 起才有用例层，answer_faq 放在这里"
        )
    allowed = ("project_rag.domain", "project_rag.ports", "project_rag.observability")
    bad: list[str] = []
    for path in sorted(app.rglob("*.py")):
        for module, level in _imports_of(path):
            if level > 0 or not module:  # 同包相对导入，允许
                continue
            root = module.split(".")[0]
            if root == "project_rag":
                # from project_rag import domain 这种写法只暴露顶层包名，无法细分，放行
                if module != "project_rag" and not module.startswith(allowed):
                    bad.append(f"{path.relative_to(SRC)} -> {module}")
            elif root not in sys.stdlib_module_names:
                bad.append(f"{path.relative_to(SRC)} -> {module}（第三方 SDK 必须藏在适配器里）")
    if bad:
        raise AssertionError(
            f"application/ 违反依赖方向：{bad}。"
            "用例只认识端口，不认识具体实现——否则换适配器就要改用例"
        )
    return f"{len(list(app.rglob('*.py')))} 个文件干净"


@check("pymysql / redis / jieba 只出现在 adapters/（且确实被用上）")
def _() -> str:
    hits: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for module, level in _imports_of(path):
            if level > 0:
                continue
            root = module.split(".")[0]
            if root in M2_SDK_ROOTS:
                hits.setdefault(root, []).append(str(path.relative_to(SRC)))
    leaked = {
        root: where
        for root, where in hits.items()
        if any(not w.startswith("adapters") for w in where)
    }
    if leaked:
        raise AssertionError(
            f"SDK 泄漏到适配器之外：{leaked}。"
            "这是「换存储/换匹配算法不用改用例」的唯一保证（同 M1 的 langchain 规则）"
        )
    absent = [root for root in M2_SDK_REQUIRED if root not in hits]
    if absent:
        raise AssertionError(
            f"{absent} 还没被任何适配器用到：FAQ 存储/缓存适配器尚未实现。"
            "期望 adapters/faq_mysql.py 用 pymysql、adapters/cache_redis.py 用 redis"
        )
    detail = ", ".join(f"{root} ← {sorted(set(hits[root]))}" for root in sorted(hits))
    return detail


# ── 4. 用例语义（直接调用 answer_faq，全 Fake） ────────────────────────────────
@check("命中：score >= threshold → 标准答案直返，不调 LLM，并写缓存")
def _() -> str:
    entry = _entry()
    store, matcher, llm, cache = FakeStore([entry]), _hit(0.9), FakeLLM(), FakeCache()
    result = _answer(
        "怎么退款",
        store=store,
        matcher=matcher,
        llm=llm,
        cache=cache,
        threshold=DEFAULT_THRESHOLD,
    )
    if result.source != "faq":
        raise AssertionError(f"命中时 source 应为 'faq'，实际 {result.source!r}")
    if result.answer != CHINESE_A:
        raise AssertionError(f"命中时应直接返回标准答案，实际 {result.answer!r}")
    if result.score != 0.9:
        raise AssertionError(f"命中时应带上分数 0.9，实际 {result.score!r}")
    if llm.calls:
        raise AssertionError("命中 FAQ 却调用了 LLM：兜底只能发生在未命中时")
    if not store.loads:
        raise AssertionError("没有调用 store.load_all()，FAQ 从哪儿来？")
    if not cache.sets:
        raise AssertionError("命中后没有写缓存（cache.set 未被调用）")
    want = f"faq{CACHE_SEP}{CHINESE_A}"  # value 契约："<source>\x1f<answer>"
    if not any(value == want and isinstance(ttl, int) and ttl > 0 for _, value, ttl in cache.sets):
        raise AssertionError(
            f"缓存应写入 (key, {want!r}, 正整数 ttl)，实际 {cache.sets!r}；"
            "value 必须是 '<source>\\x1f<answer>' 编码串，见本脚本 docstring"
        )
    return f"answer={result.answer!r} score={result.score} 缓存已写"


@check("阈值边界：score == threshold 算命中（取等号，不是严格大于）")
def _() -> str:
    result = _answer(
        "怎么退款",
        store=FakeStore([_entry()]),
        matcher=_hit(DEFAULT_THRESHOLD),
        llm=FakeLLM(),
        cache=FakeCache(),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.source != "faq":
        raise AssertionError(
            f"score 正好等于阈值时应命中，实际走了 {result.source!r}。"
            "比较必须是 score >= threshold（阈值是「最低可接受相似度」）"
        )
    return f"score={DEFAULT_THRESHOLD} == threshold → faq"


@check("阈值边界：score 略低于 threshold → 交给 LLM 兜底")
def _() -> str:
    llm = FakeLLM()
    result = _answer(
        "怎么退款",
        store=FakeStore([_entry()]),
        matcher=_hit(DEFAULT_THRESHOLD - 1e-6),
        llm=llm,
        cache=FakeCache(),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.source != "llm":
        raise AssertionError(f"低于阈值应兜底，实际 source={result.source!r}（差一点都不该放行）")
    if llm.calls != 1:
        raise AssertionError(f"兜底应恰好调用 LLM 一次，实际 {llm.calls} 次")
    return "score < threshold → llm"


@check("未命中：matcher 返回 None → LLM 兜底，答案就是模型的输出")
def _() -> str:
    llm = FakeLLM(chunks=("抱歉", "，", "我还没学过这个"))
    result = _answer(
        "公司年会在哪天",
        store=FakeStore([_entry()]),
        matcher=FakeMatcher(None),
        llm=llm,
        cache=FakeCache(),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.source != "llm":
        raise AssertionError(f"未命中时 source 应为 'llm'，实际 {result.source!r}")
    if result.score is not None:
        raise AssertionError(f"兜底答案没有相似度，score 应为 None，实际 {result.score!r}")
    if result.answer != llm.text:
        raise AssertionError(f"兜底答案应等于 LLM 流式输出的拼接，实际 {result.answer!r}")
    if llm.calls != 1:
        raise AssertionError(f"应恰好调用 LLM 一次，实际 {llm.calls} 次")
    return f"answer={result.answer!r}"


@check("FAQ 库为空时不崩：直接走 LLM 兜底")
def _() -> str:
    llm = FakeLLM()
    result = _answer(
        "随便问点什么",
        store=FakeStore([]),
        matcher=FakeMatcher(None),
        llm=llm,
        cache=FakeCache(),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.source != "llm":
        raise AssertionError(f"空库应兜底，实际 source={result.source!r}")
    return "空库 → llm"


@check("缓存短路：同一 query 第二次命中缓存，不再查库、不再匹配")
def _() -> str:
    entry = _entry()
    query = "怎么退款"
    probe = FakeCache()
    _answer(
        query,
        store=FakeStore([entry]),
        matcher=_hit(0.9),
        llm=FakeLLM(),
        cache=probe,
        threshold=DEFAULT_THRESHOLD,
    )
    if not probe.get_keys:
        raise AssertionError("answer_faq 从未调用 cache.get：缓存只写不读等于没有缓存")
    key = probe.get_keys[-1]

    store, matcher = FakeStore([entry]), _hit(0.9)
    result = _answer(
        query,
        store=store,
        matcher=matcher,
        llm=FakeLLM(),
        cache=FakeCache({key: f"faq{CACHE_SEP}{CHINESE_A}"}),
        threshold=DEFAULT_THRESHOLD,
    )
    if store.loads:
        raise AssertionError("命中缓存却仍然查了 MySQL：cache.get 必须在 store.load_all() 之前")
    if matcher.calls:
        raise AssertionError("命中缓存却仍然做了相似匹配：缓存的目的就是省掉这一步")
    if result.answer != CHINESE_A or result.source != "faq":
        raise AssertionError(
            f"缓存命中应返回 source='faq' 的答案 {CHINESE_A!r}，实际 {result.source!r}/{result.answer!r}"
        )
    return f"key={key!r} 短路成功"


@check("缓存调用期降级：cache.get 抛错不影响答案")
def _() -> str:
    result = _answer(
        "怎么退款",
        store=FakeStore([_entry()]),
        matcher=_hit(0.9),
        llm=FakeLLM(),
        cache=FakeCache(get_error=RuntimeError("redis: connection refused")),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.answer != CHINESE_A or result.source != "faq":
        raise AssertionError(
            f"缓存读失败应降级（当作未命中），答案仍须正确，实际 {result.source!r}/{result.answer!r}。"
            "Redis 是纯加速：它的故障不该降低可用性"
        )
    return "cache.get 抛错 → 仍命中 FAQ"


@check("缓存调用期降级：cache.set 抛错不影响答案")
def _() -> str:
    result = _answer(
        "怎么退款",
        store=FakeStore([_entry()]),
        matcher=_hit(0.9),
        llm=FakeLLM(),
        cache=FakeCache(set_error=RuntimeError("redis: connection refused")),
        threshold=DEFAULT_THRESHOLD,
    )
    if result.answer != CHINESE_A or result.source != "faq":
        raise AssertionError(
            f"缓存写失败应降级，答案仍须正确，实际 {result.source!r}/{result.answer!r}"
        )
    return "cache.set 抛错 → 仍命中 FAQ"


@check("穿透防护：同一超纲问题第二次问不再花 LLM 调用，且来源仍是 llm")
def _() -> str:
    query = "公司年会在哪天"
    cache, llm = FakeCache(), FakeLLM()
    first = _answer(
        query,
        store=FakeStore([_entry()]),
        matcher=FakeMatcher(None),
        llm=llm,
        cache=cache,
        threshold=DEFAULT_THRESHOLD,
    )
    if llm.calls != 1 or first.source != "llm":
        raise AssertionError(
            f"第一次应恰好走一次 LLM 兜底，实际 calls={llm.calls} source={first.source!r}"
        )
    second = _answer(
        query,
        store=FakeStore([_entry()]),
        matcher=FakeMatcher(None),
        llm=llm,
        cache=cache,
        threshold=DEFAULT_THRESHOLD,
    )
    if llm.calls != 1:
        raise AssertionError(
            f"第二次问同一句超纲问题又调了 LLM（累计 {llm.calls} 次）："
            "兜底答案也要写缓存（负缓存），否则重复提问每次都花钱（缓存穿透）"
        )
    if second.source != "llm":
        raise AssertionError(
            f"命中负缓存时 source 应仍是 'llm'，实际 {second.source!r}。"
            "模型现编的答案被标成 faq，M4 路由和 M5 评估就分不清钱花在哪了"
        )
    if second.answer != first.answer:
        raise AssertionError(f"两次答案应一致，实际 {first.answer!r} vs {second.answer!r}")
    return f"LLM 调用 {llm.calls} 次（第二次走负缓存）"


@check("负缓存只缓存非空答案（空答案不该被固化住）")
def _() -> str:
    ok = FakeCache()
    _answer(
        "公司年会在哪天",
        store=FakeStore([]),
        matcher=FakeMatcher(None),
        llm=FakeLLM(),
        cache=ok,
        threshold=DEFAULT_THRESHOLD,
    )
    if not ok.sets:
        raise AssertionError(
            "非空兜底答案没写缓存：重复问同一超纲问题会重复花 LLM 调用（穿透）"
        )

    empty = FakeCache()
    _answer(
        "公司年会在哪天",
        store=FakeStore([]),
        matcher=FakeMatcher(None),
        llm=FakeLLM(chunks=()),
        cache=empty,
        threshold=DEFAULT_THRESHOLD,
    )
    if empty.sets:
        raise AssertionError(
            f"空答案不该写缓存，实际写了 {empty.sets!r}；"
            "否则一次调用失败会变成一条'有效'的空白答案，被固化到 TTL 结束"
        )
    return "非空写、空不写"


# ── 5. CLI 链路 ──────────────────────────────────────────────────────────────
def _run_cli(argv: list[str], extra_env: dict[str, str] | None = None, timeout: int = 90):
    env_map = dict(os.environ)
    env_map.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "project_rag", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env_map,
    )


@check("--help 列出 faq；faq 缺 -q 退出码 2")
def _() -> str:
    helped = _run_cli(["--help"])
    if helped.returncode != 0:
        raise AssertionError(f"--help 退出码 {helped.returncode}：{helped.stderr[:200]}")
    if "faq" not in helped.stdout:
        raise AssertionError("--help 里没列出 faq 子命令（commands.ALL 里注册了吗？）")
    missing = _run_cli(["faq"])
    if missing.returncode != 2:
        raise AssertionError(
            f"faq 缺 -q 退出码应为 2（argparse 用法错误），实际 {missing.returncode}；"
            "别把 -q 写成可选，否则空问题会一路走到匹配层"
        )
    return "help 列出 faq；缺 -q → 2"


@check("注入 Deps：命中 → stdout 是标准答案，退出码 0，不联网")
def _() -> str:
    cli, Deps = _cli(), _deps_cls()
    entry = _entry()
    llm = FakeLLM()
    deps = Deps(
        llm_factory=lambda: llm,
        faq_store_factory=lambda: FakeStore([entry]),
        matcher_factory=lambda: _hit(0.9),
        cache_factory=lambda: FakeCache(),
        faq_threshold=DEFAULT_THRESHOLD,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(["faq", "-q", "怎么退款"], deps=deps)
    if code != 0:
        raise AssertionError(f"退出码应为 0，实际 {code}；stderr={err.getvalue()[:200]!r}")
    if CHINESE_A not in out.getvalue():
        raise AssertionError(f"stdout 里没有标准答案，实际 {out.getvalue()!r}")
    if llm.calls:
        raise AssertionError("命中 FAQ 却调用了 LLM")
    return f"stdout={out.getvalue().strip()!r}"


@check("注入 Deps：未命中 → LLM 兜底文本输出，退出码 0")
def _() -> str:
    cli, Deps = _cli(), _deps_cls()
    llm = FakeLLM(chunks=("兜底", "答案"))
    deps = Deps(
        llm_factory=lambda: llm,
        faq_store_factory=lambda: FakeStore([_entry()]),
        matcher_factory=lambda: FakeMatcher(None),
        cache_factory=lambda: FakeCache(),
        faq_threshold=DEFAULT_THRESHOLD,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(["faq", "-q", "公司年会在哪天"], deps=deps)
    if code != 0:
        raise AssertionError(f"退出码应为 0，实际 {code}；stderr={err.getvalue()[:200]!r}")
    if llm.text not in out.getvalue():
        raise AssertionError(f"stdout 里没有兜底文本，实际 {out.getvalue()!r}")
    if llm.calls != 1:
        raise AssertionError(f"应恰好调用 LLM 一次，实际 {llm.calls} 次")
    return f"stdout={out.getvalue().strip()!r}"


@check("缓存装配期降级：cache_factory 抛错 → 仍然 FAQ 命中，退出码 0")
def _() -> str:
    cli, Deps = _cli(), _deps_cls()

    def boom():
        raise RuntimeError("redis: 连接被拒绝")

    entry = _entry()
    deps = Deps(
        llm_factory=lambda: FakeLLM(),
        faq_store_factory=lambda: FakeStore([entry]),
        matcher_factory=lambda: _hit(0.9),
        cache_factory=boom,
        faq_threshold=DEFAULT_THRESHOLD,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(["faq", "-q", "怎么退款"], deps=deps)
    if code != 0:
        raise AssertionError(
            f"缓存工厂抛错却退出码 {code}；Redis 起不来不该让整个问答起不来"
            f"（stderr={err.getvalue()[:200]!r}）"
        )
    if CHINESE_A not in out.getvalue():
        raise AssertionError(f"降级后仍应给出标准答案，实际 {out.getvalue()!r}")
    return "工厂抛错 → 无缓存继续"


@check("MySQL 不可用时 fail-fast：退出码 1，错误信息含连接目标")
def _() -> str:
    proc = _run_cli(
        ["faq", "-q", "怎么退款"],
        {"RAG_MYSQL__HOST": "127.0.0.1", "RAG_MYSQL__PORT": "1"},
        timeout=60,
    )
    if proc.returncode != 1:
        raise AssertionError(
            f"MySQL 连不上时退出码应为 1（可读错误），实际 {proc.returncode}；"
            f"stderr={proc.stderr[:300]!r}"
        )
    if "127.0.0.1:1" not in proc.stderr:
        raise AssertionError(
            "错误信息里没有连接目标 127.0.0.1:1；数据库连不上却看不出连的是哪儿，"
            f"排查等于猜。实际 stderr={proc.stderr[:300]!r}"
        )
    if "mysql" not in proc.stderr.lower():
        raise AssertionError(f"错误信息没点名 MySQL：{proc.stderr[:200]!r}")
    if "Traceback (most recent call last)" in proc.stderr:
        raise AssertionError("把原始堆栈抛给了用户：堆栈进日志，人话给用户（同 M1 的 chat）")
    return f"1 + {proc.stderr.strip().splitlines()[-1][:80]!r}"


# ── 6. 真实链路（需要容器；起不来则 SKIP 并说明原因） ─────────────────────────
def _seed_faq(rows: list[tuple[int, str, str]]) -> str:
    """建一张临时 FAQ 表并落库；返回表名（由调用方负责删除）。"""
    import pymysql

    table = f"faq_acceptance_{uuid.uuid4().hex[:8]}"
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


@check("真实链路：Redis 不可用 → 降级不崩，仍然 FAQ 命中（退出码 0）")
def _() -> str:
    table = _seed_faq([(1, CHINESE_Q, CHINESE_A)])
    try:
        proc = _run_cli(
            ["faq", "-q", CHINESE_Q],
            {"RAG_FAQ__TABLE": table, "RAG_REDIS__PORT": "1"},
            timeout=90,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"Redis 连不上却退出码 {proc.returncode}（应为 0：缓存是可选加速）；"
                f"stderr={proc.stderr[-300:]!r}"
            )
        if CHINESE_A not in proc.stdout:
            raise AssertionError(
                f"降级后没给出标准答案，实际 stdout={proc.stdout!r}；"
                "是不是把「缓存不可用」当成了「FAQ 不可用」？"
            )
        return f"RAG_REDIS__PORT=1 → 仍命中（表 {table}）"
    finally:
        _drop_faq(table)


@check("中文端到端：中文问题原句命中（jieba 分词 + utf8mb4 表读取 + 阈值来源）")
def _() -> str:
    # 这一条只验证「中文链路通不通」（tokenizer / 编码 / 表读取 / 阈值注入）。
    # 自身分归一化口径下一字不差 = 1.000，所以阈值取契约默认值 0.85 也必须命中。
    rows = [
        (1, CHINESE_Q, CHINESE_A),
        (2, "如何修改收货地址？", "在订单详情页点「修改地址」。"),
        (3, "发票怎么开？", "在订单页申请开票，电子票发到邮箱。"),
    ]
    table = _seed_faq(rows)
    try:
        proc = _run_cli(
            ["faq", "-q", CHINESE_Q],  # 原句提问：口径正确就必然 1.000 命中
            {
                "RAG_FAQ__TABLE": table,
                "RAG_FAQ__THRESHOLD": str(DEFAULT_THRESHOLD),
                "RAG_LLM__TIMEOUT_S": "8",
            },
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}"
            )
        if CHINESE_A not in proc.stdout:
            raise AssertionError(
                f"原句都没能命中自己，说明中文链路或分数口径有问题；实际 stdout={proc.stdout!r}。"
                "排查顺序：jieba 分词是否生效 → 表读取编码是否 utf8mb4 → 阈值是否真的从 "
                "RAG_FAQ__THRESHOLD 读到了 → matcher 的归一化分母是不是该条问自己的分"
            )
        return f"原句『{CHINESE_Q}』→ 命中并直返标准答案"
    finally:
        _drop_faq(table)


@check("中文端到端：加礼貌语的改写仍命中（口径换了以后不再误判为不像）")
def _() -> str:
    # 覆盖范围要说清楚：这条只验证「加礼貌语」级别的改写（前缀/后缀虚词）。
    # BM25 按 query 的词求和，多出来的虚词在库里没有 IDF 贡献，所以分数与一字不差
    # 同档（实测 1.000）。**真正的改写（换词/口语化）不归 FAQ 通道**——它落到 LLM 兜底
    # 是对的，改写能力由知识库通道（rag/search）提供，评估集见 data/eval/。
    rows = [
        (1, CHINESE_Q, CHINESE_A),
        (2, "如何修改收货地址？", "在订单详情页点「修改地址」。"),
        (3, "发票怎么开？", "在订单页申请开票，电子票发到邮箱。"),
    ]
    table = _seed_faq(rows)
    try:
        proc = _run_cli(
            ["faq", "-q", "请问，" + CHINESE_Q],
            {
                "RAG_FAQ__TABLE": table,
                "RAG_FAQ__THRESHOLD": str(DEFAULT_THRESHOLD),
                "RAG_LLM__TIMEOUT_S": "8",
            },
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}"
            )
        if CHINESE_A not in proc.stdout:
            raise AssertionError(
                f"加礼貌语后没命中（阈值 {DEFAULT_THRESHOLD}），实际 stdout={proc.stdout!r}。"
                "自身分归一化口径下虚词不该拉低分数，先确认 matcher 的分母是不是"
                "「该条问自己的分」"
            )
        return f"『请问，{CHINESE_Q}』→ 仍命中退款那一条"
    finally:
        _drop_faq(table)


@check("真实语料：给原问题接一句无关请求后仍命中自己（排序必须用原始分，不能用被夹取的归一化分）")
def _() -> str:
    # 这条检查来自一次真事故：463 条真实 FAQ 上给原问题接「，另外帮我写一首诗」，
    # 64/463（13.8%）的提问命中了**另一条**条目，而分数仍是 1.000 —— 直接越过阈值
    # 返回一条无关的标准答案。根因是排序用了归一化分：归一化分被夹在 1.0，
    # 语料一大就有一批候选同时顶到 1.0，拿它 argmax 等于按 id 顺序抽签。
    # 修法：排序用原始 BM25 分，归一化分只当「和阈值比」的置信度。
    #
    # 为什么读真表而不是像上面几条那样建临时表：这个退化只在真实语料的 idf 分布下复现，
    # 3 条临时行无论如何构造都复现不出来（同一条查询下 raw < top1 自身分时不会被夹取）。
    # 所以语料没灌时只能 SKIP，并在理由里点名 owner。
    import pymysql

    from project_rag.adapters import BM25TextMatcher
    from project_rag.domain import FaqEntry

    table = _settings_cls()().faq.table
    try:
        conn = pymysql.connect(connect_timeout=5, autocommit=True, **MYSQL_ENV)
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题，不是契约失败
        raise SkipCheck(
            f"MySQL 连不上（{MYSQL_ENV['host']}:{MYSQL_ENV['port']}）："
            f"{type(exc).__name__}: {exc}；先按 docs/STATUS.md §5 把容器起起来"
        ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, question, answer FROM {table} ORDER BY id ASC")
            entries = [
                FaqEntry(int(i), str(q), str(a)) for i, q, a in cur.fetchall()
            ]
    finally:
        conn.close()

    if len(entries) < 100:
        raise SkipCheck(
            f"真实表 {table!r} 只有 {len(entries)} 条，复现不了排序退化（需要真实 idf 分布）；"
            "灌语料：.venv/bin/python scripts/tools/load_faq_csv.py（owner：数据装载）"
        )

    matcher = BM25TextMatcher()
    wrong: list[str] = []
    for entry in entries:
        for query in (entry.question, entry.question + "，另外帮我写一首诗"):
            hit = matcher.best_match(query, entries)
            got = hit.entry.question if hit is not None else "<None>"
            if got != entry.question:
                wrong.append(f"『{query[:20]}』命中了『{got[:20]}』")

    if wrong:
        raise AssertionError(
            f"{len(wrong)}/{len(entries) * 2} 条提问没命中自己，例：{wrong[0]}。"
            "排序是不是用了被夹到 1.0 的归一化分？必须用原始 BM25 分排序，"
            "归一化只用来和阈值比较；复现脚本 scripts/acceptance/calibrate_threshold.py 的 D 组"
        )
    return f"{len(entries)} 条真实问题（原句 + 接无关尾缀）全部命中自己"


def _must_match(matcher, query: str, entries):
    """输入:匹配器 + 提问 + 语料。行为:调 best_match 并断言拿到了匹配。输出:FaqMatch。

    只是把"语料非空 ⇒ 一定有条目"这件事写成类型检查看得懂的断言 ——
    端口签名是 `FaqMatch | None`（空候选给 None），而这几条检查的语料永远非空。
    """
    hit = matcher.best_match(query, entries)
    if hit is None:
        raise AssertionError(f"语料非空却拿不到匹配：{query!r}")
    return hit


# ── FAQ 命中口径的三道闸门（作者报障驱动：任何带「协程」的提问都被两个字的那条答掉） ──
def _gate_corpus():
    """输入:无。行为:造一份能复现"夹带词有底分"的小语料。输出:(matcher, entries)。

    语料要点（与真实表同形）：一条**两个字**的短条目 `协程`；另外几条里出现 `线程` / `对比`
    —— 它们不是问题的主体，却让「线程、进程、协程的对比」这条提问在 BM25Plus 里拿到底分。
    `进程` 刻意**不在**语料里（没出现过的词没有 idf，也就不贡献底分）。
    """
    from project_rag.adapters import BM25TextMatcher
    from project_rag.domain import FaqEntry

    entries = [
        FaqEntry(1, "协程", "协程是用户态的轻量级线程。"),
        FaqEntry(2, "HashMap不是线程安全的，怎么让它线程安全", "用 ConcurrentHashMap。"),
        FaqEntry(3, "对比学习与监督学习的区别", "对比学习不需要标签。"),
        FaqEntry(4, "编译型与解释型语言区别", "编译型先整体翻译。"),
        FaqEntry(5, "Java线程池的7个核心参数", "核心线程数、最大线程数……"),
        # 下面两条是"别处"：它们提供 `垃圾回收` / `内存模型` / `什么` / `是` 这些词的 idf
        # （提问里夹带的词要有底分，才能复现"分子被抬起来"那件事）
        FaqEntry(6, "垃圾回收怎么调优", "看停顿时间与吞吐。"),
        FaqEntry(7, "什么是内存模型", "内存模型规定了可见性。"),
    ]
    return BM25TextMatcher(), entries


@check("分数回到 0~1：一字不差 1.000、夹带提问也是 1.000（判不命中靠覆盖率,不靠分数大小）")
def _score_range() -> str:
    """作者定稿的口径：分数压回 0~1，判"该不该答"交给两道覆盖率。

    报障那条提问的**未剪辑**比值是 1.712（分子里混进了 `线程/的/对比` 的底分），夹回 1.0 之后
    与一字不差**同分** —— 所以光靠阈值分不开它（这正是最初的 bug）。分开它的是**提问覆盖率**
    （那条提问只有 0.20 的词落在条目里），而不是分数的大小。这一条钉住：
    ① 分数确实落在 0~1；② 一字不差拿满分；③ 夹带提问也是满分但**不命中**（被覆盖率拦下）。
    """
    from project_rag.adapters import BM25TextMatcher

    entries = _gate_corpus()[1]
    matcher = BM25TextMatcher(min_query_coverage=0.5, min_entry_coverage=0.8)
    open_matcher = BM25TextMatcher(min_query_coverage=0, min_entry_coverage=0)
    hijack = "线程、进程、协程的对比"

    exact = _must_match(matcher, "协程", entries)
    if exact.entry.question != "协程" or abs(exact.score - 1.0) > 1e-9:
        raise AssertionError(f"一字不差该得 1.000 并命中自己，实际 {exact.entry.question!r} {exact.score}")
    # 闸门关掉时这条提问会被答掉（旧行为），它的**原始**比值已经顶到 1.0 以上（信息在这里丢了，
    # 所以不能再靠它判）—— 这也解释了为什么必须用覆盖率。
    loose = _must_match(open_matcher, hijack, entries)
    if loose.score < 1.0:
        raise AssertionError(f"夹带提问的置信度该是 1.000（夹取后），实际 {loose.score:.3f}")
    blocked = _must_match(matcher, hijack, entries)
    if blocked.score != 0.0:
        raise AssertionError(
            f"夹带提问该被**覆盖率**拦下（score=0），实际 {blocked.score:.3f}"
            f"（命中 {blocked.entry.question!r}）"
        )
    every = [_must_match(matcher, entry.question, entries).score for entry in entries]
    if min(every) < 0.0 or max(every) > 1.0:
        raise AssertionError(f"分数必须落在 0~1，实测 {min(every):.3f}~{max(every):.3f}")
    # **过得了闸门、但原始比值 > 1** 的提问（同一个词重复两遍）:夹取后必须是 1.000。
    # 少了这一条，"把 min(1.0, …) 去掉"这种坏法就抓不到（其它断言的分数恰好都是 1.000）。
    repeated = _must_match(matcher, "协程 协程", entries)
    if repeated.entry.question != "协程" or abs(repeated.score - 1.0) > 1e-9:
        raise AssertionError(
            f"同一个词重复两遍的提问该被夹到 1.000（该条命中自己），实际 {repeated.score:.3f}"
        )
    for label, entries_ in (("一字不差", [_must_match(matcher, e.question, entries) for e in entries]),):
        del label, entries_
    return (
        f"一字不差 1.000 命中；同一条夹带提问也是 1.000（夹取）但 score=0（覆盖率 0.20 拦下）；"
        f"{len(every)} 条问自己的分数都在 0~1 内"
    )


@check("命中口径②提问覆盖率：提问的实词大半不在这条里 → 交知识库（边界取 >=）")
def _query_coverage_gate() -> str:
    from project_rag.adapters import BM25TextMatcher

    entries = _gate_corpus()[1]
    only_query = BM25TextMatcher(
        min_query_coverage=EXPECTED_FAQ["min_query_coverage"], min_entry_coverage=0
    )
    blocked = _must_match(only_query, "线程、进程、协程的对比", entries)
    if blocked.score != 0.0:
        raise AssertionError(
            f"提问 5 个词只沾上 1 个（覆盖率 0.20）该被拦下,实际 {blocked.score:.3f}"
        )
    # 边界:覆盖率**正好等于**门槛时必须放行（与阈值那条同一条约定:取等号）
    boundary = _must_match(only_query, "协程 什么", entries)
    if boundary.score <= 0.0:
        raise AssertionError(
            "覆盖率正好 0.5 的提问被拦了 —— 门槛写成了严格大于？口径是 >=（取等号）"
        )
    ok = _must_match(only_query, "Java线程池的7个核心参数", entries)
    if ok.entry.question != "Java线程池的7个核心参数" or abs(ok.score - 1.0) > 1e-9:
        raise AssertionError(f"一字不差该命中自己那条,实际 {ok.entry.question!r} {ok.score:.3f}")
    return f"覆盖率 0.20 → 0；正好 0.50 → 放行（>=）；一字不差 → 1.000"


@check("命中口径③条目覆盖率：条目的词用户没提 → 交知识库（问答两边不是同一件事）")
def _entry_coverage_gate() -> str:
    r"""与②是两个方向:这条拦的是"命中的条目里大半的词用户根本没提"。

    真实表里剩下的那 1 条误命中（「cookie和session的区别」→「&和&&的区别」）**不属于这一类** ——
    `&`/`&&` 在分词那一步被丢了（`tokens()` 只留含 ``\w`` 的词），那条条目在索引里只剩
    「和/的/区别」，于是它与任何"X 和 Y 的区别"都长得一样。修法在分词口径上，记在 deferred。
    """
    from project_rag.adapters import BM25TextMatcher

    entries = _gate_corpus()[1]
    open_matcher = BM25TextMatcher(min_query_coverage=0, min_entry_coverage=0)
    only_entry = BM25TextMatcher(
        min_query_coverage=0, min_entry_coverage=EXPECTED_FAQ["min_entry_coverage"]
    )
    query = "线程池 HashMap 对比学习 垃圾回收"
    loose = _must_match(open_matcher, query, entries)
    if loose.score < 0.98:
        raise AssertionError(
            f"造出来的提问在不设闸门时只有 {loose.score:.3f}（< 阈值），复现不出误命中 —— 语料要改"
        )
    blocked = _must_match(only_entry, query, entries)
    if blocked.score != 0.0:
        raise AssertionError(
            f"条目里大半的词用户没提（覆盖率 < 0.8）该被拦下,实际 {blocked.score:.3f}"
            f"（命中 {blocked.entry.question!r}）"
        )
    return (
        f"不设闸门时会误命中「{loose.entry.question}」({loose.score:.3f})；"
        "只开条目覆盖率 → 0"
    )


@check("命中口径：两道覆盖率可以用 0 关掉（回滚路径），关了以后不再拦")
def _gates_off() -> str:
    from project_rag.adapters import BM25TextMatcher

    from project_rag.adapters import BM25TextMatcher

    entries = _gate_corpus()[1]
    matcher = BM25TextMatcher(min_query_coverage=0, min_entry_coverage=0)
    hit = _must_match(matcher, "线程、进程、协程的对比", entries)
    if hit.score == 0.0:
        raise AssertionError("两道闸门都设 0 之后不该再拦（那是回滚路径）")
    return f"两道设 0 → 该提问又被答掉（score={hit.score:.3f}），回滚路径可用"


@check("命中口径：类默认值与适配器里的常量一致（两处真相迟早漂）")
def _gate_defaults_agree() -> str:
    from project_rag.adapters import matcher_bm25

    defaults = _faq_cls()()
    pairs = (
        ("min_query_coverage", matcher_bm25.DEFAULT_MIN_QUERY_COVERAGE),
        ("min_entry_coverage", matcher_bm25.DEFAULT_MIN_ENTRY_COVERAGE),
    )
    for field, constant in pairs:
        if abs(getattr(defaults, field) - constant) > 1e-9:
            raise AssertionError(
                f"FaqConfig.{field}={getattr(defaults, field)} 与适配器常量 {constant} 不一致 —— "
                "改一处忘另一处，线上行为就和配置说的不一样"
            )
    return "FaqConfig 的两个覆盖率默认值 == adapters.matcher_bm25 的两个 DEFAULT_*"


@check("命中口径对语料规模不敏感：默认规则在 150 条与 600+ 条上给同一批提问同一个判定")
def _scale_invariant() -> str:
    """作者追问"分数不再是 0~1,随着语料漂移怎么办" —— 这一条就是那个问题的答案。

    **比值会漂**（分子分母都含 idf，而 idf=ln((N+1)/df) 随语料变）：实测把 FAQ 库从 150 条撑到
    300 / 617 / 1324 条（混入一份扩充语料池，见下面的 `RAG_EXTRA_FAQ_POOL`），「请问，<原问题>」这一档的**最高**未剪辑比值
    从 1.000 涨到 1.407 / 1.334 / 1.311 —— 所以**任何"比绝对大小"的闸门都会随语料误伤合法提问**
    （曾经试过上界 1.10：放行率从 100% 被砍到 60%/69%/71%，于是删掉了）。

    **覆盖率不漂**：它只是 token 计数比值（`|提问 ∩ 条目| / |提问|` 这类），不含 idf、不含分数。
    这一条用真语料把这件事钉住：同一批提问（每条问自己 / 加礼貌语 / 无关问法 / 作者那条夹带）
    在两种规模的语料上必须给出**逐条相同**的判定。语料池不在本地时 SKIP 并点名 owner。
    """
    import csv as csv_module

    import pymysql

    from project_rag.adapters import BM25TextMatcher
    from project_rag.domain import FaqEntry

    # **扩充语料的路径从环境变量来,源码里不写文件名**：那份 CSV 是作者本机的不公开语料,
    # 它的文件名（含来源机构）不该跟着仓库一起发出去。没设 / 文件不在 → SKIP,
    # 这本来就是它在别人机器上的默认状态。
    pool = os.environ.get("RAG_EXTRA_FAQ_POOL", "").strip()
    pool_path = Path(pool) if pool else None
    if pool_path is None or not pool_path.is_file():
        raise SkipCheck(
            "扩充语料池不在本地 —— 没有它就量不出「规模变大之后还稳不稳」；owner：数据装载。"
            "本机要跑这条就 export RAG_EXTRA_FAQ_POOL=<那份扩充 CSV 的路径>"
        )

    settings = _settings_cls()()
    try:
        conn = pymysql.connect(connect_timeout=5, autocommit=True, **MYSQL_ENV)
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题，不是契约失败
        raise SkipCheck(
            f"MySQL 连不上（{MYSQL_ENV['host']}:{MYSQL_ENV['port']}）：{type(exc).__name__}: {exc}"
        ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, question, answer FROM {settings.faq.table} ORDER BY id ASC")
            live = [FaqEntry(int(i), str(q), str(a)) for i, q, a in cur.fetchall()]
    finally:
        conn.close()
    if not live:
        raise SkipCheck("FAQ 表是空的 —— 先跑 scripts/tools/load_faq_csv.py；owner：数据装载")

    with pool_path.open(encoding="utf-8-sig", newline="") as handle:
        extra_rows = [row for row in csv_module.DictReader(handle) if row.get("问题")]
    extra = [
        FaqEntry(10_000 + index, str(row["问题"]).strip(), "x")
        for index, row in enumerate(extra_rows)
    ]
    grown = live + extra

    matcher = BM25TextMatcher(
        min_query_coverage=settings.faq.min_query_coverage,
        min_entry_coverage=settings.faq.min_entry_coverage,
    )
    probes: list[str] = []
    for entry in live:
        probes += [entry.question, "请问，" + entry.question]
    probes += ["公司年会在哪天", "今天天气怎么样", "午饭吃什么", "线程、进程、协程的对比"]

    def verdicts(entries: list[FaqEntry]) -> list[bool]:
        out: list[bool] = []
        for query in probes:
            hit = matcher.best_match(query, entries)
            out.append(hit is not None and hit.score >= settings.faq.threshold)
        return out

    small, big = verdicts(live), verdicts(grown)
    changed = [
        (probes[index], small[index], big[index])
        for index in range(len(probes))
        if small[index] != big[index]
    ]
    if changed:
        query, was, now = changed[0]
        raise AssertionError(
            f"命中判定随语料规模变了 {len(changed)}/{len(probes)} 条，例：『{query[:30]}』"
            f"（{len(live)} 条时{'命中' if was else '不命中'} → {len(grown)} 条时"
            f"{'命中' if now else '不命中'}）—— 是不是引入了「比绝对大小」的判据"
            "（分数阈值、原始分门槛、idf 相关的量）？那些数会随语料漂（idf=ln((N+1)/df)）；"
            "默认口径只靠两个覆盖率，它们只数词、不漂"
        )
    hit_rate = sum(1 for value in small if value) / len(small)
    return (
        f"{len(probes)} 条探针在 {len(live)} 条与 {len(grown)} 条语料上判定逐条相同"
        f"（命中率 {hit_rate:.0%}）—— 只数词的覆盖率换来的性质"
    )


@check("数据侧：入库校验拒绝「不像问句」的行（两个字的中文主题词）")
def _loader_shape_guard() -> str:
    """入口护栏：FAQ 表里不该出现 `协程` 这种两个字的主题词（作者报障的那一条就是它）。

    算法侧已经有三道闸门兜住**行为**；这一层保证**数据**里不再进这种行 ——
    位子很低但很硬：它拦住的正是"把八个字的标题当问题灌进去"这种批量操作。
    """
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    tool = root / "scripts" / "tools" / "load_faq_csv.py"
    if not tool.is_file():
        # `scripts/tools/` **整目录不公开**（见 .gitignore）。公开 clone 上这个文件不在,
        # 而那不是「布局变了」、更不是「产品坏了」—— 按本项目的口径 SKIP 并点名 owner,
        # 别红一条谁也修不了的 FAIL（`check_m4_eval` 对同一目录一直是这么做的）。
        raise SkipCheck(
            "scripts/tools/load_faq_csv.py 不在这份检出里（该目录不公开）；owner：数据装载"
        )
    spec = importlib.util.spec_from_file_location("load_faq_csv", tool)
    if spec is None or spec.loader is None:  # pragma: no cover - 真加载不了才算布局变了
        raise AssertionError(f"load_faq_csv.py 加载不了:{tool}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # ① **下限必须来自配置**（作者要求"最少词数能在配置里设"）：脚本自己不留默认值 ——
    #    留了就是第二处真源，改了配置而脚本还是旧的，没人看得出来。
    import inspect

    params = inspect.signature(module.question_shape).parameters
    for name in ("min_tokens", "min_chars"):
        if name not in params or params[name].default is not inspect.Parameter.empty:
            raise AssertionError(
                f"question_shape 的 {name} 必须是**必填**参数（不许在脚本里留默认值）—— "
                "下限只能来自 config.yaml 的 faq.min_question_*"
            )
    if hasattr(module, "MIN_QUESTION_TOKENS") or hasattr(module, "MIN_QUESTION_CHARS"):
        raise AssertionError("装载脚本里还留着 MIN_QUESTION_* 常量 —— 口径又变成两处了")

    # ② 用**生效配置**跑判定（配置一改，这里的判定立刻跟着变）
    settings = _settings_cls()()
    min_tokens = settings.faq.min_question_tokens
    min_chars = settings.faq.min_question_chars
    shape = lambda text: module.question_shape(  # noqa: E731 - 检查脚本里够用
        text, min_tokens=min_tokens, min_chars=min_chars
    )
    rejected = {text: shape(text) for text in ("协程", "闭包", "熵", "数字签名", "???")}
    bad = [text for text, reason in rejected.items() if not reason]
    if bad:
        raise AssertionError(f"这些不该通过入库校验（单实词的短条目）：{bad}")
    accepted = [
        "Java线程池的7个核心参数",
        "IllegalArgumentException",  # 1 个实词但够长:标识符也算一个问句
        "什么是协程",
        "&和&&的区别",
    ]
    bad = [text for text in accepted if shape(text)]
    if bad:
        raise AssertionError(f"这些是合格的问句/标识符，不该被拒：{[(t, shape(t)) for t in bad]}")

    # ③ 默认那份 CSV 必须能过（否则装载脚本对新克隆的人就是坏的）
    rows, _dups, _subjects = module.read_rows(
        module.DEFAULT_CSV, min_tokens=min_tokens, min_chars=min_chars
    )
    if len(rows) < 100:
        raise AssertionError(f"默认 CSV 只读出 {len(rows)} 行，是不是被改坏了？")
    return (
        f"下限来自配置（{min_tokens} 实词 / {min_chars} 字）且脚本里没有第二处；"
        f"默认 CSV {len(rows)} 行全过"
    )


def main() -> int:
    return report("M2 FAQ 通道验收")


if __name__ == "__main__":
    sys.exit(main())

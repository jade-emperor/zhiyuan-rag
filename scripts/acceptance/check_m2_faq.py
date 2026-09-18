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

命令：``ask -q/--question`` 必填（缺了退出码 2）。刻意不复用 ``-p/--prompt``：
chat 是「把这句话发给模型」，ask 是「拿这个问题去检索」。

领域模型、端口、用例::

    from project_rag.domain import FaqEntry, FaqMatch, FaqAnswer
    FaqEntry(id: int, question: str, answer: str)
    FaqMatch(entry: FaqEntry, score: float)     # 0~1 的**置信度**，越大越可信
    FaqAnswer(answer: str, source: Literal["faq","llm"], score: float | None = None)

``score`` 的口径归适配器，用例层只认 ``score >= threshold``。当前 ``matcher_bm25.py``
给的是 **top1 原始分 ÷「该条问题问自己」的原始分**（2026-09-18 换的口径，此前是
``(top-second)/top``）。为什么换：旧 margin 口径度量的是**区分度**而不是相似度，分数随
**语料冗余度**衰减——库里只要有一条近似重复问法，完全一致的问题也会掉到 0.14；而且
条目越多分数越低，阈值被迫随语料规模漂移，标不出来（实测 N=3 时真命中 0.411 →
N=50 时 0.335，同期无关问法最高 0.481，阈值区间是空集）。新口径分子分母同源，
实测一字不差 = 1.000（与语料规模无关）、改写问法 0.2~0.7、完全无关 ≤ 0.274（N=50），
所以 0.85 有明确含义：**只答近乎一字不差的问题**。

**改写问法不归本通道管**：FAQ 通道只直返人写的标准答案，改写由 M3 的向量检索通道
负责。本脚本最后一条用例只覆盖「加礼貌语」级别的改写；真正的改写评估集在 M3 的
``data/eval/`` 里，判定标准也到那时重定。契约只保证三件事：0~1、越大越可信、判定用 ``>=``。

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
``rank_bm25`` 只准出现在 ``adapters/``；``FaqStore`` / ``TextMatcher`` / ``Cache``
每个都必须有类显式继承。

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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARK = {PASS: "✔", FAIL: "✘", SKIP: "○"}
_results: list[tuple[str, str, str]] = []

SRC = Path(__file__).resolve().parents[2] / "src" / "project_rag"

# 契约默认值（与 config/config.yaml / docker-compose.yaml 必须一致）
# 0.85 是「自身分归一化」口径下的默认值：一字不差 = 1.000、完全无关 ≤ 0.274(N=50)，
# 所以 0.85 = 只答近乎一字不差的问题。口径定义见 matcher_bm25.py 与本脚本 docstring。
DEFAULT_THRESHOLD = 0.85
EXPECTED_MYSQL = {"host": "127.0.0.1", "port": 10093, "user": "rag", "database": "rag"}
EXPECTED_REDIS = {"host": "127.0.0.1", "port": 10087, "db": 0}
EXPECTED_FAQ = {"threshold": 0.85, "table": "faq"}

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


class SkipCheck(Exception):
    """前置条件（容器）没起来，该检查无法判定；必须在报告里说清原因。"""


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def check(name: str):
    def decorate(fn):
        try:
            detail = fn() or ""
            record(name, PASS, str(detail))
        except SkipCheck as exc:
            record(name, SKIP, str(exc))
        except SystemExit as exc:
            # argparse 的用法错误抛的是 SystemExit，它继承 BaseException 而不是 Exception，
            # 不拦就会穿透装饰器、打断模块 import —— 结果是**整份报告一行都打不出来**。
            # 契约未落地时（子命令/参数还不存在）必须走这里，而不是让脚本静默死掉。
            record(name, FAIL, f"SystemExit({exc.code})：子命令或参数还没实现，契约未落地")
        except Exception as exc:  # noqa: BLE001 - 验收脚本要显示任何失败
            record(name, FAIL, f"{type(exc).__name__}: {exc}")
        return fn

    return decorate


@contextmanager
def env(**kv: str | None):
    """临时设置环境变量；传 None 表示临时删除。"""
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for key, value in kv.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
    return getter() if callable(getter) else str(value)


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
        return iter(self.chunks)


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

    if fields["FaqAnswer"] != ("answer", "source", "score"):
        raise AssertionError(
            f"FaqAnswer 字段应为 ('answer','source','score')，实际 {fields['FaqAnswer']}"
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


@check("组合根：Deps 是 frozen+slots dataclass，字段 = 4 个工厂 + faq_threshold")
def _() -> str:
    Deps = _deps_cls()
    if not dataclasses.is_dataclass(Deps):
        raise AssertionError("Deps 不是 dataclass；见本脚本头部契约第 1 条")
    if not getattr(Deps, "__dataclass_params__").frozen:
        raise AssertionError("Deps 应为 frozen=True（组合根一旦装配好就不该被子命令改）")
    if not getattr(Deps, "__slots__", None):
        raise AssertionError("Deps 应为 slots=True（跟领域模型风格一致）")

    fields = {f.name: f for f in dataclasses.fields(Deps)}
    want_factories = ("llm_factory", "faq_store_factory", "matcher_factory", "cache_factory")
    missing = [n for n in want_factories + ("faq_threshold",) if n not in fields]
    extra = sorted(set(fields) - set(want_factories) - {"faq_threshold"})
    if missing or extra:
        raise AssertionError(
            f"Deps 字段不对：缺 {missing}，多 {extra}；应为 {want_factories + ('faq_threshold',)}"
        )
    for name in want_factories:
        if fields[name].default is not None:
            raise AssertionError(f"Deps.{name} 默认值应为 None（不注入就不造），实际 {fields[name].default!r}")
    if fields["faq_threshold"].default != DEFAULT_THRESHOLD:
        raise AssertionError(
            f"Deps.faq_threshold 默认应为 {DEFAULT_THRESHOLD}，实际 {fields['faq_threshold'].default!r}"
        )
    return "字段与默认值一致"


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
    if abs(settings.faq.threshold - EXPECTED_FAQ["threshold"]) > 1e-9:
        raise AssertionError(
            f"faq.threshold 期望 {EXPECTED_FAQ['threshold']}，实际 {settings.faq.threshold}"
        )
    if settings.faq.table != EXPECTED_FAQ["table"]:
        raise AssertionError(f"faq.table 期望 {EXPECTED_FAQ['table']!r}，实际 {settings.faq.table!r}")
    return (
        f"mysql={settings.mysql.host}:{settings.mysql.port} "
        f"redis={settings.redis.host}:{settings.redis.port} "
        f"threshold={settings.faq.threshold} table={settings.faq.table}"
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


@check("三个新端口都有类显式继承（端口不能是没人执行的合同）")
def _() -> str:
    module = _require("project_rag.ports", ("FaqStore", "TextMatcher", "Cache"))
    targets = {"FaqStore", "TextMatcher", "Cache"}
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "ports" in path.parts:  # 端口自己不算实现
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                if name in targets:
                    found.setdefault(name, []).append(f"{path.relative_to(SRC)}::{node.name}")
    missing = sorted(targets - set(found))
    if missing:
        raise AssertionError(
            f"这些端口没有任何显式实现：{missing}。"
            "结构化子类型只在类型检查器眼里成立——要么让适配器显式继承端口，"
            "要么端口就只是一份没人执行的合同（同 M1 的 LLMClient 规则）"
        )
    return ", ".join(f"{k} ← {v[0]}" for k, v in sorted(found.items()))


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


@check("--help 列出 ask；ask 缺 -q 退出码 2")
def _() -> str:
    helped = _run_cli(["--help"])
    if helped.returncode != 0:
        raise AssertionError(f"--help 退出码 {helped.returncode}：{helped.stderr[:200]}")
    if "ask" not in helped.stdout:
        raise AssertionError("--help 里没列出 ask 子命令（commands.ALL 里注册了吗？）")
    missing = _run_cli(["ask"])
    if missing.returncode != 2:
        raise AssertionError(
            f"ask 缺 -q 退出码应为 2（argparse 用法错误），实际 {missing.returncode}；"
            "别把 -q 写成可选，否则空问题会一路走到匹配层"
        )
    return "help 列出 ask；缺 -q → 2"


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
        code = cli(["ask", "-q", "怎么退款"], deps=deps)
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
        code = cli(["ask", "-q", "公司年会在哪天"], deps=deps)
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
        code = cli(["ask", "-q", "怎么退款"], deps=deps)
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
        ["ask", "-q", "怎么退款"],
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
            ["ask", "-q", CHINESE_Q],
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
            ["ask", "-q", CHINESE_Q],  # 原句提问：口径正确就必然 1.000 命中
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
    # 是对的，改写能力由 M3 的向量检索通道提供，评估集见 M3 的 data/eval/。
    rows = [
        (1, CHINESE_Q, CHINESE_A),
        (2, "如何修改收货地址？", "在订单详情页点「修改地址」。"),
        (3, "发票怎么开？", "在订单页申请开票，电子票发到邮箱。"),
    ]
    table = _seed_faq(rows)
    try:
        proc = _run_cli(
            ["ask", "-q", "请问，" + CHINESE_Q],
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


def main() -> int:
    print("M2 FAQ 通道验收\n" + "─" * 64)
    for name, status, detail in _results:
        suffix = f"  — {detail}" if detail else ""
        print(f"  {_MARK[status]} [{status}] {name}{suffix}")
    failed = [name for name, status, _ in _results if status == FAIL]
    skipped = [name for name, status, _ in _results if status == SKIP]
    print("─" * 64)
    print(
        f"结果：{len(_results) - len(failed) - len(skipped)} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过"
    )
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✘ {name}")
    if skipped:
        # 两种跳过都不算通过：① 前置条件没满足（容器没起）；② 计划内未实现，
        # 但必须在理由里点名 owner 里程碑。理由串就是报告，别让它退化成一句「跳过」。
        print("\n跳过（不算通过；理由见上方每条括注，计划内的必须点名 owner）：")
        for name in skipped:
            print(f"  ○ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

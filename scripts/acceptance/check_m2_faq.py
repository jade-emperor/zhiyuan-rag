#!/usr/bin/env python
"""M2 验收：FAQ 通道（MySQL 存问答对 → 相似匹配 → 命中直答 / 未命中 LLM 兜底）。

用法::

    uv run python scripts/acceptance/check_m2_faq.py

本脚本是 **M2 契约的唯一权威**：`docs/STATUS.md` 第 8 节是草案，冲突以本脚本为准。
定稿时把草案没说死的 5 件事写死了：

1. **组合根换成 Deps dataclass**（替换 M1 的 ``llm_factory=`` 关键字）::

       from project_rag.cli import cli, Deps
       cli(argv=None, *, deps: Deps | None = None) -> int

   ``deps=None`` = 真实装配（读 Settings、装日志、连 MySQL/Redis）；
   传入 ``deps`` = 不读 .env、不装日志、不联网（沿用 M1 那条缝的语义，
   所以 M2 的 CLI 链路照样可测）。Deps 字段全是**工厂**，保持
   「用不到就不造」的惰性（chat 不该因为 MySQL 挂了就跑不起来）::

       llm_factory:       Callable[[], LLMClient]    | None = None
       faq_store_factory: Callable[[], FaqStore]     | None = None
       matcher_factory:   Callable[[], TextMatcher]  | None = None
       cache_factory:     Callable[[], Cache | None] | None = None   # 返回 None = 无缓存
       faq_threshold:     float = 0.85

   注入 Deps 时，命令只允许调用自己需要的工厂；需要而没给 → fail-fast 并在错误里点名。

2. **ask 子命令**：``ask -q/--question``（必填）。刻意不复用 ``-p/--prompt``：
   chat 是「把这句话发给模型」，ask 是「拿这个问题去检索」，语义已经分叉。

3. **阈值语义**：``score >= threshold`` 算命中（边界取等号），
   低于阈值才走 LLM 兜底；阈值来自 ``RAG_FAQ__THRESHOLD``，经 ``Deps.faq_threshold`` 注入。

4. **缓存降级**：Redis 是纯加速，**装配期**（``cache_factory`` 抛错）和**调用期**
   （``cache.get/set`` 抛错）都必须降级——答案与退出码不受影响。
   MySQL 相反：连不上必须 fail-fast，错误信息里要点名连接目标（host:port）。

5. **种子数据归属**：FaqStore 端口只有 ``load_all()``（写入/增量更新见 deferred.md），
   所以端到端用例由本脚本用 pymysql 直接建表 + 插入 + 清理。表结构是本契约的一部分::

       CREATE TABLE {table} (
           id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
           question VARCHAR(512) NOT NULL,
           answer   TEXT NOT NULL
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci

   表名由 ``RAG_FAQ__TABLE`` 配置；``load_all()`` 按 id 升序返回。

固化的公开契约（import 路径本身也是承诺）：

* ``from project_rag.domain import FaqEntry, FaqMatch, FaqAnswer``

  - ``FaqEntry(id: int, question: str, answer: str)``
  - ``FaqMatch(entry: FaqEntry, score: float)``   # score 0~1，越高越像
  - ``FaqAnswer(answer: str, source: Literal["faq","llm"], score: float | None = None)``

* ``from project_rag.ports import FaqStore, TextMatcher, Cache``

  - ``FaqStore.load_all() -> Sequence[FaqEntry]``
  - ``TextMatcher.best_match(query, candidates) -> FaqMatch | None``
  - ``Cache.get(key) -> str | None`` / ``Cache.set(key, value, ttl) -> None``
    缓存 key 怎么构造是适配器的自由；value 必须是**答案字符串**（MVP 只缓存答案）。

* ``from project_rag.application import answer_faq``

  - ``answer_faq(query, *, store, matcher, llm, cache=None, threshold=0.85, cache_ttl=300) -> FaqAnswer``
  - 顺序：先查缓存 → 命中直接返回；否则 ``store.load_all()`` + ``matcher.best_match()``；
    ``score >= threshold`` 直接返回；否则用 LLM 兜底（``source="llm"``）。
  - 缓存异常一律吞掉（记 warning），不影响答案；LLM 兜底答案是否写缓存**不在本契约内**。

* 分层（静态扫描，不靠自觉）：

  - ``application/`` 只准依赖 标准库 + ``project_rag.domain`` / ``project_rag.ports`` / ``project_rag.observability``；
  - ``pymysql`` / ``redis`` / ``jieba`` 只准出现在 ``adapters/``；
  - ``FaqStore`` / ``TextMatcher`` / ``Cache`` 每个都必须有类显式继承。

为什么这一层要有缓存短路检查：缓存存在的唯一理由就是「同一句话第二次别再查库、别再匹配」。
只测「写进去了」不测「读出来了」，等于没测缓存。
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
DEFAULT_THRESHOLD = 0.85
EXPECTED_MYSQL = {"host": "127.0.0.1", "port": 10093, "user": "rag", "database": "rag"}
EXPECTED_REDIS = {"host": "127.0.0.1", "port": 10087, "db": 0}
EXPECTED_FAQ = {"threshold": 0.85, "table": "faq"}

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

# M2 新引入的第三方 SDK：只准出现在 adapters/（pymysql/redis 必须有实际使用点，
# 否则下面的分层扫描会在「一条都没用到」时假装通过）
M2_SDK_ROOTS = ("pymysql", "redis", "jieba")
M2_SDK_REQUIRED = ("pymysql", "redis")

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
    if not any(value == CHINESE_A and isinstance(ttl, int) and ttl > 0 for _, value, ttl in cache.sets):
        raise AssertionError(
            f"缓存应写入 (key, {CHINESE_A!r}, 正整数 ttl)，实际 {cache.sets!r}"
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
        cache=FakeCache({key: CHINESE_A}),
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
            "先 docker compose up -d，或跑 check_env_services.py 定位"
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


@check("中文端到端：落库中文问答 → 换一种措辞提问 → 命中正确的那一条")
def _() -> str:
    # 阈值压到 0.5 是为了让这个检查只验证「中文链路通不通」（tokenizer/编码/表读取），
    # 不验证匹配质量——匹配质量属于 M5 的评估闭环，用真实语料说话。
    rows = [
        (1, CHINESE_Q, CHINESE_A),
        (2, "如何修改收货地址？", "在订单详情页点「修改地址」。"),
        (3, "发票怎么开？", "在订单页申请开票，电子票发到邮箱。"),
    ]
    table = _seed_faq(rows)
    try:
        proc = _run_cli(
            ["ask", "-q", "请问，退款流程怎么走？"],
            {"RAG_FAQ__TABLE": table, "RAG_FAQ__THRESHOLD": "0.5", "RAG_LLM__TIMEOUT_S": "8"},
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"退出码 {proc.returncode}；stderr={proc.stderr[-300:]!r}"
            )
        if CHINESE_A not in proc.stdout:
            raise AssertionError(
                f"改写后的问法没命中退款那一条（阈值 0.5），实际 stdout={proc.stdout!r}。"
                "排查顺序：jieba 分词是否生效 → 表读取编码是否 utf8mb4 → 阈值是否真的从 "
                "RAG_FAQ__THRESHOLD 读到了"
            )
        return f"『请问，退款流程怎么走？』→ 命中『{CHINESE_Q}』"
    finally:
        _drop_faq(table)


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
        print("\n跳过（前置条件未满足，不算通过）：")
        for name in skipped:
            print(f"  ○ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

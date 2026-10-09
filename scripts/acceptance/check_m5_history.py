#!/usr/bin/env python
"""M5.2 验收：对话历史 —— conversations 表 + 最近 N 轮。

用法::

    uv run python scripts/acceptance/check_m5_history.py

本脚本是 **M5.2 契约的唯一权威**（`docs/STATUS.md` §0.1 / §3 只留设计理由，不重复规格）。
6.1 的路由契约在 `check_m5_qa.py`，这里只管「历史怎么存、怎么读、什么时候读写」。

公开契约（import 路径本身也是承诺）::

    from project_rag.domain import ConversationTurn
    ConversationTurn(question: str, answer: str)

    from project_rag.ports import ConversationStore
    ConversationStore.record(session_id, turn, *, keep: int) -> None
    ConversationStore.recent(session_id, *, limit: int) -> Sequence[ConversationTurn]
    ConversationStore.clear(session_id) -> int          # 返回删掉的轮数

    from project_rag.adapters import MySQLConversationStore
    MySQLConversationStore(mysql_config, conversation_config)   # 表名非法 → ValueError
    MySQLConversationStore.ensure_table() -> None               # 幂等（适配器方法，不是端口）

    from project_rag.application import Session
    Session(store: ConversationStore, session_id: str, rounds: int = 5)
        # 空 session_id / rounds <= 0 → ValueError（半成品在类型上就构造不出来）

    answer_question(query, *, faq_store, matcher, knowledge, faq_threshold=0.85,
                    session: Session | None = None) -> QaAnswer
    knowledge: Callable[[Sequence[ConversationTurn]], RagAnswer]   # 历史由用例读出来交进来

**端口承诺的两件事**（实现必须满足，本脚本用真 MySQL 逐条验）::

    recent(...) 返回的是**时间正序**（最旧在前）—— 就算底层是 ORDER BY id DESC 取的，
                返回前也要翻正；提示词按正序读才像一段对话。
    record(..., keep=N) 写入这一轮之后，该会话只留**最近 N 轮**，且写入与裁剪
                在**同一次事务**里 —— 失败不留半条、也不留「裁了一半」的中间态。

表结构是契约的一部分（改这里就要同步 `adapters/conversation_mysql.py`）::

    CREATE TABLE {table} (
        id         BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(36) NOT NULL,
        question   TEXT NOT NULL,
        answer     TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_session_id (session_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci

建表不在适配器的 `__init__` 里（构造不该有副作用），生产上由
``scripts/tools/init_conversation.py`` 跑一次。

**四处刻意的偏离**（都有理由，别改回去）:

1. **排序用 `id` 不用 `timestamp`**:DATETIME 只精确到秒，同一秒内的两轮谁先谁后由
   MySQL 定 —— 「最近 5 轮」会随排序抖动，而这种抖动只在连续快速提问时出现，极难查。
2. **列名 `created_at` 不是 `timestamp`**:后者与 MySQL 类型名同名，读 SQL 容易看岔。
3. **裁剪的 DELETE 带一层派生表**:MySQL 不支持 `IN (SELECT ... LIMIT n)`，
   那层 `SELECT ... FROM (...) AS keep_rows` 去掉就报语法错（原始写法就是这样）。
4. **表名过白名单**:表名要拼进 SQL（参数化管不到表名），配置写错就可能拼出别的语句。

用例语义（`answer_question` + `Session`，本脚本用全假端口逐条钉）::

    session is None            → 完全不碰会话存储（不读也不写）
    FAQ 命中 + 有会话           → **不读**历史（标准答案不需要上文），但这一轮**要记**
    知识库命中 + 有会话         → 读最近 rounds 轮 → 交给 knowledge() → 记这一轮
    知识库拒答 + 有会话         → 一样记（拒答也是一轮，不然下一轮的「上一句」是错的）
    recent / record 抛错        → **原样上抛**：历史不是可选装饰，写不进去必须当场可见

配置（`config/config.yaml` 的 `conversation` 段）::

    table: conversations          # 表名
    history_rounds: 5             # 读写共用:读最近 N 轮,写的时候裁到最近 N 轮

命令：``qa --session <id>``（不给就完全没有会话）。带了 `--session` 却没装
`conversation_store_factory` → 退出码 1 并点名（fail-fast，不静默丢掉历史）。
"""

from __future__ import annotations

import ast
import importlib
import io
import os
import subprocess
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from _harness import check, report, env, SkipCheck
from _fakes import FakeConversationStore, FakeEmbedder, FakeFaqStore, FakeKeywordIndex, FakeLLM, FakeMatcher, FakeReranker, FakeVectorStore

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"

DEFAULT_ROUNDS = 5

FAQ_QUESTION = "退款流程怎么走？"
FAQ_ANSWER = "在订单页点「申请退款」，1-3 个工作日到账。"
KB_ANSWER = "装饰器是给函数加行为的语法 [1]"
KB_PARENT = "装饰器是一个接收函数并返回新函数的函数。"
SESSION_A = "sess-aaaa-0001"
SESSION_B = "sess-bbbb-0002"

MYSQL_ENV = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "10093")),
    "user": os.environ.get("MYSQL_USER", "rag"),
    "password": os.environ.get("MYSQL_PASSWORD", "1234"),
    "database": os.environ.get("MYSQL_DATABASE", "rag"),
    "charset": "utf8mb4",
}

# 与 adapters/conversation_mysql.py 里的 _CREATE_TABLE 是同一份契约
CONVERSATION_DDL = (
    "CREATE TABLE {table} ("
    "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
    "session_id VARCHAR(36) NOT NULL, "
    "question TEXT NOT NULL, "
    "answer TEXT NOT NULL, "
    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "INDEX idx_session_id (session_id)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)
# 多一个「非空且无默认值」的列:适配器的 INSERT 不会带它 → 写入必失败,
# 正好用来验「写失败时事务回滚、旧数据还在」
BROKEN_DDL = (
    "CREATE TABLE {table} ("
    "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
    "session_id VARCHAR(36) NOT NULL, "
    "question TEXT NOT NULL, "
    "answer TEXT NOT NULL, "
    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "extra_required VARCHAR(8) NOT NULL, "
    "INDEX idx_session_id (session_id)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


def _imports_of(path: Path) -> list[tuple[str, int]]:
    """输入:一个 .py 文件。行为:解析出它 import 的 (模块名, 相对层数)。输出:列表。"""
    pairs: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            pairs.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            pairs.append((node.module or "", node.level))
    return pairs


def _require(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise AssertionError(
            f"{module_name} 缺少公开成员 {missing}；"
            f"请在 {module_name.replace('.', '/')}/__init__.py 里显式 re-export"
        )
    return module


def _turn_type() -> Any:
    return _require("project_rag.domain", ("ConversationTurn",)).ConversationTurn


def _store_protocol() -> Any:
    return _require("project_rag.ports", ("ConversationStore",)).ConversationStore


def _mysql_store_cls() -> Any:
    return _require("project_rag.adapters", ("MySQLConversationStore",)).MySQLConversationStore


def _session_type() -> Any:
    return _require("project_rag.application", ("Session",)).Session


def _answer_question() -> Any:
    return _require("project_rag.application", ("answer_question",)).answer_question


def _conversation_config_type():
    from project_rag.config import ConversationConfig

    return ConversationConfig


# ── 假端口（不联网、不连库） ──────────────────────────────────────────────────


def _faq_entry():
    FaqEntry = _require("project_rag.domain", ("FaqEntry",)).FaqEntry
    return FaqEntry(id=7, question=FAQ_QUESTION, answer=FAQ_ANSWER)


def _kb_hit():
    from project_rag.domain import Chunk, RetrievedChunk

    chunk = Chunk("c1", "装饰器 子块", "ai", "d1", "p1", KB_PARENT)
    return RetrievedChunk(chunk=chunk, score=0.91)


class _World:
    """一整套假端口 + 记录（会话层用）。`knowledge` 记下被调用几次、收到什么历史。"""

    def __init__(
        self,
        faq_score: float | None = None,
        *,
        turns=None,
        rag=None,
        hits=None,
        record_error=None,
        read_error=None,
    ) -> None:
        from project_rag.domain import RagAnswer

        self.llm = FakeLLM(reply=KB_ANSWER)
        self.embedder = FakeEmbedder()
        self.vector_store = FakeVectorStore([_kb_hit()] if hits is None else hits)
        self.keyword_index = FakeKeywordIndex()
        self.faq_store = FakeFaqStore([_faq_entry()])
        self.matcher = FakeMatcher(faq_score)
        self.conversation = FakeConversationStore(
            turns=turns, record_error=record_error, read_error=read_error
        )
        if rag is not None:
            self.rag = rag
        else:
            from project_rag.domain import Citation

            chunks = list(self.vector_store.hits)
            self.rag = RagAnswer(
                answer=KB_ANSWER,
                citations=tuple(
                    Citation(index=i, chunk=hit.chunk, score=hit.score, routes=("vector",))
                    for i, hit in enumerate(chunks, start=1)
                ),
            )
        self.knowledge_calls = 0
        self.knowledge_history: tuple = ()

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

    def session(self, session_id: str = SESSION_A, rounds: int = DEFAULT_ROUNDS):
        return _session_type()(
            store=self.conversation, session_id=session_id, rounds=rounds
        )

    def ask(self, query: str = FAQ_QUESTION, *, session=None, threshold: float = 0.85):
        return _answer_question()(
            query,
            faq_store=self.faq_store,
            matcher=self.matcher,
            knowledge=self.knowledge,
            faq_threshold=threshold,
            session=session,
        )


def _deps(world: _World, **overrides):
    """按 Deps 契约装一整套假工厂（含 M5.2 的会话工厂）。"""
    from project_rag.cli import Deps

    fields = {
        "llm_factory": lambda: world.llm,
        "faq_store_factory": lambda: world.faq_store,
        "matcher_factory": lambda: world.matcher,
        "cache_factory": None,
        "faq_threshold": 0.85,
        "embedder_factory": lambda: world.embedder,
        "vector_store_factory": lambda: world.vector_store,
        "keyword_index_factory": lambda: world.keyword_index,
        "reranker_factory": lambda: FakeReranker(),
        "conversation_store_factory": lambda: world.conversation,
    }
    fields.update(overrides)
    return Deps(**fields)


def _run_injected(argv: list[str], deps) -> tuple[int, str, str]:
    from project_rag.cli import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


def _mysql_connect(autocommit: bool = True):
    import pymysql

    target = f"{MYSQL_ENV['host']}:{MYSQL_ENV['port']}"
    try:
        return pymysql.connect(connect_timeout=5, autocommit=autocommit, **MYSQL_ENV)
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题，不是契约失败
        raise SkipCheck(
            f"MySQL 连不上（{target}）：{type(exc).__name__}: {exc}；"
            "先看 Windows 侧 Docker Desktop 是否开着（docs/STATUS.md §5 第 10 条），"
            "或跑 check_env_services.py 定位"
        ) from exc


def _create_table(ddl: str = CONVERSATION_DDL) -> str:
    """建一张临时会话表；返回表名（调用方负责删）。"""
    table = f"conv_m5_{uuid.uuid4().hex[:8]}"
    conn = _mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl.format(table=table))
    finally:
        conn.close()
    return table


def _drop_table(table: str) -> None:
    try:
        conn = _mysql_connect()
    except SkipCheck:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        conn.close()


def _store_for(table: str):
    from project_rag.config import get_settings

    settings = get_settings()
    store = _mysql_store_cls()(
        settings.mysql, _conversation_config_type()(table=table)
    )
    store.ensure_table()
    return store


def _seed_row(table: str, session_id: str, question: str, answer: str, extra=None) -> None:
    conn = _mysql_connect()
    try:
        with conn.cursor() as cur:
            if extra is None:
                cur.execute(
                    f"INSERT INTO {table} (session_id, question, answer) VALUES (%s, %s, %s)",
                    (session_id, question, answer),
                )
            else:
                cur.execute(
                    f"INSERT INTO {table} (session_id, question, answer, extra_required) "
                    "VALUES (%s, %s, %s, %s)",
                    (session_id, question, answer, extra),
                )
    finally:
        conn.close()


def _count(table: str, session_id: str | None = None) -> int:
    conn = _mysql_connect()
    try:
        with conn.cursor() as cur:
            if session_id is None:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
            else:
                cur.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = %s", (session_id,)
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    finally:
        conn.close()


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


# ── 1. 领域 / 端口 / 配置 / 组合根 ───────────────────────────────────────────
@check("domain：ConversationTurn 是 frozen+slots dataclass，字段 question/answer，包入口可导入")
def _() -> str:
    import dataclasses

    Turn = _turn_type()
    if not dataclasses.is_dataclass(Turn):
        raise AssertionError("ConversationTurn 不是 dataclass")
    if not getattr(Turn, "__dataclass_params__").frozen:
        raise AssertionError("ConversationTurn 应为 frozen=True（历史是只读的）")
    if not getattr(Turn, "__slots__", None):
        raise AssertionError("ConversationTurn 应为 slots=True（与其它领域模型一致）")
    names = [field.name for field in dataclasses.fields(Turn)]
    if names != ["question", "answer"]:
        raise AssertionError(f"字段应为 ['question','answer']，实际 {names}")
    exported = getattr(importlib.import_module("project_rag.domain"), "__all__", [])
    if "ConversationTurn" not in exported:
        raise AssertionError("domain/__init__.py 的 __all__ 里缺 ConversationTurn")
    return "字段与默认值一致，且从包入口可导入"


@check("端口：ConversationStore 是 Protocol，record/recent/clear 签名按契约")
def _() -> str:
    import inspect

    proto = _store_protocol()
    for name in ("record", "recent", "clear"):
        if not hasattr(proto, name):
            raise AssertionError(f"ConversationStore 缺 {name}()")

    record_params = inspect.signature(proto.record).parameters
    if list(record_params) != ["self", "session_id", "turn", "keep"]:
        raise AssertionError(f"record 参数应为 [self,session_id,turn,keep]，实际 {list(record_params)}")
    if record_params["keep"].kind is not inspect.Parameter.KEYWORD_ONLY:
        raise AssertionError("record 的 keep 必须是关键字参数（避免和 turn 位置搞混）")
    if record_params["keep"].default is not inspect.Parameter.empty:
        raise AssertionError("record 的 keep 不能有默认值 —— 保留几轮只能有一个真值来源（配置）")

    recent_params = inspect.signature(proto.recent).parameters
    if list(recent_params) != ["self", "session_id", "limit"]:
        raise AssertionError(f"recent 参数应为 [self,session_id,limit]，实际 {list(recent_params)}")
    if recent_params["limit"].kind is not inspect.Parameter.KEYWORD_ONLY:
        raise AssertionError("recent 的 limit 必须是关键字参数")

    if list(inspect.signature(proto.clear).parameters) != ["self", "session_id"]:
        raise AssertionError("clear 只该收一个 session_id")
    return "三个方法签名与契约一致"


@check("配置：conversation 段默认值 + 可被环境变量覆盖（RAG_CONVERSATION__HISTORY_ROUNDS）")
def _() -> str:
    ConversationConfig = _conversation_config_type()
    defaults = ConversationConfig()
    if defaults.table != "conversations":
        raise AssertionError(f"默认表名应为 conversations，实际 {defaults.table!r}")
    if defaults.history_rounds != DEFAULT_ROUNDS:
        raise AssertionError(f"默认保留轮数应为 {DEFAULT_ROUNDS}，实际 {defaults.history_rounds!r}")

    with env(RAG_CONVERSATION__HISTORY_ROUNDS="3"):
        from project_rag.config import get_settings

        get_settings.cache_clear()
        try:
            if get_settings().conversation.history_rounds != 3:
                raise AssertionError("环境变量没能覆盖 history_rounds")
        finally:
            get_settings.cache_clear()
    return "默认 conversations / 5，env 可覆盖"


@check("配置：config.yaml 里确实写了 conversation 段（代码里有默认值 ≠ 配置文件里有）")
def _() -> str:
    from project_rag.config import CONFIG_FILE, get_settings

    text = CONFIG_FILE.read_text(encoding="utf-8")
    missing = [key for key in ("conversation:", "table:", "history_rounds:") if key not in text]
    if missing:
        raise AssertionError(f"config/config.yaml 里缺 {missing}")
    settings = get_settings()
    if settings.conversation.table != "conversations":
        raise AssertionError(f"生效的表名是 {settings.conversation.table!r}")
    return f"yaml 与生效值一致（{settings.conversation.table} / {settings.conversation.history_rounds} 轮）"


@check("组合根：Deps.conversation_store_factory 默认 None（惰性：不给就不造）")
def _() -> str:
    import dataclasses

    from project_rag.cli import Deps

    fields = {field.name: field for field in dataclasses.fields(Deps)}
    if "conversation_store_factory" not in fields:
        raise AssertionError("Deps 缺 conversation_store_factory")
    if fields["conversation_store_factory"].default is not None:
        raise AssertionError("默认值应为 None（不注入就不造）")
    return "字段存在且默认 None"


@check("组合根：真实装配接的是 MySQLConversationStore，不是内存实现")
def _() -> str:
    import inspect

    from project_rag.cli.deps import build_real_deps

    source = inspect.getsource(build_real_deps)
    if "MySQLConversationStore" not in source:
        raise AssertionError("build_real_deps 里没有装配 MySQLConversationStore")
    if "settings.conversation" not in source:
        raise AssertionError("装配时没把 settings.conversation 传进适配器（表名从哪来？）")
    return "build_real_deps 接 MySQLConversationStore(settings.mysql, settings.conversation)"


@check("分层：端口只依赖 标准库 + domain；pymysql 只出现在适配器里")
def _() -> str:
    port = SRC / "ports" / "conversation.py"
    if not port.exists():
        raise AssertionError("src/project_rag/ports/conversation.py 还没落地")
    bad = [
        module
        for module, level in _imports_of(port)
        if level == 0
        and module.split(".")[0] not in sys.stdlib_module_names
        and not module.startswith("project_rag.domain")
        and module != "project_rag"
    ]
    if bad:
        raise AssertionError(f"端口里出现了不该有的依赖：{bad}")

    adapter = SRC / "adapters" / "conversation_mysql.py"
    if not adapter.exists():
        raise AssertionError("src/project_rag/adapters/conversation_mysql.py 还没落地")
    leaked = [
        f"{path.relative_to(SRC)} 也 import 了 {module.split('.')[0]}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "conversation_mysql.py" and path.parts[-2] != "adapters"
        for module, level in _imports_of(path)
        if level == 0 and module.split(".")[0] == "pymysql"
    ]
    if leaked:
        raise AssertionError(f"pymysql 泄漏到适配器之外：{leaked}")
    return "端口干净；pymysql 只在 adapters/"


@check("Session：frozen+slots；空 session_id 与 rounds<=0 都在构造时 fail-fast")
def _() -> str:
    import dataclasses

    Session = _session_type()
    # 注意:is_dataclass / fields 都传**表达式**而不是变量 —— 它们是 TypeGuard,
    # 会把变量收窄成「DataclassInstance | type」，于是下面 Session(...) 会被 pyright
    # 判成「实例不可调用」。传表达式就只收窄那个临时值,变量仍是 Any。
    if not dataclasses.is_dataclass(
        _session_type()
    ) or not getattr(_session_type(), "__dataclass_params__").frozen:
        raise AssertionError("Session 应为 frozen dataclass")
    names = [field.name for field in dataclasses.fields(_session_type())]
    if names != ["store", "session_id", "rounds"]:
        raise AssertionError(f"字段应为 ['store','session_id','rounds']，实际 {names}")
    if dataclasses.fields(_session_type())[2].default != DEFAULT_ROUNDS:
        raise AssertionError("rounds 默认值应为 5（与配置默认一致）")

    store = FakeConversationStore()
    for kwargs, why in (
        ({"session_id": "  "}, "空白 session_id"),
        ({"session_id": SESSION_A, "rounds": 0}, "rounds=0"),
    ):
        try:
            Session(store=store, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"{why} 应该在构造 Session 时就报错（半成品要构造不出来）")
    return "字段与默认值一致；两个半成品都被拦住"


# ── 2. 适配器（真 MySQL；容器不在则 SKIP） ───────────────────────────────────
@check("真实链路：ensure_table 幂等，且表结构带 session_id 索引")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)  # ensure_table 第一次
        store.ensure_table()  # 第二次必须不报错
        conn = _mysql_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW INDEX FROM {table}")
                rows = cur.fetchall()
                indexes = {str(row[2]) for row in rows}
        finally:
            conn.close()
        if "idx_session_id" not in indexes:
            raise AssertionError(f"表上没建 idx_session_id 索引，实际索引 {sorted(indexes)}")
        return f"表 {table} 建了两次都没事，索引 {sorted(indexes)}"
    finally:
        _drop_table(table)


@check("真实链路：record → recent 往返，且是时间正序（最旧在前）")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        for i in range(1, 4):
            store.record(SESSION_A, Turn(question=f"q{i}", answer=f"a{i}"), keep=5)
        turns = store.recent(SESSION_A, limit=5)
        got = [(t.question, t.answer) for t in turns]
        want = [("q1", "a1"), ("q2", "a2"), ("q3", "a3")]
        if got != want:
            raise AssertionError(f"应为时间正序 {want}，实际 {got}")
        return f"三轮原样往返（{got}）"
    finally:
        _drop_table(table)


@check("真实链路：recent 的 limit 只取最近几轮；未知会话给空序列")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        for i in range(1, 4):
            store.record(SESSION_A, Turn(question=f"q{i}", answer=f"a{i}"), keep=5)
        latest = [(t.question) for t in store.recent(SESSION_A, limit=1)]
        if latest != ["q3"]:
            raise AssertionError(f"limit=1 应只给最近一轮，实际 {latest}")
        two = [t.question for t in store.recent(SESSION_A, limit=2)]
        if two != ["q2", "q3"]:
            raise AssertionError(f"limit=2 应给最近两轮且正序，实际 {two}")
        if store.recent(SESSION_B, limit=5):
            raise AssertionError("不存在的会话应该给空序列")
        return "limit 生效、顺序正确、未知会话为空"
    finally:
        _drop_table(table)


@check("真实链路：record(keep=N) 把该会话裁到最近 N 轮，别的会话不受影响")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        store.record(SESSION_B, Turn(question="b1", answer="b1"), keep=5)
        for i in range(1, 4):
            store.record(SESSION_A, Turn(question=f"q{i}", answer=f"a{i}"), keep=2)
        left = [t.question for t in store.recent(SESSION_A, limit=10)]
        if left != ["q2", "q3"]:
            raise AssertionError(f"keep=2 应只留最近两轮，实际 {left}")
        if _count(table, SESSION_A) != 2:
            raise AssertionError("库里 A 的行数也应只剩 2（裁剪真的删了，不是读的时候过滤）")
        if [t.question for t in store.recent(SESSION_B, limit=10)] != ["b1"]:
            raise AssertionError("裁剪把别的会话也删了")
        return "keep=2 生效，会话之间互不影响"
    finally:
        _drop_table(table)


@check("真实链路：clear 只删该会话，并返回删掉的轮数")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        for i in range(1, 4):
            store.record(SESSION_A, Turn(question=f"q{i}", answer=f"a{i}"), keep=5)
        store.record(SESSION_B, Turn(question="b1", answer="b1"), keep=5)
        removed = store.clear(SESSION_A)
        if removed != 3:
            raise AssertionError(f"clear 应返回 3，实际 {removed}")
        if store.recent(SESSION_A, limit=5):
            raise AssertionError("A 的历史应被清空")
        if [t.question for t in store.recent(SESSION_B, limit=5)] != ["b1"]:
            raise AssertionError("clear 误删了 B 的历史")
        return "clear 返回条数且只影响该会话"
    finally:
        _drop_table(table)


@check("真实链路：参数化查询 —— 引号/分号/反斜杠原样往返（不是拼字符串）")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        nasty_q = "'; DROP TABLE " + table + "; -- \\ \" 引号"
        nasty_a = "1'; DELETE FROM " + table + "; -- \\x00"
        store.record(SESSION_A, Turn(question=nasty_q, answer=nasty_a), keep=5)
        turns = store.recent(SESSION_A, limit=5)
        if len(turns) != 1 or turns[0].question != nasty_q or turns[0].answer != nasty_a:
            raise AssertionError(f"带引号的问答没能原样往返：{turns!r}")
        if _count(table) != 1:
            raise AssertionError("表被那条「恶意」问题搞坏了 —— 说明 SQL 是拼出来的")
        return "危险字符原样往返，表还在"
    finally:
        _drop_table(table)


@check("真实链路：超长 session_id 直接报错（不静默截断 —— 截断会把两个会话并成一个）")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        Turn = _turn_type()
        try:
            store.record("x" * 37, Turn(question="q", answer="a"), keep=5)
        except ValueError as exc:
            if "36" not in str(exc):
                raise AssertionError(f"错误信息要说清上限，实际 {exc!r}")
            return "37 字符 → ValueError 且说明上限"
        raise AssertionError("超过 VARCHAR(36) 的 session_id 应该报错")
    finally:
        _drop_table(table)


@check("真实链路：写失败时事务回滚 —— 旧数据还在，不留半条")
def _() -> str:
    table = _create_table(BROKEN_DDL)
    try:
        store = _store_for(table)
        Turn = _turn_type()
        _seed_row(table, SESSION_A, "old-q", "old-a", extra="x")
        before = _count(table, SESSION_A)
        try:
            store.record(SESSION_A, Turn(question="new-q", answer="new-a"), keep=5)
        except Exception:  # noqa: BLE001 - 期望失败：表里多一个非空列
            pass
        else:
            raise AssertionError("写入本该失败（表里多了个非空列），却成功了")
        after = _count(table, SESSION_A)
        if after != before:
            raise AssertionError(f"失败却动了数据：{before} → {after} 行")
        return f"写入失败后仍是 {after} 行（回滚生效）"
    finally:
        _drop_table(table)


@check("真实链路：连不上时错误点名 host:port（同 M2 的 FAQ 适配器）")
def _() -> str:
    from project_rag.config import get_settings

    settings = get_settings()
    bad = settings.mysql.model_copy(update={"port": 1})
    store = _mysql_store_cls()(bad, _conversation_config_type()())
    Turn = _turn_type()
    try:
        store.record(SESSION_A, Turn(question="q", answer="a"), keep=5)
    except RuntimeError as exc:
        if "127.0.0.1:1" not in str(exc):
            raise AssertionError(f"错误信息没点名连接目标，实际 {exc!r}")
        return "RuntimeError 含 127.0.0.1:1"
    raise AssertionError("连不上却没有报错")


@check("适配器：非法表名在构造时就报错（表名要拼进 SQL，不能让配置随便写）")
def _() -> str:
    from project_rag.config import get_settings

    settings = get_settings()
    for bad in ("conv; DROP TABLE x", "conv-table", "", "conv table"):
        try:
            _mysql_store_cls()(settings.mysql, _conversation_config_type()(table=bad))
        except ValueError:
            continue
        raise AssertionError(f"非法表名 {bad!r} 应该被拒绝")
    return "四种非法表名都被拒绝"


# ── 3. 提示词（全假，不连库） ─────────────────────────────────────────────────
@check("提示词：有历史时进提示词；没有历史时与不带历史逐字一致（混进变量就说不清了）")
def _() -> str:
    from project_rag.application import prompts
    from project_rag.domain import Citation, ConversationTurn

    chunk = _kb_hit().chunk
    citations = (Citation(index=1, chunk=chunk, score=0.9),)
    plain = prompts.rag_answer_prompt(citations, "装饰器是什么")
    empty = prompts.rag_answer_prompt(citations, "装饰器是什么", ())
    if plain != empty:
        raise AssertionError("空历史时提示词变了 —— 那会让「带不带历史」的对比多一个变量")

    history = (
        ConversationTurn(question="什么是闭包", answer="闭包是带环境的函数"),
        ConversationTurn(question="那装饰器呢", answer="装饰器是给函数加行为的语法"),
    )
    with_history = prompts.rag_answer_prompt(citations, "它有什么用", history)
    for piece in ("什么是闭包", "闭包是带环境的函数", "那装饰器呢", "2 轮"):
        if piece not in with_history:
            raise AssertionError(f"提示词里没有 {piece!r}：{with_history[:200]!r}")
    if "它有什么用" not in with_history:
        raise AssertionError("提示词里没有当前问题")
    if "Q: 那装饰器呢" not in with_history:
        raise AssertionError("历史没渲染成 Q/A 两行")
    return "空历史 → 逐字一致；有历史 → Q/A 两行都在"


@check("answer_rag：历史只进提示词，引用编号不受影响；检索为空仍拒答（历史救不活它）")
def _() -> str:
    from project_rag.application import answer_rag
    from project_rag.domain import ConversationTurn

    history = (ConversationTurn(question="q0", answer="a0"),)
    world = _World()
    result = answer_rag(
        "装饰器有什么用",
        embedder=world.embedder,
        store=world.vector_store,
        llm=world.llm,
        history=history,
    )
    if not world.llm.calls:
        raise AssertionError("有引用却没调模型")
    prompt = world.llm.prompts[0]
    if "q0" not in prompt or "a0" not in prompt:
        raise AssertionError(f"历史没进提示词：{prompt[:200]!r}")
    if "[1]" not in prompt:
        raise AssertionError("引用编号不见了 —— 历史不该挤掉上下文")
    if [c.index for c in result.citations] != [1]:
        raise AssertionError(f"引用编号被改写：{[c.index for c in result.citations]}")

    empty = _World(hits=[])
    refused = answer_rag(
        "装饰器有什么用",
        embedder=empty.embedder,
        store=empty.vector_store,
        llm=empty.llm,
        history=history,
    )
    if not refused.refused or empty.llm.calls:
        raise AssertionError("检索为空时必须拒答且不调模型 —— 有历史也不行")
    return "历史进提示词、引用不变；空检索仍拒答"


# ── 4. 用例：什么时候读、什么时候写（全假） ──────────────────────────────────
@check("无会话：一个存储调用都没有，结果与 M5.1 完全一致")
def _() -> str:
    world = _World(0.2)
    result = world.ask()
    if result.channel != "rag" or result.answer != KB_ANSWER:
        raise AssertionError(f"无会话时行为应完全不变，实际 {result!r}")
    if world.conversation.records or world.conversation.reads or world.conversation.clears:
        raise AssertionError("没给会话却动了会话存储")
    if world.knowledge_history != ():
        raise AssertionError("无会话时交给知识库的历史应为空")
    return "零存储调用，行为不变"


@check("FAQ 命中 + 有会话：不读历史，但这一轮要记（知识库通道零调用）")
def _() -> str:
    world = _World(0.97)
    world.ask(session=world.session())
    if world.conversation.reads:
        raise AssertionError("FAQ 命中不该读历史 —— 标准答案不需要上文，读了是白跑一次库")
    if len(world.conversation.records) != 1:
        raise AssertionError(f"应该记 1 轮，实际 {len(world.conversation.records)}")
    session_id, turn, keep = world.conversation.records[0]
    if (session_id, turn.question, turn.answer) != (SESSION_A, FAQ_QUESTION, FAQ_ANSWER):
        raise AssertionError(f"记下来的内容不对：{(session_id, turn.question, turn.answer)!r}")
    if keep != DEFAULT_ROUNDS:
        raise AssertionError(f"keep 应为 {DEFAULT_ROUNDS}，实际 {keep}")
    if world.knowledge_calls:
        raise AssertionError("FAQ 命中却调了知识库通道")
    return "不读、记一轮（标准答案）"


@check("FAQ 未命中 + 有会话：读最近 rounds 轮 → 原样交给 knowledge → 记一轮")
def _() -> str:
    Turn = _turn_type()
    world = _World(0.2, turns={SESSION_A: [Turn(question="q1", answer="a1")]})
    world.ask(session=world.session())
    if world.conversation.reads != [(SESSION_A, DEFAULT_ROUNDS)]:
        raise AssertionError(f"读历史的参数不对：{world.conversation.reads}")
    if [t.question for t in world.knowledge_history] != ["q1"]:
        raise AssertionError(f"历史没交给知识库通道：{world.knowledge_history!r}")
    if len(world.conversation.records) != 1:
        raise AssertionError("知识库回答后也该记一轮")
    _, turn, _ = world.conversation.records[0]
    if turn.question != FAQ_QUESTION or turn.answer != KB_ANSWER:
        raise AssertionError(f"记下来的内容不对：{turn!r}")
    return "读一次、历史原样透传、记一轮"


@check("知识库拒答 + 有会话：同样记一轮（拒答也是一轮，否则下一轮的「上一句」是错的）")
def _() -> str:
    from project_rag.domain import RagAnswer

    world = _World(0.1, rag=RagAnswer(answer="信息不足,无法回答,请联系人工客服。", refused=True))
    result = world.ask(session=world.session())
    if not result.refused:
        raise AssertionError("应当拒答")
    if len(world.conversation.records) != 1:
        raise AssertionError("拒答也该记一轮")
    _, turn, _ = world.conversation.records[0]
    if turn.answer != result.answer:
        raise AssertionError("记下来的应该是用户看到的那句话")
    return "拒答也进历史，内容与用户看到的一致"


@check("轮数来自 Session.rounds，不是写死的 5（读和写用的是同一个值）")
def _() -> str:
    world = _World(0.2)
    world.ask(session=world.session(rounds=3))
    if world.conversation.reads != [(SESSION_A, 3)]:
        raise AssertionError(f"读历史应为 limit=3，实际 {world.conversation.reads}")
    if world.conversation.records[0][2] != 3:
        raise AssertionError(f"keep 应为 3，实际 {world.conversation.records[0][2]}")
    return "rounds=3 → recent(limit=3) + record(keep=3)"


@check("存储故障：record 抛错 → 原样上抛（历史不是可选装饰，写不进去要当场可见）")
def _() -> str:
    world = _World(0.97, record_error=RuntimeError("会话表写不进去"))
    try:
        world.ask(session=world.session())
    except RuntimeError as exc:
        if "会话表" not in str(exc):
            raise AssertionError(f"异常应原样上抛，实际 {exc!r}")
        return "异常原样上抛"
    raise AssertionError("record 抛错被吞了 —— 用户会以为历史记下来了")


@check("存储故障：recent 抛错 → 原样上抛，且不调知识库、不写历史")
def _() -> str:
    world = _World(0.2, read_error=RuntimeError("会话表读不出来"))
    try:
        world.ask(session=world.session())
    except RuntimeError:
        if world.knowledge_calls or world.conversation.records:
            raise AssertionError("读历史失败却继续跑了（知识库被调或写了一条）")
        return "异常原样上抛，未继续"
    raise AssertionError("recent 抛错被吞了")


# ── 5. 命令行 ─────────────────────────────────────────────────────────────────
@check("CLI：qa --help 里有 --session；不带 --session 时完全不碰会话存储")
def _() -> str:
    helped = subprocess.run(
        [sys.executable, "-m", "project_rag", "qa", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if helped.returncode != 0 or "--session" not in helped.stdout:
        raise AssertionError(f"qa --help 里没有 --session：{helped.stdout[-200:]!r}")

    world = _World(0.97)
    code, out, err = _run_injected(["qa", "-q", FAQ_QUESTION], _deps(world))
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    if world.conversation.records or world.conversation.reads:
        raise AssertionError("没带 --session 却动了会话存储")
    return "--session 在；不带就不碰存储"


@check("CLI：--session 给了 → 这一轮记进库，且历史确实进了提示词")
def _() -> str:
    Turn = _turn_type()
    world = _World(0.2, turns={SESSION_A: [Turn(question="上一轮问", answer="上一轮答")]})
    code, out, err = _run_injected(
        ["qa", "-q", "装饰器有什么用", "--session", SESSION_A], _deps(world)
    )
    if code != 0:
        raise AssertionError(f"退出码 {code}；stderr={err[:200]!r}")
    # 命令层走的是真的 answer_rag，所以证据要看「读没读」和「提示词里有没有」，
    # 不是 world.knowledge（那是用例层那套假通道）
    if world.conversation.reads != [(SESSION_A, DEFAULT_ROUNDS)]:
        raise AssertionError(f"没按会话 ID 读历史：{world.conversation.reads}")
    if "上一轮答" not in world.llm.prompts[-1]:
        raise AssertionError(f"历史没进提示词：{world.llm.prompts[-1][:200]!r}")
    if len(world.conversation.records) != 1:
        raise AssertionError("这一轮没被记下来")
    return "历史进提示词 + 这一轮进库"


@check("CLI：带了 --session 却没装会话存储 → 退出码 1 并点名（不许静默丢历史）")
def _() -> str:
    world = _World(0.97)
    code, out, err = _run_injected(
        ["qa", "-q", FAQ_QUESTION, "--session", SESSION_A],
        _deps(world, conversation_store_factory=None, reranker_factory=None),
    )
    if code != 1:
        raise AssertionError(f"应退出码 1，实际 {code}；stdout={out!r}")
    if "conversation_store_factory" not in err:
        raise AssertionError(f"错误信息要点名缺哪个工厂，实际 stderr={err!r}")
    return "缺工厂 → 1 且点名"


@check("真实链路：真 MySQL 表 + 两轮 --session → 第二轮的提示词里能看到第一轮")
def _() -> str:
    table = _create_table()
    try:
        store = _store_for(table)
        world = _World(0.2)
        deps = _deps(world, conversation_store_factory=lambda: store)

        first_code, _, first_err = _run_injected(
            ["qa", "-q", "第一个问题", "--session", SESSION_A], deps
        )
        if first_code != 0:
            raise AssertionError(f"第一轮退出码 {first_code}；stderr={first_err[:200]!r}")
        second_code, _, second_err = _run_injected(
            ["qa", "-q", "第二个问题", "--session", SESSION_A], deps
        )
        if second_code != 0:
            raise AssertionError(f"第二轮退出码 {second_code}；stderr={second_err[:200]!r}")

        turns = store.recent(SESSION_A, limit=5)
        if [t.question for t in turns] != ["第一个问题", "第二个问题"]:
            raise AssertionError(f"库里应有两轮且正序，实际 {[t.question for t in turns]}")
        if "第一个问题" not in world.llm.prompts[-1]:
            raise AssertionError(
                f"第二轮的提示词里没有第一轮：{world.llm.prompts[-1][:200]!r}"
            )
        return f"表 {table}：两轮落库、第二轮带着第一轮的历史"
    finally:
        _drop_table(table)


def main() -> int:
    return report("M5.2 对话历史验收")


if __name__ == "__main__":
    sys.exit(main())
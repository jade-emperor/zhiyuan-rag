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
from datetime import UTC, datetime, timedelta
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

    def knowledge(self, history=(), timing=None):
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

    def session(
        self,
        session_id: str = SESSION_A,
        rounds: int = DEFAULT_ROUNDS,
        max_chars: int = 0,
    ):
        return _session_type()(
            store=self.conversation,
            session_id=session_id,
            rounds=rounds,
            max_chars=max_chars,
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
@check("domain：ConversationTurn 是 frozen+slots dataclass，字段 question/answer/citations，包入口可导入")
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
    if names != ["question", "answer", "citations"]:
        raise AssertionError(f"字段应为 ['question','answer','citations']，实际 {names}")
    # citations 必须有默认值:老调用点(以及"这一轮没有出处")都不用显式给
    make: Any = Turn  # 动态取来的类(pyright 只看到 Any),这一条本来就只关心字段与默认值
    built = make(question="q", answer="a")
    if getattr(built, "citations", None) != ():
        raise AssertionError("citations 的默认值该是空元组")
    domain = importlib.import_module("project_rag.domain")
    exported = getattr(domain, "__all__", [])
    for name in ("ConversationTurn", "CitationSnapshot", "snapshot_citations", "PREVIEW_CHARS"):
        if name not in exported:
            raise AssertionError(f"domain/__init__.py 的 __all__ 里缺 {name}")
    return "字段 question/answer/citations(默认空),快照类型与拍快照的函数都在包入口"


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


@check("配置：conversation 段默认值 + 字符预算默认 0（= 现状）+ 两个旋钮都能被环境变量覆盖")
def _() -> str:
    ConversationConfig = _conversation_config_type()
    defaults = ConversationConfig()
    if defaults.table != "conversations":
        raise AssertionError(f"默认表名应为 conversations，实际 {defaults.table!r}")
    if defaults.history_rounds != DEFAULT_ROUNDS:
        raise AssertionError(f"默认保留轮数应为 {DEFAULT_ROUNDS}，实际 {defaults.history_rounds!r}")
    if defaults.history_max_chars != 0:
        raise AssertionError(
            "字符预算的**代码默认值**应为 0（不限）—— 默认值一律取「这段代码一直以来的行为」，"
            "要收紧就在 config.yaml 里写（§5 法律 57③）"
        )
    try:
        ConversationConfig(history_max_chars=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("负的字符预算该被拒 —— 它会让历史永远为空(静默失忆)")

    from project_rag.config import get_settings

    with env(RAG_CONVERSATION__HISTORY_ROUNDS="3", RAG_CONVERSATION__HISTORY_MAX_CHARS="800"):
        get_settings.cache_clear()
        try:
            conversation = get_settings().conversation
            if conversation.history_rounds != 3 or conversation.history_max_chars != 800:
                raise AssertionError(f"环境变量没能覆盖:{conversation}")
        finally:
            get_settings.cache_clear()
    return "默认 conversations / 5 / 不限;负数被拒;两个旋钮 env 都能覆盖"


@check("配置：config.yaml 里确实写了 conversation 段（代码里有默认值 ≠ 配置文件里有）")
def _() -> str:
    from project_rag.config import CONFIG_FILE, get_settings

    text = CONFIG_FILE.read_text(encoding="utf-8")
    missing = [
        key
        for key in ("conversation:", "table:", "history_rounds:", "history_max_chars:")
        if key not in text
    ]
    if missing:
        raise AssertionError(f"config/config.yaml 里缺 {missing}")
    settings = get_settings()
    if settings.conversation.table != "conversations":
        raise AssertionError(f"生效的表名是 {settings.conversation.table!r}")
    return (
        f"yaml 与生效值一致（{settings.conversation.table} / "
        f"{settings.conversation.history_rounds} 轮 / "
        f"{settings.conversation.history_max_chars} 字符）"
    )


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
    if names != ["store", "session_id", "rounds", "max_chars"]:
        raise AssertionError(
            f"字段应为 ['store','session_id','rounds','max_chars']，实际 {names}"
        )
    if dataclasses.fields(_session_type())[2].default != DEFAULT_ROUNDS:
        raise AssertionError("rounds 默认值应为 5（与配置默认一致）")
    if dataclasses.fields(_session_type())[3].default != 0:
        raise AssertionError("max_chars 默认值应为 0 = 不限（默认值取「代码一直以来的行为」）")

    store = FakeConversationStore()
    for kwargs, why in (
        ({"session_id": "  "}, "空白 session_id"),
        ({"session_id": SESSION_A, "rounds": 0}, "rounds=0"),
        ({"session_id": SESSION_A, "max_chars": -1}, "max_chars=-1"),
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


@check("滑动窗口：从最新往回装、装不下整轮丢、0 = 不限、最新一轮必留")
def _() -> str:
    """光按轮数限不住大小:一轮可以很长(用户贴文档来问、或模型答满 4096 token)。

    5 轮 × 几千字 = 几十万字符的提示词,而那是**每一轮都要重发的输入 token**。
    所以窗口要能按大小滑:从最新往回装,装不下的**整轮**丢掉(不截断半轮 ——
    半个答案进提示词,模型会把断句当结论)。
    """
    from project_rag.application.answer_question import window_history

    Turn = _require("project_rag.domain", ("ConversationTurn",)).ConversationTurn
    # 每轮 4 字符(问 2 + 答 2),5 轮共 20 字符 —— 预算的算式要能手算复核
    turns = tuple(Turn(question=f"问{i}", answer=f"答{i}") for i in range(1, 6))
    size = 4

    if window_history(turns, max_chars=0) != turns:
        raise AssertionError("0 = 不限,要原样返回")
    if window_history(turns, max_chars=1000) != turns:
        raise AssertionError("预算够就该全带上")
    if [turn.question for turn in window_history(turns, max_chars=3 * size)] != [
        "问3",
        "问4",
        "问5",
    ]:
        raise AssertionError("正好装下 3 轮时该留下(判据是「超过才丢」,不是「达到就丢」)")
    if [turn.question for turn in window_history(turns, max_chars=3 * size - 1)] != [
        "问4",
        "问5",
    ]:
        raise AssertionError("少 1 个字符就该丢掉最旧的那一轮(整轮丢,不是截断)")
    if [turn.question for turn in window_history(turns, max_chars=size)] != ["问5"]:
        raise AssertionError("只装得下 1 轮时给最新那一轮")
    if [turn.question for turn in window_history(turns, max_chars=size - 1)] != ["问5"]:
        raise AssertionError("最新一轮自己就超预算时也要留着 —— 窗口空了等于静默失忆")
    if window_history(turns, max_chars=size * 5) != turns:
        raise AssertionError("正好装下全部 5 轮时不该丢任何一轮")
    if window_history((), max_chars=10) != ():
        raise AssertionError("空历史给空")
    return "最新往回装 / 整轮丢 / 边界 3×4 留 3 轮、11 留 2 轮 / 最新一轮必留"


@check("滑动窗口接线：Session.max_chars 裁的是**进提示词的那一份**，库里仍是 5 轮")
def _() -> str:
    from project_rag.domain import ConversationTurn

    long_answer = "长" * 100
    turns = {
        SESSION_A: [
            ConversationTurn(question=f"问{i}", answer=long_answer) for i in range(1, 7)
        ]
    }
    world = _World(0.2, turns=turns)
    # 预算只够最新的 2 轮(每轮 101 字符)
    world.ask(session=world.session(rounds=5, max_chars=250))
    got = world.knowledge_history
    if [turn.question for turn in got] != ["问5", "问6"]:
        raise AssertionError(f"该只带最新 2 轮进提示词,实际 {[t.question for t in got]}")
    if world.conversation.reads != [(SESSION_A, 5)]:
        raise AssertionError(
            f"存储那一段该按**轮数**取(库里留几轮是存储的口径),实际 {world.conversation.reads}"
        )
    if world.conversation.records[0][2] != 5:
        raise AssertionError("写回时的 keep 该仍是轮数(预算不影响库里留几轮)")
    # 不限大小(0)时,拿到的就是存储给的那 5 轮
    world2 = _World(0.2, turns=turns)
    world2.ask(session=world2.session(rounds=5, max_chars=0))
    if [turn.question for turn in world2.knowledge_history] != ["问2", "问3", "问4", "问5", "问6"]:
        raise AssertionError(
            f"0 = 不限,该原样带 5 轮,实际 {[t.question for t in world2.knowledge_history]}"
        )
    return "预算 250 → 只带最新 2 轮进提示词;存储仍按 5 轮读、写回也是 5;0 = 不限"


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


@check("真实链路：引用快照随这一轮落库、切回旧会话读得回来（老表自动补列）")
def _citations_roundtrip() -> str:
    """作者报的那条:**答案里的 [1][2] 还在,引用卡片没了** —— 因为它从来没被存过。

    这条盯三件事:① 引用随 ``record`` 落库、``recent`` 读回来(序号/来源/页码/分数/预览);
    ② 预览按 ``PREVIEW_CHARS`` 截断 —— 实时那份与历史那份必须是同一个数,否则
    "刚问完"与"切回来"长得不一样;③ **老表**(没有 ``citations`` 列)由 ``ensure_table``
    就地补列,老行读成空元组(不是报错,也不是编一份引用出来)。
    """
    import uuid as _uuid

    from project_rag.domain import (
        PREVIEW_CHARS,
        Chunk,
        Citation,
        ConversationTurn,
        snapshot_citations,
    )

    table = f"conv_cit_{_uuid.uuid4().hex[:8]}"
    old_table = f"conv_old_{_uuid.uuid4().hex[:8]}"
    store = _store_for(table)
    try:
        store.ensure_table()
        long_text = "父块正文" * 100
        cites = (
            Citation(
                index=1,
                chunk=Chunk("c1", "子块", "bagu_ai", "d1", "p1", long_text, page=7),
                score=0.9,
                rerank_score=0.42,
            ),
            Citation(
                index=2,
                chunk=Chunk("c2", "子块", "bagu_java", "d2", "p2", "短正文"),
                score=0.5,
            ),
        )
        store.record(
            SESSION_A,
            ConversationTurn(
                question="大模型微调有哪些策略?",
                answer="主要分两类 [1][2]。",
                citations=snapshot_citations(cites),
            ),
            keep=5,
        )
        turns = store.recent(SESSION_A, limit=5)
        if len(turns) != 1:
            raise AssertionError(f"该读回 1 轮,实际 {len(turns)}")
        got = turns[0].citations
        if [c.index for c in got] != [1, 2]:
            raise AssertionError(f"引用条数与序号不对:{got}")
        first = got[0]
        if (first.source, first.doc_id, first.page, first.score, first.rerank_score) != (
            "bagu_ai",
            "d1",
            7,
            0.9,
            0.42,
        ):
            raise AssertionError(f"引用字段不对:{first}")
        if len(first.preview) != PREVIEW_CHARS or len(got[1].preview) != len("短正文"):
            raise AssertionError(
                f"预览该截到 {PREVIEW_CHARS} 字(短的原样),实际 {len(first.preview)}/{len(got[1].preview)}"
            )
        if got[1].page is not None:
            raise AssertionError(f"没有页码的引用读回来该是 None(不许编 0/1),实际 {got[1].page}")

        # ② 老表:没有 citations 列的那种 —— ensure_table 幂等补列,老行读成空元组
        conn = _mysql_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE {old_table} ("
                    "  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                    "  session_id VARCHAR(36) NOT NULL,"
                    "  question TEXT NOT NULL,"
                    "  answer TEXT NOT NULL,"
                    "  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                cur.execute(
                    f"INSERT INTO {old_table} (session_id, question, answer)"
                    " VALUES (%s, '老问题', '老答案')",
                    (SESSION_A,),
                )
        finally:
            conn.close()
        old_store = _store_for(old_table)
        old_store.ensure_table()  # 补列(幂等)
        old_store.ensure_table()
        old_turns = old_store.recent(SESSION_A, limit=5)
        if len(old_turns) != 1 or old_turns[0].citations != ():
            raise AssertionError(f"老行该读成『没有引用』,实际 {old_turns}")
        return f"表 {table}:2 条引用(预览截到 {PREVIEW_CHARS} 字、无页码保持 None);老表补列后老行 = 无引用"
    finally:
        _drop_table(table)
        _drop_table(old_table)


@check("记历史时带上引用：快照来自这一轮的证据链，字符预算仍只数问答")
def _record_keeps_citations_and_budget_ignores_them() -> str:
    """两条容易踩的:

    ① ``_record`` 拿到的 ``done.citations`` 必须变成 ``turn.citations`` —— 少了这一步,
       落库的还是空的(而"切回旧会话看不到引用"就是这么来的);
    ② 引用**不进**字符预算:``window_history`` 数的是问题 + 答案,
       把 120 字的预览也算进去会让窗口莫名其妙地少装一轮。
    """
    from project_rag.application.answer_question import Session, stream_question, window_history
    from project_rag.domain import (
        PREVIEW_CHARS,
        Chunk,
        Citation,
        ConversationTurn,
        snapshot_citations,
    )

    store = FakeConversationStore()
    hit = Citation(
        index=1,
        chunk=Chunk("c1", "子块", "bagu_ai", "d1", "p1", "父块正文" * 50, page=3),
        score=0.9,
    )
    QaEvent = _require("project_rag.domain", ("QaEvent",)).QaEvent
    answer = "装饰器是给函数加行为的语法 [1]"
    events = list(
        stream_question(
            "装饰器有什么用",
            faq_store=FakeFaqStore([]),
            matcher=FakeMatcher(),
            knowledge=lambda history, timing: iter(
                [
                    QaEvent(kind="sources", citations=(hit,)),
                    QaEvent(kind="token", text=answer),
                    QaEvent(kind="done", answer=answer, citations=(hit,)),
                ]
            ),
            session=Session(store=store, session_id="s1"),
        )
    )
    if events[-1].kind != "done":
        raise AssertionError(f"帧协议:该以 done 收尾,实际 {events[-1]!r}")
    if not store.records:
        raise AssertionError("这一轮没记进历史")
    _sid, turn, _keep = store.records[0]
    if [c.index for c in turn.citations] != [1] or turn.citations[0].source != "bagu_ai":
        raise AssertionError(f"记历史时没带上引用快照:{turn}")
    if len(turn.citations[0].preview) != PREVIEW_CHARS:
        raise AssertionError(f"存进历史的预览该截到 {PREVIEW_CHARS} 字:{turn.citations[0]}")

    # 字符预算只数问答:同一段历史,带不带引用,窗口装下的轮数必须一样
    turns = tuple(
        ConversationTurn(
            question=f"问{i}",
            answer="答案" * 20,
            citations=snapshot_citations(
                (
                    Citation(
                        index=1,
                        chunk=Chunk(f"c{i}", "子块", "s", "d", "p", "长父块" * 100),
                        score=0.9,
                    ),
                )
            ),
        )
        for i in range(1, 4)
    )
    size = len(turns[0].question) + len(turns[0].answer)
    with_citations = window_history(turns, max_chars=size * 2)
    without = window_history(
        tuple(ConversationTurn(question=t.question, answer=t.answer) for t in turns),
        max_chars=size * 2,
    )
    if [t.question for t in with_citations] != [t.question for t in without]:
        raise AssertionError(
            f"引用不该影响字符预算:带引用装了 {[t.question for t in with_citations]},"
            f"不带装了 {[t.question for t in without]}"
        )
    return "record 带上引用快照(预览截断)；窗口预算只数问答(带不带引用装下的一样多)"


@check("真实链路：账本带 token(含缓存命中)的行来回一趟 MySQL —— 消耗报表按三档×两时段算得出钱")
def _() -> str:
    """P0-7:账本是"整行 JSON + 几列索引",token 那四个字段**只进 payload**;
    而**时段**靠存储层盖的 ``ts`` + ``ts_offset``(读回来时会附在行里)—— 这条把三件事一起验了:
    写进去能原样读回来、读回来的时间戳**是个绝对时刻**(带时区)、报表据此判得出时段。

    单价用注入的(不动配置文件):**命中 0.04 / 未命中 2 / 输出 8 元每百万**(高峰),
    时段规则也是注入的(北京时间工作日 09–12/14–18 高峰)—— 于是这条断言与"现在是几点"无关。
    """
    import uuid as _uuid

    from project_rag.adapters.ledger_mysql import MySQLLedgerStore
    from project_rag.application.cost import PeriodRule, summarize_cost
    from project_rag.config import get_settings
    from project_rag.ports.ledger import LEDGER_TS

    table = f"ledger_p0_7_{_uuid.uuid4().hex[:8]}"
    store = MySQLLedgerStore(get_settings().mysql, table)
    store.init_schema()

    class _Tier:
        def __init__(self, peak, off_peak=None):
            self.peak, self.off_peak = peak, off_peak

    class _Price:
        def __init__(self):
            self.input_cache_hit = _Tier(0.04)
            self.input_cache_miss = _Tier(2.0)
            self.output = _Tier(8.0)

    try:
        store.record(
            {
                "label": "rag",
                "request_id": "p0-7-probe",
                "channel": "rag",
                "llm_ttft_ms": "900.0",
                "llm_model": "DeepSeek-V4.1-Flash",
                "llm_input_tokens": "1000000",
                "llm_cached_input_tokens": "800000",
                "llm_output_tokens": "100000",
            }
        )
        rows = store.load_since(datetime.now() - timedelta(minutes=5))
        mine = [row for row in rows if row.get("request_id") == "p0-7-probe"]
        if len(mine) != 1:
            raise AssertionError(f"该读回 1 行,实际 {len(mine)} 行:{rows[:3]}")
        row = mine[0]
        if (
            row.get("llm_model"),
            row.get("llm_input_tokens"),
            row.get("llm_cached_input_tokens"),
        ) != ("DeepSeek-V4.1-Flash", "1000000", "800000"):
            raise AssertionError(f"token 那几列没原样回来:{row}")
        stamp = row.get(LEDGER_TS)
        if not stamp:
            raise AssertionError(
                f"读回来的行必须带存储层盖的时间戳({LEDGER_TS})—— 分时段计价全靠它:{row}"
            )
        moment = datetime.fromisoformat(stamp)  # 解析不了会抛,由 @check 记成 FAIL
        if moment.tzinfo is None:
            raise AssertionError(
                f"时间戳必须带时区(ts_offset 那列没读出来?):{stamp!r} —— "
                "裸钟点判不出高峰/空闲,报表只能按高峰价算"
            )
        # 刚写的这一行,时刻该与现在一致(±5 分钟):既验了 ts 是 UTC 存的,也验了时区换算没错
        now = datetime.now(UTC)
        if abs((now - moment).total_seconds()) > 300:
            raise AssertionError(f"ts 该是刚刚(±5 分钟),实际 {stamp}(现在 {now.isoformat()})")

        cost = get_settings().cost
        rule = PeriodRule.of(
            weekdays=cost.peak.weekdays, windows=cost.peak.windows, holidays=cost.holidays
        )
        report_ = summarize_cost(
            mine,
            prices={"DeepSeek-V4.1-Flash": _Price()},
            currency="CNY",
            window="probe",
            rule=rule,
        )
        # 0.8M 命中×0.04 + 0.2M 未命中×2 + 0.1M 输出×8 = 0.032 + 0.4 + 0.8 = 1.232(高峰)
        # 同一笔落在空闲时段就是 0.616 —— 到底哪个,取决于**跑这条检查时的北京时间**,
        # 所以两种都接受,但必须**判得有依据**(不能是"时区不明按高峰算"蒙出来的)。
        if report_.total_cost not in (1.232, 0.616):
            raise AssertionError(
                f"真库读回来的行该算出 1.232(高峰)或 0.616(空闲)元,实际 {report_.total_cost}"
            )
        if report_.metered_calls != 1 or report_.cache_unknown_calls:
            raise AssertionError(f"该 1 次调用、缓存明细齐全:{report_}")
        if report_.zone_unknown_calls:
            raise AssertionError(f"真库盖的戳该带时区,不该报『时区不明』:{report_.zone_unknown_calls}")
        # 日历那一档**分两种情况都要如实**:配置覆盖了这一年 → 不该有缺口;
        # 没覆盖(新一年的放假通知还没发布)→ 必须报出来,而不是假装那天不放假。
        from project_rag.application.cost import BEIJING

        year = now.astimezone(BEIJING).year
        if year in rule.covered_years:
            if report_.calendar_unknown_calls:
                raise AssertionError(f"{year} 年已在节假日表里,不该报『日历不明』:{report_}")
        elif report_.calendar_unknown_calls != 1:
            raise AssertionError(
                f"{year} 年不在节假日表里,该如实报『日历不明』并按高峰计:{report_}"
            )
        period = report_.lines[0].period
        # 判出来的时段必须与"这一笔实际发生的北京时间"一致(自己按同一条规则手算一遍)
        want = rule.classify(moment)[0]
        if period != want:
            raise AssertionError(f"{moment.isoformat()} 该判 {want},实际 {period}")
        return (
            f"表 {table}:四个 token 字段 + 带时区的 ts 往返;{period} 时段算出 "
            f"{report_.total_cost} 元"
        )
    finally:
        _drop_table(table)


@check("口径：所有 CREATE TABLE 的尾巴都来自 mysql_schema.TABLE_OPTIONS（少写 COLLATE = 1267）")
def _ddl_options_are_shared() -> str:
    """**这一条盯的是"两族表"**：``DEFAULT CHARSET=utf8mb4`` **不带** ``COLLATE`` 时,
    MySQL 8 用的是**字符集自己的**默认排序规则(``utf8mb4_0900_ai_ci``),而不是库/服务端的默认
    (本部署钉的是 ``utf8mb4_unicode_ci``)。于是同一个库里长出两族表,一旦按字符串列 JOIN
    就是 ``1267 Illegal mix of collations`` —— 作者那次的表现是**会话列表整页读不出来**,
    而前端只拿到一句 "Internal Server Error"。

    规则:凡是建表的地方,尾巴都写 ``{options}``(值来自 ``adapters/mysql_schema.py`` 的唯一常量)。
    这里做的是**静态**检查(不连库):出现 ``CREATE TABLE`` 的文件里不许再出现 ``DEFAULT CHARSET``,
    且占位符个数要对得上 —— 少一个就是漏了一处。
    """
    from project_rag.adapters.mysql_schema import CHARSET, COLLATION, TABLE_OPTIONS

    if f"DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}" not in TABLE_OPTIONS:
        raise AssertionError(
            f"TABLE_OPTIONS 必须同时写死 CHARSET 与 COLLATE(只写 CHARSET 就会掉进 1267):{TABLE_OPTIONS!r}"
        )
    owners = sorted(
        [*SRC.rglob("*.py"), ROOT / "scripts" / "tools" / "load_faq_csv.py"]
    )
    checked = 0
    for path in owners:
        text = path.read_text(encoding="utf-8")
        # 只数**真的建表语句**(``CREATE TABLE IF NOT EXISTS {表名}``):注释里提到这几个字不算
        tables = text.count("CREATE TABLE IF NOT EXISTS {")
        if not tables:
            continue
        checked += tables
        if "DEFAULT CHARSET" in text:
            raise AssertionError(
                f"{path.relative_to(ROOT)} 里有裸的 DEFAULT CHARSET —— 尾巴必须写 {{options}}"
                f"(值来自 adapters/mysql_schema.py)"
            )
        if text.count("{options}") != tables:
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{tables} 处 CREATE TABLE,{text.count('{options}')} 处 {{options}}"
                " —— 有不走共享口径的建表"
            )
    return f"{checked} 处 CREATE TABLE 全部走 {TABLE_OPTIONS.split('DEFAULT ')[1]}"


@check("真实链路：老表(只写了 CHARSET)被 init_schema 扳成同一族，会话目录能 JOIN 出来")
def _collation_heals_legacy_tables() -> str:
    """**这一条复现作者报的那次故障**:``session_owner``(老 DDL,只写 CHARSET ⇒ 0900)
    JOIN ``conversations``(写了 COLLATE ⇒ unicode)报 1267,前端整页会话读不出来。

    三步:① 按老 DDL 建探针表 → 断言 JOIN **确实会炸**(前置条件;不炸说明这台 MySQL 的默认
    变了,这条检查就失去了意义,必须报出来而不是假装通过);② 跑一次适配器的 ``init_schema``;
    ③ 同一句 JOIN 必须给出会话。
    """
    import uuid as _uuid

    from project_rag.adapters import MySQLConversationStore
    from project_rag.adapters.auth_mysql import MySQLAuthStore
    from project_rag.adapters.sessions_mysql import MySQLSessionCatalog
    from project_rag.config import ConversationConfig, get_settings

    settings = get_settings()
    tag = _uuid.uuid4().hex[:8]
    owners, users, tokens = f"m5_owner_{tag}", f"m5_users_{tag}", f"m5_tokens_{tag}"
    table = f"conv_m5j_{tag}"
    store = MySQLConversationStore(settings.mysql, ConversationConfig(table=table))
    conn = _mysql_connect()
    try:
        with conn.cursor() as cur:
            # 老样子:只写 CHARSET(这正是 session_owner 当初的 DDL)
            cur.execute(
                f"CREATE TABLE {owners} ("
                "  session_id VARCHAR(64) PRIMARY KEY,"
                "  user_id VARCHAR(32) NOT NULL,"
                "  created_at DATETIME(3) NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
    finally:
        conn.close()
    try:
        store.ensure_table()  # 新口径建的会话表(unicode)
        Turn = _turn_type()
        store.record(
            "s-collation",
            Turn(question="会话列表为什么读不出来", answer="因为两张表的排序规则不是一族"),
            keep=5,
        )
        conn = _mysql_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {owners} (session_id, user_id, created_at)"
                    " VALUES ('s-collation', 'u-probe', NOW(3))"
                )
        finally:
            conn.close()

        catalog = MySQLSessionCatalog(
            settings.mysql, ConversationConfig(table=table), owners_table=owners
        )
        try:
            catalog.list_for("u-probe", limit=5)
        except Exception as exc:  # noqa: BLE001 - 预期:两族表 JOIN 报 1267
            if "1267" not in str(exc) and "Illegal mix of collations" not in str(exc):
                raise AssertionError(f"预期 1267(排序规则不一致),实际:{exc}") from exc
        else:
            raise AssertionError(
                "前置条件不成立:两族排序规则的表 JOIN 竟然成功了 —— 这台 MySQL 的默认排序规则"
                "变了,这条检查证明不了任何事(要么改探针,要么承认它没在测那件事)"
            )

        # 修复路径:适配器的 init_schema(启动时跑的就是它)
        MySQLAuthStore(
            settings.mysql, users_table=users, tokens_table=tokens, owners_table=owners
        ).init_schema()
        rows = MySQLSessionCatalog(
            settings.mysql, ConversationConfig(table=table), owners_table=owners
        ).list_for("u-probe", limit=5)
        if [row.session_id for row in rows] != ["s-collation"]:
            raise AssertionError(f"扳回来之后该列出那一段会话,实际 {[r.session_id for r in rows]}")
        if rows[0].rounds != 1 or "读不出来" not in rows[0].title:
            raise AssertionError(f"轮数/标题不对:{rows[0]}")
        return "老表 JOIN 先炸 1267,init_schema 之后同一句 JOIN 给出 1 段会话"
    finally:
        for name in (owners, users, tokens, table):
            _drop_table(name)


@check("真实链路：老账本表(没有 ts_offset 列)能被 init_schema 就地补上，老行的时段判不出来")
def _() -> str:
    """**这条盯的是作者那张已有的表**:``CREATE TABLE IF NOT EXISTS`` 不会给老表加列,
    所以 ``init_schema`` 里显式补一次。补不上的话,重建镜像后服务启动即报
    "Unknown column 'ts_offset'",而那是**生产启动路径**。

    老行必须读成**不带时区**的时间戳(那时只存了裸钟点):报表据此按高峰价 + 计数,
    而不是把 UTC 的钟点当成北京时间(8 小时差 = 高峰判成空闲)。
    """
    import uuid as _uuid

    from project_rag.adapters.ledger_mysql import MySQLLedgerStore
    from project_rag.config import get_settings
    from project_rag.ports.ledger import LEDGER_TS

    table = f"ledger_old_{_uuid.uuid4().hex[:8]}"
    conn = _mysql_connect()
    try:
        with conn.cursor() as cur:
            # 老表的样子:没有 ts_offset(与加这一列之前的 DDL 一致)
            cur.execute(
                f"CREATE TABLE {table} ("
                "  id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                "  request_id VARCHAR(64) NOT NULL,"
                "  ts DATETIME(3) NOT NULL,"
                "  label VARCHAR(16) NOT NULL,"
                "  channel VARCHAR(16) NULL,"
                "  cold TINYINT(1) NOT NULL,"
                "  builds INT NOT NULL,"
                "  total_ms DOUBLE NULL,"
                "  ttft_ms DOUBLE NULL,"
                "  refused TINYINT(1) NOT NULL,"
                "  truncated TINYINT(1) NOT NULL,"
                "  aborted TINYINT(1) NOT NULL,"
                "  payload JSON NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cur.execute(
                f"INSERT INTO {table} (ts, request_id, label, cold, builds, "
                "refused, truncated, aborted, payload) VALUES "
                "(NOW(3), 'old-row', 'rag', 0, 0, 0, 0, 0, '{\"request_id\": \"old-row\", "
                "\"llm_model\": \"DeepSeek-V4.1-Flash\", \"llm_input_tokens\": \"1000000\"}')"
            )
    finally:
        conn.close()

    store = MySQLLedgerStore(get_settings().mysql, table)
    try:
        store.init_schema()  # 补列(幂等:再跑一次也不该炸)
        store.init_schema()
        rows = store.load_since(datetime.now(UTC) - timedelta(minutes=5))
        old = [row for row in rows if row.get("request_id") == "old-row"]
        if len(old) != 1:
            raise AssertionError(f"老行该读回来 1 行,实际 {len(old)}")
        if datetime.fromisoformat(old[0][LEDGER_TS]).tzinfo is not None:
            raise AssertionError(
                f"老行的裸钟点不该被猜成某个时区:{old[0][LEDGER_TS]!r} —— "
                "它只说明写它的是某个钟,而那张表里容器(UTC)与宿主(CST)的历史行都有"
            )
        # 补列之后新写的行必须带上时区偏移
        store.record({"label": "rag", "request_id": "new-row", "channel": "rag"})
        fresh = [
            row
            for row in store.load_since(datetime.now(UTC) - timedelta(minutes=5))
            if row.get("request_id") == "new-row"
        ]
        stamp = datetime.fromisoformat(fresh[0][LEDGER_TS])
        if stamp.tzinfo is None:
            raise AssertionError(f"补列之后写的行该带时区:{fresh[0][LEDGER_TS]!r}")
        return f"表 {table}:老表补上 ts_offset;老行裸钟点(判不出来)/ 新行 {stamp.isoformat()}"
    finally:
        _drop_table(table)


def main() -> int:
    return report("M5.2 对话历史验收")


if __name__ == "__main__":
    sys.exit(main())
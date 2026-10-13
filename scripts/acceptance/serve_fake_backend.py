"""给前端手工验证用的**假后端**:真 uvicorn + 假依赖、假账号、假会话目录。

**它不是验收脚本**(不在 `check_*` / `reverse_*` 里),所以回归不会跑它。
它是给「M7.x 的界面改了,想用眼睛看一遍」准备的一台可控后端:

* 真库里的账号密码不该猜、更不该为了让界面跑起来去改真库;
* 真后端会把"看一眼界面"变成"往作者的 MySQL 里写会话与账号";
* 而这里登录 / 角色 / 会话目录 / 流式回答 / 引用页码走的**全是真接口**,只有数据是假的。

    $ RAG_EMBEDDING__DEVICE=cpu .venv/bin/python scripts/acceptance/serve_fake_backend.py
    fake backend on http://127.0.0.1:18081 账号:{'admin': 'admin-pass', 'employee': 'emp-pass'}

浏览器打开 `<http://127.0.0.1:18081/>`(会被 307 到 `/static/app/`)即可。
上传的文件落在 **`.cache/fake_uploads/`**(不是 `data/uploads/` —— 演示不该往真语料目录里写东西)。
**每收到一次提问,它会把后端真正解析出来的旋钮打在 `>>> QUERY` 那一行** ——
「界面上改了参数,后端到底收到什么」不用猜。

配合 `browser_m7_frontend.cjs` 就是那套自动化的浏览器实测(见 `docs/frontend.md` §7)。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from typing import Any
from pathlib import Path

# **上传落盘目录挪到 `.cache/` 下**:假后端不该往作者的 `data/uploads/` 里写文件 ——
# 它只是个演示后端,而"演示"留下的垃圾会混进真语料的目录清单里。
# 必须在 `get_settings()` 之前设(配置是进程级单例,读过就定了)。
os.environ.setdefault("RAG_API__UPLOADS_DIR", ".cache/fake_uploads")

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))          # 为了 import _fakes / _harness
sys.path.insert(0, str(ROOT / "src"))   # 为了 import project_rag

import uvicorn  # noqa: E402

from _fakes import (  # noqa: E402
    FakeConversationStore,
    FakeEmbedder,
    FakeFaqStore,
    FakeKeywordIndex,
    FakeLLM,
    FakeMatcher,
    FakeReranker,
)
from project_rag.api import create_app  # noqa: E402
from project_rag.application.security import hash_password  # noqa: E402
from project_rag.cli import Deps  # noqa: E402
from project_rag.domain import Chunk, ConversationTurn, RetrievedChunk  # noqa: E402
from project_rag.domain.auth import User  # noqa: E402
from project_rag.domain.sessions import SessionSummary  # noqa: E402

# 三个角色各一个账号(P0-6):ops 只能看报表,员工只能问答 —— 浏览器用例靠它们验分流。
ACCOUNTS = {"admin": "admin-pass", "employee": "emp-pass", "ops": "ops-pass"}
_ROLES = {"admin": "admin", "employee": "employee", "ops": "ops"}


class FakeAuthStore:
    def __init__(self) -> None:
        self.users = {
            name: (
                User(user_id=f"u-{name}", username=name, role=_ROLES[name]),  # type: ignore[arg-type]
                hash_password(pw),
            )
            for name, pw in ACCOUNTS.items()
        }
        self.tokens: dict[str, tuple[str, float]] = {}
        self.owners: dict[str, str] = {}

    def init_schema(self) -> None: ...
    def create_user(self, username, password_hash, role): ...
    def set_password(self, user_id, password_hash) -> None: ...
    def delete_user(self, user_id) -> int:  # pragma: no cover - 假后端里没有删号入口
        return 1 if self.users.pop(user_id, None) is not None else 0

    def list_users(self) -> Sequence[User]:
        return [user for user, _ in self.users.values()]

    def find_by_username(self, username):
        return self.users.get(username)

    def issue_token(self, user, token_hash, ttl_seconds) -> None:
        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash):
        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next(u for u, _ in self.users.values() if u.user_id == item[0])

    def revoke_token(self, token_hash) -> None:
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id) -> None:
        self.owners.setdefault(session_id, user_id)

    def owner_of(self, session_id):
        return self.owners.get(session_id)


def _ts(days_ago: int, hour: int, minute: int) -> str:
    """夹具时间戳：**相对当下算**，不写死日期（不把任何人的时间线写进仓库）。

    各自的小时/分钟与先后关系要保住 —— 界面按 `updated_at` 倒序展示，顺序本身是被看的东西。
    """
    from datetime import datetime, timedelta

    moment = datetime.now() - timedelta(days=days_ago)
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


SESSIONS = {
    "u-admin": [
        SessionSummary(
            session_id="11111111-1111-1111-1111-111111111111",
            title="每周加班最多多长时间",
            rounds=3,
            created_at=_ts(1, 9, 0),
            updated_at=_ts(1, 9, 12),
        ),
        SessionSummary(
            session_id="22222222-2222-2222-2222-222222222222",
            title="差旅报销标准是多少",
            rounds=1,
            created_at=_ts(2, 14, 0),
            updated_at=_ts(2, 14, 3),
        ),
    ],
    "u-employee": [
        SessionSummary(
            session_id="33333333-3333-3333-3333-333333333333",
            title="年假怎么申请",
            rounds=2,
            created_at=_ts(1, 8, 0),
            updated_at=_ts(1, 8, 5),
        )
    ],
}


class FakeCatalog:
    def list_for(self, user_id: str, *, limit: int) -> Sequence[SessionSummary]:
        return tuple(SESSIONS.get(user_id, ())[:limit])


SEED_CHUNKS = [
    Chunk(
        "seed-pdf-1",
        "子块:每周加班时长按每月加班总时长折算",
        "bagu_ai",
        "中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf",
        "seed-pdf-parent",
        "劳动者每日工作时间不超过八小时、平均每周工作时间不超过四十四小时。用人单位由于"
        "生产经营需要,经与工会和劳动者协商后可以延长工作时间,一般每日不得超过一小时。",
        page=7,
    ),
    Chunk(
        "seed-pdf-2",
        "子块:延长工作时间的小时数",
        "bagu_ai",
        "中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf",
        "seed-pdf-parent",
        "",
        page=8,
    ),
    Chunk(
        "seed-md-1",
        "子块:年假申请流程",
        "bagu_java",
        "员工手册.md",
        "seed-md-parent",
        "年假申请走 OA,提前三天提交;审批通过后到 HR 备案。",
    ),
    # 受限文档(P0-6):员工在知识库清单里**看不到**它(他被授权了,但清单是管理员页);
    # 它存在的意义是让管理员页显示「限定可见」,并让权限弹层有东西可改。
    Chunk(
        "seed-quote-1",
        "子块:内部报价单",
        "uploads",
        "内部报价单.md",
        "seed-quote-parent",
        "内部报价单:标准版 39800 元 / 年,含 5 个坐席。",
    ),
]


class FakeStore:
    """假向量库 —— **真的存**:入库写进来,预览与删除都读它。

    与 `_fakes.FakeVectorStore` 的差别要说清:那个是**只读替身**(``upsert`` 只回个数、
    ``delete_by_doc_id`` 恒 0、``load_all`` 回的是构造时给的那几条),它服务的是"检索链路"
    那一批检查;而假后端要演示的是「上传 → 入库 → 预览 → 删除」这条**治理链路**,
    必须真的存下来 —— 第一版拿只读替身顶,结果上传成功、预览永远 0 段(当场露馅)。
    """

    def __init__(self, chunks=()) -> None:
        self.chunks: list[Chunk] = list(chunks)

    def upsert(self, chunks, embeddings, *, sparse=None) -> int:
        """写入(整行覆盖)。``sparse`` 收下就丢 —— 这一档没接稀疏编码器(见 cli/deps.py)。

        **签名要跟着端口走**:入库路径是 ``store.upsert(batch, embeddings, sparse=sparse)``,
        少一个关键字参数就是上传即失败(TypeError)—— 而这个假件不在验收集合里,
        没人跑它,直到有人用眼睛看界面时才发现(记在 STATUS 的事故里)。
        """
        incoming = {chunk.chunk_id for chunk in chunks}
        self.chunks = [c for c in self.chunks if c.chunk_id not in incoming] + list(chunks)
        return len(self.chunks)

    def has_sparse_column(self) -> bool:
        """输入:无。行为:这一档没有稀疏列。输出:False(与 `keyword.backend=memory` 的集合一致)。"""
        return False

    def delete_by_doc_id(self, doc_id: str) -> int:
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id]
        return before - len(self.chunks)

    def load_all(self):
        return list(self.chunks)

    def chunk_counts(self) -> dict[str, int]:
        """按 doc_id 数一遍:知识库清单拿它回答"这份现在到底搜不搜得到"。"""
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
        return counts

    def search(self, query_embedding, *, k, source=None):
        found = [c for c in self.chunks if source is None or c.source == source]
        return [RetrievedChunk(chunk=chunk, score=0.9) for chunk in found[:k]]

    def fingerprint(self):
        return sorted(f"{c.chunk_id}\x1f{c.parent_id}" for c in self.chunks)

    def delete_by_source(self, source: str) -> int:
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c.source != source]
        return before - len(self.chunks)

    def drop(self) -> None:
        self.chunks = []


# **一份共享实例**:问答的引用、预览、删除读的都是它 —— 三个入口共用一个库,
# 才会出现"删完之后预览变 404、引用里也没了"这种真实联动
VECTOR = FakeStore(SEED_CHUNKS)


# ── 文档治理(M7.2):登记表 / 假装载 / 假切分 ────────────────────────────────
# 认识的后缀:**一处定义两处用** —— capabilities 报给前端的就是它,磁盘索引也按它过滤
# (两份清单不一致时,前端会"看到 accept 允许 .docx、清单里却没有 .docx 那一类")。
SUFFIXES = (".pdf", ".md", ".txt", ".docx")

DOCS: dict[str, Any] = {}


def _seed_documents() -> None:
    """六份种子文档:**五种 state 各来一份**,界面上挨个看得到。

    * 劳动法 PDF —— ``ok``(可预览,带页码;浏览器实测拿它验跨页);
    * 员工手册.md —— ``changed``(登记 4096 字节,磁盘上是 5000);
    * 没有入库成功的.pdf —— ``not_indexed``(登记说 processed,库里一块没有);
    * 已经删掉的文件.md —— ``file_missing``(磁盘上找不到它);
    * reimburse.docx —— ``processing``(在途:不做一致性判定);
    * 未登记的语料.md —— ``unregistered``(**只在磁盘上**,登记表里没有它);
    * 内部报价单.md —— ``ok`` 但**被收窄过**(P0-6):卡片上要看到「限定可见」。
    """
    from project_rag.domain import DocRecord

    DOCS["中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf"] = DocRecord(
        document_id="中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf",
        source="bagu_ai",
        filename="中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf",
        status="processed",
        chunks=2,
        size_bytes=20480,
        created_at=_ts(3, 9, 0),
    )
    DOCS["员工手册.md"] = DocRecord(
        document_id="员工手册.md",
        source="bagu_java",
        filename="员工手册.md",
        status="processed",
        chunks=1,
        size_bytes=4096,
        created_at=_ts(2, 16, 20),
    )
    DOCS["reimburse.docx"] = DocRecord(
        document_id="reimburse.docx",
        source="uploads",
        filename="reimburse.docx",
        status="processing",
        chunks=0,
        size_bytes=8192,
        created_at=_ts(1, 9, 30),
    )
    DOCS["没有入库成功的.pdf"] = DocRecord(
        document_id="没有入库成功的.pdf",
        source="pdf",
        filename="没有入库成功的.pdf",
        status="processed",
        chunks=12,
        size_bytes=999,
        created_at=_ts(4, 10, 0),
    )
    DOCS["内部报价单.md"] = DocRecord(
        document_id="内部报价单.md",
        source="uploads",
        filename="内部报价单.md",
        status="processed",
        chunks=1,
        size_bytes=2048,
        created_at=_ts(6, 10, 0),
    )
    DOCS["已经删掉的文件.md"] = DocRecord(
        document_id="已经删掉的文件.md",
        source="doc",
        filename="已经删掉的文件.md",
        status="processed",
        chunks=5,
        size_bytes=100,
        created_at=_ts(5, 11, 0),
    )


def _seed_disk_files() -> tuple:
    """磁盘上那几份**合成**的语料(演示 changed / file_missing / unregistered)。

    真实的上传目录由 ``_disk_files()`` 现扫 —— 这一份只补"语料目录"的几种情况,
    因为假后端不往作者的 ``data/corpus`` 里写东西。
    """
    from project_rag.adapters.document_loader import DiskFile

    def entry(name: str, size: int, source: str) -> Any:
        return DiskFile(path=Path(f"/fake-corpus/{name}"), size_bytes=size, source=source,
                        doc_ids=(name,))

    return (
        entry("中华人民共和国劳动法_中华人民共和国人力资源和社会保障部.pdf", 20480, "bagu_ai"),
        # 与登记行对不上(登记 4096)→ changed
        entry("员工手册.md", 5000, "bagu_java"),
        entry("reimburse.docx", 8192, "uploads"),
        entry("没有入库成功的.pdf", 999, "pdf"),
        entry("内部报价单.md", 2048, "uploads"),  # 受限文档也要三处一致(state=ok)
        # 「已经删掉的文件.md」**故意不在**这里 → file_missing
        entry("未登记的语料.md", 1234, "doc"),
    )


def _disk_files() -> tuple:
    """输入:无。行为:磁盘索引 = 假上传目录里**真扫出来的文件** + 上面那几条合成的语料。

    **上传目录必须现扫**:上传完一份文件之后它就在那儿了 —— 固定清单会让刚入库的文档
    显示成「文件已不在」(状态判定的输入是假的,界面就会说谎)。
    """
    from project_rag.adapters.document_loader import list_files

    return (*list_files(_fake_uploads_dir(), suffixes=SUFFIXES), *_seed_disk_files())


def _fake_uploads_dir() -> Path:
    """输入:无。行为:假上传目录(与 `RAG_API__UPLOADS_DIR` 同一个口径)。输出:绝对路径。"""
    from project_rag.config import get_settings, project_path

    return project_path(get_settings().api.uploads_dir)


class FakeDocs:
    """登记表的内存替身(端口 `DocumentStore`)。"""

    def init_schema(self) -> None: ...
    def add(self, record): DOCS[record.document_id] = record
    def list_all(self):
        return sorted(DOCS.values(), key=lambda row: row.created_at, reverse=True)
    def get(self, document_id):
        return DOCS.get(document_id)
    def remove(self, document_id):
        return 1 if DOCS.pop(document_id, None) is not None else 0

    def upsert(self, record) -> None:
        DOCS[record.document_id] = record

    def set_status(self, document_id, status, *, chunks=None, error=None) -> None:
        from project_rag.domain import DocRecord

        old = DOCS.get(document_id)
        if old is None:
            return
        DOCS[document_id] = DocRecord(
            document_id=old.document_id,
            source=old.source,
            filename=old.filename,
            status=status,
            chunks=old.chunks if chunks is None else chunks,
            size_bytes=old.size_bytes,
            created_at=old.created_at,
            error=error,
        )


class FakeChunker:
    """把正文按 120 字切父块、40 字切子块,**并如实报偏移**(与真切分器同一份契约)。

    为什么不用真切分器:这里要的是「上传 → 入库 → 预览」这条链路在界面上走得通,
    而不必让假后端也去拉 langchain 与 jieba。偏移照报,所以页码那条链是真的。
    """

    PARENT = 120
    CHILD = 40

    def split(self, text):
        from project_rag.domain import ChunkGroup

        groups = []
        for start in range(0, max(len(text), 1), self.PARENT):
            parent = text[start : start + self.PARENT]
            children = [
                parent[offset : offset + self.CHILD]
                for offset in range(0, max(len(parent), 1), self.CHILD)
            ]
            starts = tuple(range(0, max(len(parent), 1), self.CHILD))
            groups.append(
                ChunkGroup(parent_text=parent, children=tuple(children), child_starts=starts)
            )
        return tuple(groups)


def fake_load(path):
    """输入:上传落盘的文件。行为:当成 UTF-8 文本读进来(不认的格式给一句占位)。输出:LoadedCorpus。

    **页码也造出来**:把正文按每 200 字一页切开,好让预览里的「第 N 页」是真算出来的,
    而不是硬编码在界面里的。
    """
    from project_rag.adapters.document_loader import LoadedCorpus
    from project_rag.domain import Document

    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        text = f"(假后端:读不了 {path.name} 的正文,当成二进制处理)"

    spans = []
    page_size = 200
    for page, start in enumerate(range(0, max(len(text), 1), page_size), start=1):
        spans.append((start, min(start + page_size, len(text)), page))
    return LoadedCorpus(
        documents=(Document(doc_id=path.name, text=text, source="uploads", pages=tuple(spans)),),
        skipped=(),
        empty=(),
    )


def _slow_down_ingest() -> None:
    """让入库慢一点(1.2 秒),好让浏览器看得见「处理中 → 已入库」这一步。"""
    import time

    time.sleep(1.2)


# ── 报表(M7.3):一份"像真的"的账本 ─────────────────────────────────────────
class FakeLedger:
    """假账本:按「行龄」过滤,好让不同窗口返回不同条数(1h 少、24h 多)。

    **刻意留了两个坑**,因为它们正是看板最该画对的地方:
    * ``rerank_ms`` 只出现在 3 行里(< p95 门槛 20)⇒ 那一格必须是 ``-`` 而不是 0;
    * 最慢的那一条是**冷启动**⇒ 最慢榜要标出来(分段统计里它不算)。
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    def init_schema(self) -> None: ...

    def record(self, row) -> None: ...

    def load_since(self, since):
        from datetime import UTC, datetime, timedelta

        from project_rag.ports.ledger import LEDGER_TS

        self.calls.append(since.timestamp())
        now = datetime.now(UTC)
        age_hours = (now - since).total_seconds() / 3600
        # **把时间戳附在行里,而且带时区**(与真适配器同一条口径):分时段计价靠它判高峰/空闲。
        # 带时区是必须的:裸钟点判不出时段,报表会说"时区不明,按高峰价计" —— 那是真适配器
        # 读到老账时的兜底表现,不是演示该有的样子(行龄 → 真实时刻,落哪个时段取决于你几点跑)。
        return [
            {**row, LEDGER_TS: (now - timedelta(hours=born)).isoformat()}
            for born, row in _ledger_rows()
            if born <= age_hours
        ]


def _ledger_row(index: int, *, age_hours: float, total: float, cold: bool = False, **over: str):
    base = {
        "label": "rag",
        "request_id": f"req-{index:03d}",
        "channel": "rag",
        "thinking": "off",
        "cold": "true" if cold else "false",
        "builds": "0",
        "total_ms": f"{total:.1f}",
        "ttft_ms": f"{total * 0.6:.1f}",
        "refused": "false",
        "truncated": "false",
        "aborted": "false",
        "embed_ms": f"{total * 0.05:.1f}",
        "vector_ms": f"{total * 0.08:.1f}",
        "bm25_ms": f"{total * 0.02:.1f}",
        "fuse_ms": "0.4",
        "collapse_ms": "0.3",
        "sources_ms": f"{total * 0.2:.1f}",
        "retrieve_ms": f"{total * 0.3:.1f}",
        "llm_ttft_ms": f"{total * 0.4:.1f}",
    }
    # P0-7:计费口径的三列。**只有真调了模型的行才有** —— FAQ 直答那几行是 "-"
    # (与真账本同一条口径:用量只记在调 provider 的那一层)。
    if base["channel"] == "rag":
        base["llm_model"] = "deepseek-flash"
        inputs = 800 + index * 37
        base["llm_input_tokens"] = str(inputs)
        # P0-7:prompt cache 的命中数(单价与未命中差 50 倍)。这里让约 60% 命中,
        # 好让界面上的「其中命中缓存」那一列有非零值可看。
        base["llm_cached_input_tokens"] = str(int(inputs * 0.6))
        base["llm_output_tokens"] = str(120 + index * 11)
    else:
        base["llm_model"] = "-"
        base["llm_input_tokens"] = "-"
        base["llm_cached_input_tokens"] = "-"
        base["llm_output_tokens"] = "-"
    if index % 8 == 3:
        base["rerank_ms"] = "668.0"  # 只有 3 行有重排 —— 样本不够,p95 该是 "-"
    return (age_hours, {**base, **over})


def _ledger_rows():
    """24 条热行 + 1 条冷启动(那条最慢)。"""
    rows = [
        _ledger_row(
            index,
            age_hours=0.2 + index * 0.4,  # 0.2h ~ 9.8h:1h 窗口只看得见前两条
            total=900.0 + index * 120.0,
            channel="faq" if index % 7 == 0 else "rag",
            refused="true" if index % 11 == 5 else "false",
            truncated="true" if index % 9 == 4 else "false",
            aborted="true" if index % 13 == 6 else "false",
        )
        for index in range(24)
    ]
    rows.append(
        _ledger_row(
            99,
            age_hours=30.0,  # 只落在 7d/30d 窗口里
            total=9200.0,
            cold=True,
            channel="rag",
            rerank_ms="5383.0",
        )
    )
    return rows


class FakeAccess:
    """假可见范围存储(P0-6):一个组 + 一份受限文档。

    形状与 ``ports/access.py`` 一致(内存版),``init_schema`` 是空操作 ——
    它存在的意义是让浏览器用例**有东西可点**:「限定可见」标记、权限弹层里的勾选框。
    """

    def __init__(self) -> None:
        self._groups = {"g-1": "销售"}
        self._members = {"g-1": ["u-employee"]}
        self._subjects: dict[str, tuple[list[str], list[str]]] = {
            "内部报价单.md": ([], ["g-1"]),
        }

    def init_schema(self) -> None: ...

    def create_group(self, name):
        from project_rag.domain.access import Group

        group_id = f"g-{len(self._groups) + 1}"
        self._groups[group_id] = name
        self._members[group_id] = []
        return Group(group_id=group_id, name=name)

    def list_groups(self):
        from project_rag.domain.access import Group

        return [Group(group_id=gid, name=name) for gid, name in sorted(self._groups.items())]

    def delete_group(self, group_id):
        self._groups.pop(group_id, None)
        self._members.pop(group_id, None)
        return 1

    def set_members(self, group_id, user_ids):
        self._members[group_id] = list(user_ids)

    def members_of(self, group_id):
        return tuple(self._members.get(group_id, ()))

    def groups_by_user(self):
        grouped: dict[str, list[str]] = {}
        for group_id, user_ids in self._members.items():
            for user_id in user_ids:
                grouped.setdefault(user_id, []).append(group_id)
        return {user_id: tuple(gids) for user_id, gids in grouped.items()}

    def set_document_subjects(self, document_id, *, user_ids, group_ids):
        if user_ids or group_ids:
            self._subjects[document_id] = (list(user_ids), list(group_ids))
        else:
            self._subjects.pop(document_id, None)

    def _acl(self, document_id):
        from project_rag.domain.access import DocumentAcl

        users, groups = self._subjects.get(document_id, ([], []))
        return DocumentAcl(
            document_id=document_id, user_ids=tuple(users), group_ids=tuple(groups)
        )

    def subjects_of(self, document_id):
        return self._acl(document_id)

    def rules(self):
        return {document_id: self._acl(document_id) for document_id in self._subjects}

    def forget_document(self, document_id):
        return 1 if self._subjects.pop(document_id, None) is not None else 0

    def forget_user(self, user_id):
        removed = 0
        for group_id in list(self._members):
            if user_id in self._members[group_id]:
                self._members[group_id] = [u for u in self._members[group_id] if u != user_id]
                removed += 1
        return removed


def _period_rule():
    """输入:无。行为:造一条与 config.yaml 同口径的高峰规则。输出:``PeriodRule``。

    节假日用**真表的一部分**:国庆那一段(界面演示很少正好跑在国庆,但口径要与真配置一致 ——
    演示用的假件一旦与真口径不一致,就会把"错的画面"演示成对的)。
    """
    from project_rag.application.cost import PeriodRule

    return PeriodRule.of(
        weekdays=[1, 2, 3, 4, 5],
        windows=[["09:00", "12:00"], ["14:00", "18:00"]],
        holidays=[["2026-10-01", "2026-10-07"]],
    )


def _price(peak_hit, off_hit, peak_miss, off_miss, peak_out, off_out):
    """输入:三档的高峰/空闲价。行为:造一个与 ``config.ModelPriceConfig`` 同形状的对象。输出:它。

    用同一个形状的**简单对象**而不是 pydantic 模型:假后端不该依赖配置层的校验,
    它只是把"报表要的那些数字"喂给界面(``application/cost.py`` 用鸭子类型读它)。
    """
    from types import SimpleNamespace

    def tier(peak, off_peak):
        return SimpleNamespace(peak=peak, off_peak=off_peak)

    return SimpleNamespace(
        input_cache_hit=tier(peak_hit, off_hit),
        input_cache_miss=tier(peak_miss, off_miss),
        output=tier(peak_out, off_out),
    )


# 假可见范围的**进程级单例**:授权改动要跨请求留着(浏览器用例会改它)
ACCESS = FakeAccess()


def build():
    _seed_documents()
    store = FakeAuthStore()
    # 会话归属必须真的绑上:否则 /api/v1/history/{id} 会 403(「这段会话没有归属记录」)——
    # 那是**对的行为**(P0-3a 的越权防线),但会让浏览器实测卡在"点会话没反应"上。
    for user_id, rows in SESSIONS.items():
        for row in rows:
            store.owners[row.session_id] = user_id
    llm = FakeLLM(("根据《劳动法》", "第三十六条,", "每日不超过八小时、每周不超过四十四小时;", "加班一般每日不超过一小时。"))

    def fake_ingest(store, record, path, **kwargs):  # noqa: ANN001
        """把真入库包一层:先睡 1.2 秒(界面上能看见「处理中」),再跑真的那套。"""
        _slow_down_ingest()
        return real_ingest(store, record, path, **kwargs)

    def options_spy(deps, request, access):  # noqa: ANN001
        """看一眼后端**实际**收到了什么检索参数(P0-6 起多一个 access 参数)。"""
        from project_rag.api import app as app_module

        result = original_options(deps, request, access)
        print(
            ">>> QUERY "
            + json.dumps(
                {
                    "request": request.model_dump(exclude_none=True),
                    "options": {
                        "k": result.k,
                        "max_context": result.max_context,
                        "source": result.source,
                        "fusion": result.fusion,
                        "bm25_weight": result.bm25_weight,
                        "vector_weight": result.vector_weight,
                        # 可见范围怎么落到检索上:被禁的文档 ID(空 = 不限制)
                        "denied_docs": list(result.denied_docs),
                    },
                    "rerank": original_rerank(deps, request),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    import project_rag.api.app as app_module
    import project_rag.application.documents as documents_module

    global original_options, original_rerank, real_ingest
    real_ingest = documents_module.ingest_uploaded
    # 只打这一个补丁就够了:`api/app.py` 的 `_run_ingest` 是**在函数里** import 这个名字的,
    # 所以它每次都从模块属性上取 —— 打在这里就生效(打在 api 模块上反而没用)
    documents_module.ingest_uploaded = fake_ingest
    original_options = app_module._options
    original_rerank = app_module._rerank_enabled
    # setattr 而不是直接赋值:劫持模块属性是为了**看后端收到了什么**,
    # 直接赋值会让 pyright 拿函数签名去比（类型层面它们确实不是一回事）
    setattr(app_module, "_options", options_spy)  # noqa: B010

    # dict[str, Any] + Deps(**fields):与 check_m7_frontend.py 同一写法 ——
    # 这里装的是假件(鸭子类型),逐参数比对没有意义,只会淹掉真问题
    fields: dict[str, Any] = {
        "llm_factory": lambda: llm,
        "faq_store_factory": lambda: FakeFaqStore([]),
        "matcher_factory": lambda: FakeMatcher(None),
        "cache_factory": None,
        "faq_threshold": 0.85,
        "embedder_factory": lambda: FakeEmbedder(),
        "vector_store_factory": lambda: VECTOR,
        "keyword_index_factory": lambda: FakeKeywordIndex(),
        "reranker_factory": lambda: FakeReranker(),
        # 注意是 **dict**(session_id -> 轮次):FakeConversationStore 按会话分桶,
        # 传 list 会在 .items() 上抛 AttributeError(接口 500 —— 假后端自己的 bug,记过一次)
        "conversation_store_factory": lambda: FakeConversationStore(
            {
                "11111111-1111-1111-1111-111111111111": [
                    ConversationTurn(question="加班上限是多少", answer="每日不超过一小时,特殊原因每日不超过三小时。"),
                    ConversationTurn(question="每月最多加班多久", answer="三十六小时。"),
                ]
            }
        ),
        "session_catalog_factory": lambda: FakeCatalog(),
        "auth_store_factory": lambda: store,
        "suffixes_factory": lambda: SUFFIXES,
        # M7.2:上传 / 删除 / 预览要的三样。切分与装载都是假的,但**偏移照报**,
        # 所以预览里的页码是真算出来的。
        "document_store_factory": lambda: FakeDocs(),
        # 清单的"磁盘那一处":实时清单靠它判 ok / changed / file_missing / unregistered
        "disk_files_factory": _disk_files,
        "chunker_factory": lambda: FakeChunker(),
        "loader_factory": lambda: fake_load,
        # M7.3:报表要一份账本(不然看板只能看到空窗)
        "ledger_store_factory": lambda: FakeLedger(),
        # P0-6:可见范围(一个组 + 一份受限文档)。接了它,浏览器用例才看得到
        # 「限定可见」标记与权限弹层;不接的话一切照旧(没规则 = 人人可见)。
        "access_store_factory": lambda: ACCESS,
        # P0-7:单价表(三档 × 两时段)+ 时段规则 —— 报表页因此有金额与时段可看
        # (数字与口径都按作者那份价目表:北京时间工作日 09–12/14–18 高峰、其余半价;
        #  假账本的时间戳在最近几小时,落哪个时段取决于你几点跑 —— 两种都看得见才对)
        "cost_prices": {
            "deepseek-flash": _price(0.04, 0.02, 2.0, 1.0, 8.0, 4.0)
        },
        "cost_currency": "CNY",
        "cost_rule": _period_rule(),
    }
    return create_app(Deps(**fields))


if __name__ == "__main__":
    print(f"fake backend on http://127.0.0.1:18081 账号:{ACCOUNTS}", flush=True)
    uvicorn.run(build(), host="127.0.0.1", port=18081, log_level="warning")

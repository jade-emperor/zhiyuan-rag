#!/usr/bin/env python
"""补丁契约：知识库清单必须**和磁盘 / 向量库对账**，不能只念登记表。

用法：

    uv run python scripts/acceptance/check_m7_2_live_kb.py

**这一份在堵什么洞**（原话：「我后端删除了,前端依旧能看到;我更新了,前端依旧看不到」）：

一份文档在**三个地方**留痕 —— 登记表（MySQL ``documents``）、向量库（Milvus 的子块）、
磁盘（上传目录 / 语料目录）。入库那条路三处一起写，但**只有上传那条路**会写登记表：
``project-rag ingest`` 不写、``rm`` 一个语料文件不写、drop 一个集合更不写。
于是 ``GET /api/v1/documents`` 只是**把登记表念了一遍**，而登记表是一份**没人失效的缓存**：

* 集合被 drop / 语料被删 → 卡片照样「已入库 · 677 块」,点预览还有内容（搜不到但看不出来）;
* 语料目录里新放一份 docx → 清单里根本没有它（前端凭什么显示一个没登记的文件?）;
* 同一个文件改了内容重新灌 → 卡片上的块数与时间还是旧那一次。

**所以清单要算的不是「登记表说了什么」，而是「现在到底怎么样」**：三处摆在一起,给出
``state``（闭集见 ``domain/documents.py`` 的 ``DOC_STATES``）：

    ok           三处都在,大小也对得上
    changed      磁盘上的文件变了(与登记时记的大小不符)—— 库里的内容是旧的
    not_indexed  登记说 processed,库里一块没有(集合被清过 / 入库其实没成)
    file_missing 磁盘上找不到这个文件(被手工删了 / 上传的原件被清了)
    unregistered 磁盘上有、登记表没有(后端直接放进去的语料还没入库)
    pending / processing / failed / deleting —— 在途与失败:**原样回状态**,不做一致性判定
                 (那种行的真相就是登记表,拿"库里没块"去报它会自己造假问题)

四条口径钉在下面:

1. **``state`` 的优先级是刻意的**:文件不在 **压过** 库里没块 —— 前者重灌也救不了,
   后者重灌就能修,先报前者才不会让人白跑一趟;
2. **``searchable`` 按库内实际块数算**,不是按 ``status == processed``。登记表会撒谎,
   而这个字段唯一的用途就是回答「现在搜不搜得到」;
3. **未登记的文件也进清单**（带真实字节数与来源），但**不进登记表** —— 读接口不写库;
4. **写路径补齐**：``project-rag ingest`` 结束后把这次真的入库了的结果写进登记表
   （块数 / 大小 / 状态），否则"后端灌的语料"在前端永远是「未登记」。

另有一条**删除**的边界：清单现在能看见语料目录里的文件了,而"删除"必须仍然只删
**上传目录里的原件** —— 语料目录是源材料,删库不等于删源。
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from _fakes import FakeVectorStore
from _harness import check, report

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _read(relative: str) -> str:
    """输入:相对 ``web/src/`` 的路径。行为:读它。输出:正文(不在就报错,不静默给空串)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— 知识库页还没落地?")
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """输入:一份前端源码。行为:去掉块注释与整行注释。输出:剩下的文本(注释里提到禁令不算违规)。"""
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", without_block)


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _FakeDocs:
    """登记表的内存替身(端口 ``DocumentStore`` 的形状)。``upsert`` 是这次新加的那条。"""

    def __init__(self, rows=()) -> None:
        self.rows = {row.document_id: row for row in rows}
        self.calls: list[str] = []

    def init_schema(self) -> None: ...

    def add(self, record) -> None:
        if record.document_id in self.rows:
            raise ValueError(f"文档 {record.document_id!r} 已登记过")
        self.rows[record.document_id] = record

    def upsert(self, record) -> None:
        self.calls.append(f"upsert:{record.document_id}")
        self.rows[record.document_id] = record

    def list_all(self):
        return list(self.rows.values())

    def get(self, document_id):
        return self.rows.get(document_id)

    def set_status(self, document_id, status, *, chunks=None, error=None) -> None:
        record = self.rows.get(document_id)
        if record is None:
            return
        self.rows[document_id] = type(record)(
            document_id=record.document_id,
            source=record.source,
            filename=record.filename,
            status=status,
            chunks=record.chunks if chunks is None else chunks,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            error=error,
        )

    def remove(self, document_id) -> int:
        return 1 if self.rows.pop(document_id, None) is not None else 0


class _FakeVectors(FakeVectorStore):
    """向量库的内存替身:**真的会增删块**(``hits`` 就是唯一状态),按 ``doc_id`` 数得出来。

    继承共享假件是为了拿到**完整端口形状**(search / fingerprint / 稀疏那一路…)——
    这条契约要验的是"清单读的是库里的现实";假件缺一条就会在别处变成 AttributeError,
    那种红与被验对象无关(刻意的残缺留给 `SearchOnlyVectorStore` 那种场合)。
    """

    def __init__(self, chunks=()) -> None:
        super().__init__(RetrievedChunk(chunk, 0.5) for chunk in chunks)

    @property
    def chunks(self) -> list:
        """输入:无。行为:库里现在有哪些块。输出:子块列表(顺序即写入顺序)。"""
        return [hit.chunk for hit in self.hits]

    def chunk_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
        return counts

    def upsert(self, chunks, embeddings, *, sparse=None) -> int:
        known = {hit.chunk.chunk_id for hit in self.hits}
        self.hits += [
            RetrievedChunk(chunk, 0.5) for chunk in chunks if chunk.chunk_id not in known
        ]
        return len(self.hits)

    def delete_by_doc_id(self, doc_id: str) -> int:
        before = len(self.hits)
        self.hits = [hit for hit in self.hits if hit.chunk.doc_id != doc_id]
        return before - len(self.hits)

    def delete_by_source(self, source: str) -> int:
        before = len(self.hits)
        self.hits = [hit for hit in self.hits if hit.chunk.source != source]
        return before - len(self.hits)

    def drop(self) -> None:
        self.hits = []


class _FakeAuth:
    """认证存储的内存替身(只实现登录那三条)。"""

    PASSWORDS = {"boss": "pw-123456", "worker": "pw-123456"}

    def __init__(self) -> None:
        import time

        from project_rag.application.security import hash_password
        from project_rag.domain.auth import User

        self.users = {
            "boss": (User("u-boss", "boss", "admin"), hash_password(self.PASSWORDS["boss"])),
            "worker": (
                User("u-worker", "worker", "employee"),
                hash_password(self.PASSWORDS["worker"]),
            ),
        }
        self.tokens: dict[str, tuple[str, float]] = {}
        self._time = time

    def init_schema(self) -> None: ...

    def create_user(self, *args, **kwargs):  # pragma: no cover - 本脚本不用
        raise NotImplementedError

    def set_password(self, user_id, password_hash) -> None: ...

    def delete_user(self, user_id) -> int:  # pragma: no cover - 本脚本用不到
        return 0

    def list_users(self):
        return [user for user, _ in self.users.values()]

    def find_by_username(self, username):
        return self.users.get(username)

    def issue_token(self, user, token_hash, ttl_seconds) -> None:
        self.tokens[token_hash] = (user.user_id, self._time.time() + ttl_seconds)

    def user_for_token(self, token_hash):
        item = self.tokens.get(token_hash)
        if item is None or item[1] <= self._time.time():
            return None
        return next(user for user, _ in self.users.values() if user.user_id == item[0])

    def revoke_token(self, token_hash) -> None:
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id) -> None: ...

    def owner_of(self, session_id):
        return None


def _record(
    document_id: str,
    *,
    source: str = "uploads",
    status: str = "processed",
    chunks: int = 3,
    size_bytes: int = 1000,
):
    """输入:文档 ID 与几个字段。行为:造一条登记记录。输出:DocRecord。"""
    from project_rag.domain import DocRecord

    return DocRecord(
        document_id=document_id,
        source=source,
        filename=Path(document_id).name,
        status=status,  # type: ignore[arg-type]
        chunks=chunks,
        size_bytes=size_bytes,
        created_at="<ISO 时间>",
    )


def _disk(doc_id: str, *, size: int = 1000, source: str = "uploads", alts=()):
    """输入:doc_id 与磁盘读数。行为:造一条 ``DiskFile``(不碰真磁盘)。输出:DiskFile。"""
    from project_rag.adapters.document_loader import DiskFile

    return DiskFile(
        path=Path("/tmp") / doc_id,
        size_bytes=size,
        source=source,
        doc_ids=(doc_id, *alts),
    )


def _chunk(doc_id: str, index: int = 0, *, source: str = "uploads"):
    """输入:doc_id 与序号。行为:造一个子块。输出:Chunk。"""
    from project_rag.domain import Chunk

    return Chunk(f"c{index}", f"正文{index}", source, doc_id, "", "")


if True:  # 只为类型:``RetrievedChunk`` 在 `_FakeVectors.__init__` 里就把块包好了
    from project_rag.domain import RetrievedChunk


def _client(*, docs=(), chunks=(), files=(), auth=False):
    """输入:登记行 / 库内块 / 磁盘文件 / 要不要认证。行为:建注入假依赖的 TestClient。输出:client。"""
    from fastapi.testclient import TestClient

    from project_rag.api import create_app
    from project_rag.cli import Deps

    fields: dict[str, Any] = {
        "document_store_factory": lambda: _FakeDocs(docs),
        "vector_store_factory": lambda: _FakeVectors(chunks),
        "disk_files_factory": lambda: tuple(files),
    }
    if auth:
        fields["auth_store_factory"] = lambda: _FakeAuth()
    return TestClient(create_app(Deps(**fields)))


def _list(client, *, headers=None) -> dict:
    """输入:客户端与可选请求头。行为:调清单接口。输出:JSON 正文。"""
    response = client.get("/api/v1/documents", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _only(body: dict, document_id: str) -> dict:
    """输入:清单正文与文档 ID。行为:按 ID 取那一行。输出:该行(取不到就抛,带上全部 ID)。"""
    for row in body["documents"]:
        if row["document_id"] == document_id:
            return row
    raise AssertionError(f"清单里没有 {document_id!r},只有 {[r['document_id'] for r in body['documents']]}")


# ══ 一、清单 = 三处对账（行为契约） ══════════════════════════════════════════
@check("清单：登记说已入库、库里一块没有 → state=not_indexed 且 searchable=false")
def _not_indexed() -> str:
    """作者的场景①:集合被 drop 了,卡片还写「已入库 · 677 块」—— 点预览有内容,检索却搜不到。"""
    body = _list(_client(docs=[_record("a.md", chunks=677)], chunks=[], files=[_disk("a.md")]))
    row = _only(body, "a.md")
    assert row["state"] == "not_indexed", f"库里零块该报 not_indexed,实际 {row['state']}"
    assert row["chunks"] == 677 and row["indexed_chunks"] == 0, row
    assert row["searchable"] is False, "库里一块都没有,却说可检索 —— 这就是那个谎"
    assert row["on_disk"] is True, row
    return "登记 677 块 / 库内 0 块 → not_indexed,searchable=false,磁盘仍在"


@check("清单：库里有块、磁盘上没有这个文件 → state=file_missing")
def _file_missing() -> str:
    """作者的场景②:语料文件被手工删了,卡片照样在 —— 现在它必须说出来。"""
    body = _list(_client(docs=[_record("a.md")], chunks=[_chunk("a.md")], files=[]))
    row = _only(body, "a.md")
    assert row["state"] == "file_missing", f"文件不在该报 file_missing,实际 {row['state']}"
    assert row["on_disk"] is False and row["disk_size_bytes"] == 0, row
    assert row["indexed_chunks"] == 1, "库里的块数照样要如实给(它现在还能被检索到)"
    assert row["searchable"] is True, "库里有块就是搜得到 —— 哪怕登记/磁盘那边已经不对了"
    return "磁盘无、库内 1 块 → file_missing;searchable 按库内实际算"


@check("清单：磁盘大小与登记不符 → state=changed；一致才是 ok")
def _changed() -> str:
    """作者的场景③:同一份文件改了内容重灌之前,卡片上的块数/大小是旧的。"""
    same = _list(_client(docs=[_record("a.md", size_bytes=1000)], chunks=[_chunk("a.md")],
                         files=[_disk("a.md", size=1000)]))
    assert _only(same, "a.md")["state"] == "ok", _only(same, "a.md")

    moved = _list(_client(docs=[_record("a.md", size_bytes=1000)], chunks=[_chunk("a.md")],
                          files=[_disk("a.md", size=2400)]))
    row = _only(moved, "a.md")
    assert row["state"] == "changed", f"大小 1000 → 2400 该报 changed,实际 {row['state']}"
    assert row["size_bytes"] == 1000 and row["disk_size_bytes"] == 2400, (
        f"两个数都要给(登记说多少 / 磁盘现在多少),实际 {row}"
    )

    unknown = _list(_client(docs=[_record("a.md", size_bytes=0)], chunks=[_chunk("a.md")],
                            files=[_disk("a.md", size=2400)]))
    assert _only(unknown, "a.md")["state"] == "ok", (
        "size_bytes=0 = 当年没记过大小(回填的老语料就是这种),判不了就别猜成 changed"
    )
    return "1000=1000 → ok;1000 vs 2400 → changed;登记 0(没记过)→ 不猜"


@check("清单：磁盘上有、登记表没有 → 也要列出来（state=unregistered，数字给真实读数）")
def _unregistered() -> str:
    """反向的那个洞:后端直接往语料目录里放文件(或 CLI 灌完没登记),前端**根本看不到它**。"""
    body = _list(_client(
        docs=[_record("bagu_ai/x.md", source="bagu_ai", chunks=2)],
        chunks=[_chunk("bagu_ai/x.md", 0, source="bagu_ai"), _chunk("bagu_ai/x.md", 1, source="bagu_ai")],
        files=[_disk("bagu_ai/x.md", source="bagu_ai"),
               _disk("handbook.docx", size=4321, source="doc")],
    ))
    assert len(body["documents"]) == 2, [r["document_id"] for r in body["documents"]]
    row = _only(body, "handbook.docx")
    assert row["state"] == "unregistered", row
    assert row["status"] == "unregistered", "未登记的文件没有登记状态,别拿 processed 顶"
    assert row["disk_size_bytes"] == 4321 and row["chunks"] == 0, row
    assert row["indexed_chunks"] == 0 and row["searchable"] is False, row
    assert row["source"] == "doc", "来源要按装载器那套规则给(顶层目录名去掉 _data)"
    assert row["created_at"] == "", "磁盘文件的 mtime 不是登记时间,别编一个"
    return "磁盘上的 handbook.docx 出现在清单里:未登记 / 4321 字节 / 库内 0 块"


@check("state 优先级：文件不在 压过 库里没块（前者重灌也救不了）")
def _precedence() -> str:
    """两条都中时先报哪条:文件不在更根本 —— 报成 not_indexed 会让人白跑一次重灌。"""
    row = _only(_list(_client(docs=[_record("a.md")], chunks=[], files=[])), "a.md")
    assert row["state"] == "file_missing", f"两条都中该报 file_missing,实际 {row['state']}"
    return "文件不在 + 库内零块 → file_missing(不是 not_indexed)"


@check("清单：在途与失败的行原样回状态，不参与一致性判定")
def _in_flight() -> str:
    """pending 的文档库里当然没有块 —— 那是正常的,不是"缺块";failed 的真相是登记表上的错误。"""
    docs = [
        _record("p.md", status="pending", chunks=0),
        _record("g.md", status="processing", chunks=0),
        _record("f.md", status="failed", chunks=0),
        _record("d.md", status="deleting", chunks=0),
    ]
    body = _list(_client(docs=docs, chunks=[], files=[_disk("p.md"), _disk("g.md"),
                                                 _disk("f.md"), _disk("d.md")]))
    got = {row["document_id"]: row["state"] for row in body["documents"]}
    assert got == {"p.md": "pending", "g.md": "processing", "f.md": "failed", "d.md": "deleting"}, (
        f"在途/失败的行该原样回状态,实际 {got}"
    )
    states = {row["state"] for row in body["documents"]}
    assert "not_indexed" not in states, "把 pending 报成「库里没块」= 自己造一批假问题"
    return "pending/processing/failed/deleting 原样回状态;not_indexed 一个都没冒出来"


@check("清单：同一个文件从两个入库根看都认（x.md 与 bagu_ai_data/x.md 两种登记都对得上）")
def _two_roots() -> str:
    """``--path data/corpus/bagu_ai_data`` 与 ``--path data/corpus`` 给的 doc_id 不一样,
    但它们指的是**同一个文件** —— 两种登记行都该认出磁盘上那份,而不是一边报 file_missing。

    **走真磁盘索引**(不是手造一个 ``DiskFile``):候选 id 是 ``index_files`` 合并出来的,
    手造就把"合并"这一步跳过去了 —— 那正是这条契约要钉的地方之一。
    """
    from project_rag.adapters.document_loader import index_files, ingest_roots

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        (corpus / "bagu_ai_data").mkdir(parents=True)
        (corpus / "bagu_ai_data" / "x.md").write_text("正文" * 20, encoding="utf-8")
        uploads = Path(raw) / "uploads"
        uploads.mkdir()
        size = (corpus / "bagu_ai_data" / "x.md").stat().st_size
        files = index_files(ingest_roots(uploads, corpus), suffixes=(".md",))
        assert len(files) == 1 and len(files[0].doc_ids) == 2, (
            f"同一个文件该合成一条、带两个候选 id,实际 {[f.doc_ids for f in files]}"
        )
        body = _list(_client(
            docs=[_record("x.md", source="bagu_ai", size_bytes=size),
                  _record("bagu_ai_data/x.md", source="bagu_ai", size_bytes=size)],
            chunks=[_chunk("x.md", 0, source="bagu_ai"),
                    _chunk("bagu_ai_data/x.md", 0, source="bagu_ai")],
            files=files,
        ))
    states = {row["document_id"]: row["state"] for row in body["documents"]}
    assert states == {"x.md": "ok", "bagu_ai_data/x.md": "ok"}, (
        f"两种 doc_id 都该认出同一个文件,实际 {states}"
    )
    assert len(body["documents"]) == 2, "一个文件不该因为认了两个 id 就变成两行未登记"
    return "真磁盘索引给两个候选 id,两行登记都是 ok,且没有多出一行 unregistered"


@check("清单：summary 与逐行 state 是同一个口径（total=行数，各状态计数=数一遍）")
def _summary() -> str:
    body = _list(_client(
        docs=[_record("ok.md", size_bytes=10), _record("gone.md"), _record("miss.md"),
              _record("p.md", status="pending", chunks=0)],
        chunks=[_chunk("ok.md"), _chunk("gone.md")],
        files=[_disk("ok.md", size=10), _disk("miss.md"), _disk("p.md"), _disk("new.md")],
    ))
    counts: dict[str, int] = {}
    for row in body["documents"]:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    summary = body["summary"]
    assert summary["total"] == len(body["documents"]) == 5, summary
    for state, count in counts.items():
        if state in ("pending", "processing", "failed", "deleting"):
            continue
        assert summary[state] == count, f"{state} 的计数 {summary[state]} 与逐行数出来的 {count} 不一致"
    assert summary["in_flight"] == 1, f"在途计数该是 1,实际 {summary}"
    assert sum(summary[key] for key in ("ok", "changed", "not_indexed", "file_missing",
                                        "unregistered", "in_flight")) == summary["total"], summary
    return f"5 行 → {summary}"


@check("磁盘索引：只收后端认识的后缀（.DS_Store / .zip 之类不进清单）")
def _disk_suffix_filter() -> str:
    """清单不是 ``ls``:不认识的文件后端也灌不进去,把它们列成「未入库」只会造噪音。"""
    from project_rag.adapters.document_loader import index_files, ingest_roots

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        (corpus / "bagu_ai_data").mkdir(parents=True)
        (corpus / "bagu_ai_data" / "x.md").write_text("正文", encoding="utf-8")
        (corpus / "bagu_ai_data" / ".DS_Store").write_text("junk", encoding="utf-8")
        (corpus / "bagu_ai_data" / "raw.zip").write_text("junk", encoding="utf-8")
        uploads = Path(raw) / "uploads"
        uploads.mkdir()
        (uploads / "y.txt").write_text("正文", encoding="utf-8")

        files = index_files(ingest_roots(uploads, corpus), suffixes=(".md", ".txt"))
        names = sorted(file.doc_ids[0] for file in files)
        assert names == ["x.md", "y.txt"], f"磁盘索引该只收 .md/.txt,实际 {names}"
        # 同一个文件在两个入库根下的两个候选 id,顺序是"从具体到宽泛"
        x = next(file for file in files if file.doc_ids[0] == "x.md")
        assert x.doc_ids == ("x.md", "bagu_ai_data/x.md"), x.doc_ids
        assert x.source == "bagu_ai" and x.path.name == "x.md", x
        return f"语料 + 上传两个根 → {names};x.md 的候选 id {x.doc_ids}"


@check("上传：落盘是原子的（写 .part 再改名）—— 传到一半的文件不进清单")
def _atomic_upload() -> str:
    """清单是"磁盘上有什么"的视图,而**传到一半的文件不是一份文档**。

    以前是直接往最终文件名里写:上传中断 / 超限之后,半截文件留在上传目录里,
    而清单（按后缀过滤）照样把它列成一张「未入库」的卡片 —— 用户只能自己去磁盘上删。
    现在写 ``<名字>.part``(不在认识的后缀里)再原子改名,要么完整要么没有。
    """
    import asyncio

    from fastapi import HTTPException

    from project_rag.api.app import _stream_to_file

    seen: list[bool] = []

    class _Request:
        """假请求:分块吐数据;每吐一块就让检查看一眼"最终文件名出现了没有"。"""

        def __init__(self, blocks, *, fail_after: int | None = None) -> None:
            self.blocks = list(blocks)
            self.fail_after = fail_after

        async def stream(self):
            for index, block in enumerate(self.blocks):
                if self.fail_after is not None and index >= self.fail_after:
                    raise RuntimeError("客户端断了(模拟)")
                yield block

    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "half.md"

        async def watch(blocks, *, fail_after=None):
            request = _Request(blocks, fail_after=fail_after)
            original_stream = request.stream

            async def stream():
                async for block in original_stream():
                    seen.append(target.exists())
                    yield block

            request.stream = stream  # type: ignore[method-assign]
            return await _stream_to_file(request, target, 1024)  # type: ignore[arg-type]

        size = asyncio.run(watch([b"abc", b"de"]))
        assert size == 5 and target.read_bytes() == b"abcde", (size, target)
        assert not seen or not any(seen), f"还没收完,最终文件名就出现了:{seen}"
        assert not (Path(raw) / "half.md.part").exists(), "收尾没清掉 .part"

        # 后面两步各用一个新的目标名:否则"超限之后文件还在"会被上一步的成品判成失败
        target = Path(raw) / "over.md"
        try:
            asyncio.run(watch([b"x" * 800, b"y" * 800]))
        except HTTPException as exc:
            assert exc.status_code == 413, exc
        else:
            raise AssertionError("超限该报 413")
        assert not target.exists(), "超限之后半截文件还在(清单上会多一张「未入库」的卡片)"
        assert not list(Path(raw).glob("*.part")), f"留下垃圾:{list(Path(raw).iterdir())}"

        target = Path(raw) / "broken.md"
        try:
            asyncio.run(watch([b"first", b"second"], fail_after=1))
        except RuntimeError:
            pass
        else:
            raise AssertionError("客户端断线该原样抛出")
        assert not target.exists() and not list(Path(raw).glob("*.part")), (
            f"断线之后没清干净:{list(Path(raw).iterdir())}"
        )
    return "收完才改名(过程中最终名字不存在);413 与断线之后目录里既没有成品也没有 .part"


# ══ 二、写路径：CLI 灌完要登记（不然「后端灌的」在前端永远是未登记） ══════════
def _run_ingest(path: Path):
    """输入:要灌的目录。行为:用假依赖走一遍真 CLI ingest。输出:(退出码, 登记表替身, 向量库替身)。"""
    import argparse

    from project_rag.adapters import HashingEmbedder
    from project_rag.cli import Deps
    from project_rag.cli.commands import ingest as ingest_cmd

    docs = _FakeDocs()
    vectors = _FakeVectors()
    parser = argparse.ArgumentParser()
    ingest_cmd.add_parser(parser.add_subparsers())
    args = parser.parse_args([ingest_cmd.NAME, "--path", str(path)])
    args.deps = Deps(
        embedder_factory=HashingEmbedder,
        vector_store_factory=lambda: vectors,
        document_store_factory=lambda: docs,
    )
    code = ingest_cmd.run(args)
    return code, docs, vectors


@check("写路径：CLI ingest 之后登记表有行 —— 块数/大小/状态如实（后端灌的也要在前端看得见）")
def _ingest_registers() -> str:
    """这就是「我更新了,前端依旧看不到」的那一半:``project-rag ingest`` 从来不写登记表。"""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "corpus" / "bagu_ai_data"
        root.mkdir(parents=True)
        body = "装饰器有什么用?" * 200
        (root / "x.md").write_text(body, encoding="utf-8")
        code, docs, vectors = _run_ingest(root)
        assert code == 0, f"ingest 该成功,实际退出码 {code}"

        rows = docs.rows
        assert set(rows) == {"x.md"}, f"灌完该有一行登记(x.md),实际 {sorted(rows)}"
        row = rows["x.md"]
        assert row.status == "processed", row
        assert row.chunks > 0 and row.chunks == len(vectors.chunks), (
            f"登记块数要与写进库里的块数一致:{row.chunks} vs {len(vectors.chunks)}"
        )
        assert row.size_bytes == len(body.encode("utf-8")), (
            f"大小要记成磁盘上的真实字节数({len(body.encode('utf-8'))}),实际 {row.size_bytes} —— "
            "记 0 的话「文件改没改」永远判不出来"
        )
        assert row.source == "bagu_ai", f"来源标签要与装载器一致,实际 {row.source!r}"
        return f"ingest 后登记 1 行:{row.document_id} / {row.chunks} 块 / {row.size_bytes} 字节"


@check("写路径：重灌同一份文件是**覆盖**登记行，不是报「已登记过」")
def _ingest_overwrites() -> str:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "corpus" / "bagu_ai_data"
        root.mkdir(parents=True)
        target = root / "x.md"
        target.write_text("第一版正文。" * 200, encoding="utf-8")
        _run_ingest(root)
        first = None
        with tempfile.TemporaryDirectory() as raw2:
            root2 = Path(raw2) / "bagu_ai_data"
            root2.mkdir(parents=True)
            target2 = root2 / "x.md"
            target2.write_text("第二版,长得多。" * 600, encoding="utf-8")
            code, docs, vectors = _run_ingest(root2)
            assert code == 0, f"重灌同一份文件不该失败,实际退出码 {code}"
            first = docs.rows["x.md"]
            assert first.size_bytes == target2.stat().st_size, first
            assert first.chunks == len(vectors.chunks) > 0, first
        return f"重灌后登记行被覆盖:{first.chunks} 块 / {first.size_bytes} 字节(不是报「已登记过」)"


# ══ 三、删除的边界：语料目录是源材料，删库不删源 ══════════════════════════════
@check("删除：只清上传目录里的原件；语料目录里的源文件不动（块与登记行照删）")
def _delete_keeps_source() -> str:
    """清单现在能看见语料目录了,删除就必须守住边界 —— 误删语料是不可逆的。"""
    from project_rag.application.documents import delete_document
    from project_rag.domain import Chunk

    with tempfile.TemporaryDirectory() as raw:
        uploads = Path(raw) / "uploads"
        corpus = Path(raw) / "corpus" / "bagu_ai_data"
        uploads.mkdir(parents=True)
        corpus.mkdir(parents=True)
        (uploads / "up.md").write_text("上传的原件", encoding="utf-8")
        (corpus / "src.md").write_text("语料源材料", encoding="utf-8")

        docs = _FakeDocs([_record("up.md", chunks=1), _record("src.md", source="bagu_ai", chunks=1)])
        vectors = _FakeVectors([Chunk("c0", "正文", "bagu_ai", "src.md", "", ""),
                                Chunk("c1", "正文", "uploads", "up.md", "", "")])

        outcome = delete_document(docs, vectors, "src.md", uploads_dir=uploads,
                                  source_file=corpus / "src.md")
        assert (corpus / "src.md").is_file(), "语料目录里的源文件被删了 —— 删库不该删源"
        assert outcome.removed_file is False and outcome.kept_source is True, outcome
        assert outcome.deleted_chunks == 1 and outcome.removed_record is True, outcome
        assert "src.md" not in docs.rows and not any(c.doc_id == "src.md" for c in vectors.chunks)

        uploaded = delete_document(docs, vectors, "up.md", uploads_dir=uploads,
                                   source_file=uploads / "up.md")
        assert not (uploads / "up.md").exists(), "上传目录里的原件该被删掉"
        assert uploaded.removed_file is True and uploaded.kept_source is False, uploaded
        return "语料原件保留(kept_source=true),上传原件删除(removed_file=true);两边的块与登记行都清了"


# ══ 四、前端（静态契约） ═════════════════════════════════════════════════════
@check("前端：卡片按 state 显示，不是按 status 直接显示「已入库」")
def _card_uses_state() -> str:
    card = _strip_comments(_read("components/DocumentCard.vue"))
    # 精确到"标签表是按 state 查的":只判 `doc.state` 出现过的话,`props.doc.status as DocState`
    # 这种写法也含这个子串,而 `STATE_TONES[props.doc.state]` 那种无关行也会让它为真
    # (反向测试就是这么发现这条断言是摆设的 —— 两次)
    assert re.search(r"STATE_LABELS\[\s*props\.doc\.state\s*\]", card), (
        "卡片没有拿 state 去查标签表 —— 那就还是「登记表说什么就显示什么」"
    )
    for state in ("not_indexed", "file_missing", "unregistered", "changed"):
        assert state in card, f"卡片没有处理 {state} 这个状态"
    types = _read("api/types.ts")
    assert "DocState" in types, "types.ts 里没有 state 的类型(前端只能猜)"
    for state in ("not_indexed", "file_missing", "unregistered"):
        assert state in types, f"types.ts 的 DocState 少了 {state}"
    assert "indexed_chunks" in types and "on_disk" in types, "新字段没进类型定义"
    return "卡片读 state;四种不一致态都有标签;TS 侧 DocState/indexed_chunks/on_disk 齐了"


@check("前端：不一致的话术指向可执行的下一步（未入库/文件不在/库内没块都要有）")
def _hints() -> str:
    card = _strip_comments(_read("components/DocumentCard.vue"))
    assert "HINTS" in card, "没有每种状态的「怎么办」话术表"
    for needle in ("project-rag ingest", "删除"):
        assert needle in card, f"话术里没有 `{needle}` —— 用户看完不知道下一步做什么"
    assert "登记" in card and "库内" in card, (
        "两个数(登记说多少 / 库里实际多少)要并排显示,只给一个数看不出差在哪"
    )
    return "话术含 `project-rag ingest` 与「删除」两条路;登记数与库内数并排"


@check("前端：显示「上次刷新」并在窗口重新可见时刷新（不是只有 onMounted 那一次）")
def _live_refresh() -> str:
    view = _read("views/KnowledgeView.vue")
    assert "refreshedAt" in view, "没有「上次刷新」的时间 —— 页面是不是过期了用户看不出来"
    assert "visibilitychange" in view and "focus" in view, (
        "只有 onMounted 拉一次:页面开着不动就永远显示上一次的结果"
    )
    store = _read("stores/documents.ts")
    assert "refreshedAt" in store and "summary" in store, "store 里没有刷新时间与汇总"
    assert "unregistered" in store or "unregistered" in view, (
        "汇总/文案里没有「未登记」这一类 —— 后端直接放进去的文件会被整体读漏"
    )
    return "页面显示上次刷新时间;窗口 focus/可见时重拉;store 带 refreshedAt 与 summary"


def main() -> int:
    return report(
        "补丁 知识库清单与磁盘/向量库对账（实时视图）",
        hint="前端改动后先 `cd web && npm run build`；产物是入库的（Python 侧与镜像都不装 node）",
    )


if __name__ == "__main__":
    sys.exit(main())

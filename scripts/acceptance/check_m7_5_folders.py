#!/usr/bin/env python
"""契约:知识库的**两级结构** —— 一级目录(folder)与目录里的文档。

用法：

    uv run python scripts/acceptance/check_m7_5_folders.py

**这一批在堵什么洞**:知识库原来只有一层 —— 清单把 8 份文档平铺出来,唯一的"分组"痕迹是
``source``(顶层语料目录名去掉 ``_data``),而它是**物理来源**、不是业务分类。
于是"公司制度放哪、产品资料放哪"只能靠文件名自己读出来。这一批加上一级目录:

    GET    /api/v1/folders                  -> {folders, unfiled, summary}
    GET    /api/v1/folders/{folder_id}      -> {folder, documents, summary}
    POST   /api/v1/folders                  -> 建目录（管理员）
    PATCH  /api/v1/folders/{folder_id}      -> 改名/改说明（管理员）
    DELETE /api/v1/folders/{folder_id}      -> 只让删**空**目录（管理员）
    PATCH  /api/v1/documents/{document_id}  -> 挪目录（管理员）

本脚本固化的口径(改它们 = 改契约):

1. **只有两层**:目录没有 ``parent_id``。再分下去,``--folder`` 就要写路径,
   而路径式分类会把"目录名"变成"路径片段"(重命名 = 全部文档换位置);
2. **未分类是合法的归属,不是错误**:空串 = 没有目录。磁盘上刚放进来、还没登记的文件
   只能是它(没有行可写),所以它恒定出现在列表末尾,而且不许画成告警;
3. **删目录不删文档**:目录里还有文档时回 409 并点出份数 —— 一次误点清空一整格是不可逆的;
4. **改名不改 ID**:``folder_id`` 是路由与 ``documents.folder`` 的键,变了就等于文档全脱钩;
5. **目录不承载可见范围**:授权仍按 ``document_id`` 逐份收窄,目录的计数只数**看得见的** —
   "这一格里有 3 份"与"你能看见 2 份"是两件事,混起来就会泄露份数;
6. **`--folder` 按名字解析**:命令行给的是人话,所以名字唯一、且规则只在
   ``domain/documents.check_folder_name`` 一处(空/超长/斜杠/控制字符/保留名全部当场驳回);
7. **老库一次性回填**:``documents`` 加 ``folder`` 列的那一次,按 ``source`` 把老行归进
   同名目录。**之后不再回填** —— 用户把文档移回未分类,重启不许被塞回去。

前半段用内存假件(不需要容器 / 模型 / 网络);迁移那两条**用真 MySQL**(连不上就 SKIP,
并点名 owner)。前端那半是静态契约(读源码)。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from _fakes import FakeVectorStore
from _harness import SkipCheck, check, report

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"

MYSQL_ENV = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "10093")),
    "user": os.environ.get("MYSQL_USER", "rag"),
    "password": os.environ.get("MYSQL_PASSWORD", "1234"),
    "database": os.environ.get("MYSQL_DATABASE", "rag"),
    "charset": "utf8mb4",
}


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _read(relative: str) -> str:
    """输入:相对 ``web/src/`` 的路径。行为:读它。输出:正文(不在就报错,不静默给空串)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— 目录这一层的界面还没落地?")
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """输入:一份前端源码。行为:去掉块注释与整行注释。输出:剩下的文本。"""
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", without_block)


# ── 假件 ─────────────────────────────────────────────────────────────────────
class _FakeDocs:
    """登记表 + 目录表的**一致性**内存替身(端口 ``DocumentStore`` 的完整形状)。

    刻意不做成 dict 直通:``set_folder`` 必须像真适配器一样"没这份文档就返回 0" ——
    否则"挪一份不存在的文档"这条契约会被假件顺手满足。
    """

    def __init__(self, rows=(), folders=()) -> None:
        self.rows: dict[str, Any] = {row.document_id: row for row in rows}
        self.folders: dict[str, Any] = {folder.folder_id: folder for folder in folders}

    def init_schema(self) -> None: ...

    def add(self, record) -> None:
        if record.document_id in self.rows:
            raise ValueError(f"文档 {record.document_id!r} 已登记过")
        self.rows[record.document_id] = record

    def upsert(self, record) -> None:
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
            folder=record.folder,
        )

    def remove(self, document_id) -> int:
        return 1 if self.rows.pop(document_id, None) is not None else 0

    # ── 目录 ──────────────────────────────────────────────────────────────
    def list_folders(self):
        return sorted(self.folders.values(), key=lambda item: item.name)

    def get_folder(self, folder_id):
        return self.folders.get(folder_id)

    def folder_named(self, name):
        return next((item for item in self.folders.values() if item.name == name), None)

    def add_folder(self, folder) -> None:
        if folder.folder_id in self.folders or self.folder_named(folder.name) is not None:
            raise ValueError(f"目录名 {folder.name!r} 已被占用")
        self.folders[folder.folder_id] = folder

    def update_folder(self, folder) -> None:
        other = self.folder_named(folder.name)
        if other is not None and other.folder_id != folder.folder_id:
            raise ValueError(f"目录名 {folder.name!r} 已被占用")
        self.folders[folder.folder_id] = folder

    def remove_folder(self, folder_id) -> int:
        return 1 if self.folders.pop(folder_id, None) is not None else 0

    def set_folder(self, document_id, folder_id) -> int:
        record = self.rows.get(document_id)
        if record is None:
            return 0
        self.rows[document_id] = type(record)(
            document_id=record.document_id,
            source=record.source,
            filename=record.filename,
            status=record.status,
            chunks=record.chunks,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            error=record.error,
            folder=folder_id,
        )
        return 1


class _FakeVectors(FakeVectorStore):
    """向量库的内存替身:**真的会增删块**(目录的份数/块数按它算)。

    继承共享假件是为了拿到**完整端口形状**(search / 稀疏那一路 / 指纹…):这一层只用到
    "按 ``doc_id`` 数块"与"删块",但 ``Deps`` 收的是端口类型 —— 少一个方法就是一处
    与本检查无关的 pyright 红。
    """

    def __init__(self, chunks=()) -> None:
        super().__init__()
        self._chunks = list(chunks)

    def chunk_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self._chunks:
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
        return counts

    def upsert(self, chunks, embeddings, *, sparse=None) -> int:
        self._chunks.extend(chunks)
        return len(self._chunks)

    def delete_by_doc_id(self, doc_id: str) -> int:
        before = len(self._chunks)
        self._chunks = [chunk for chunk in self._chunks if chunk.doc_id != doc_id]
        return before - len(self._chunks)

    def load_all(self):
        return list(self._chunks)


class _FakeAuth:
    """认证存储的内存替身(三个角色各一个号)。"""

    PASSWORDS = {"boss": "pw-123456", "worker": "pw-123456", "ops": "pw-123456"}

    def __init__(self) -> None:
        from project_rag.application.security import hash_password
        from project_rag.domain.auth import User

        self.users = {
            "boss": (User("u-boss", "boss", "admin"), hash_password(self.PASSWORDS["boss"])),
            "worker": (
                User("u-worker", "worker", "employee"),
                hash_password(self.PASSWORDS["worker"]),
            ),
            "ops": (User("u-ops", "ops", "ops"), hash_password(self.PASSWORDS["ops"])),
        }
        self.tokens: dict[str, tuple[str, float]] = {}

    def init_schema(self) -> None: ...

    def create_user(self, *args, **kwargs):  # pragma: no cover - 本脚本不用
        raise NotImplementedError

    def set_password(self, user_id, password_hash) -> None: ...

    def delete_user(self, user_id) -> int:  # pragma: no cover - 本脚本用不到
        return 0

    def revoke_all_tokens(self, user_id) -> int:  # pragma: no cover - 本脚本用不到
        return 0

    def list_users(self):
        return [user for user, _ in self.users.values()]

    def find_by_username(self, username):
        return self.users.get(username)

    def issue_token(self, user, token_hash, ttl_seconds) -> None:
        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash):
        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next(user for user, _ in self.users.values() if user.user_id == item[0])

    def revoke_token(self, token_hash) -> None:
        self.tokens.pop(token_hash, None)

    def bind_session(self, session_id, user_id) -> None: ...

    def owner_of(self, session_id):
        return None


class _FakeAccess:
    """可见范围的内存替身:只用来验"目录的份数按看得见的算"。"""

    def __init__(self, rules: dict[str, tuple[list[str], list[str]]] | None = None) -> None:
        self._rules = rules or {}

    def init_schema(self) -> None: ...

    def create_group(self, name):  # pragma: no cover - 本脚本不用
        raise NotImplementedError

    def list_groups(self):
        return []

    def delete_group(self, group_id) -> int:
        return 0

    def set_members(self, group_id, user_ids) -> None: ...

    def members_of(self, group_id):
        return ()

    def groups_by_user(self):
        return {}

    def set_document_subjects(self, document_id, *, user_ids, group_ids) -> None: ...

    def subjects_of(self, document_id):
        return self.rules().get(document_id)

    def rules(self):
        from project_rag.domain.access import DocumentAcl

        return {
            document_id: DocumentAcl(
                document_id=document_id, user_ids=tuple(users), group_ids=tuple(groups)
            )
            for document_id, (users, groups) in self._rules.items()
        }

    def forget_document(self, document_id) -> int:
        return 0

    def forget_user(self, user_id) -> int:
        return 0


# ── 造数据 / 造客户端 ────────────────────────────────────────────────────────
def _record(
    document_id: str,
    *,
    folder: str = "",
    status: str = "processed",
    chunks: int = 3,
    size_bytes: int = 1000,
    source: str = "uploads",
):
    """输入:文档 ID 与几个字段。行为:造一条登记记录。输出:``DocRecord``。"""
    from project_rag.domain import DocRecord

    return DocRecord(
        document_id=document_id,
        source=source,
        filename=Path(document_id).name,
        status=status,  # type: ignore[arg-type]
        chunks=chunks,
        size_bytes=size_bytes,
        created_at="2026-10-01T10:00:00",
        folder=folder,
    )


def _folder(folder_id: str, name: str, description: str = ""):
    """输入:目录 ID / 名字 / 说明。行为:造一个目录。输出:``Folder``。"""
    from project_rag.domain import Folder

    return Folder(
        folder_id=folder_id,
        name=name,
        description=description,
        created_at="2026-10-01T09:00:00",
        updated_at="2026-10-01T09:00:00",
    )


def _chunk(doc_id: str, index: int = 0):
    """输入:文档 ID 与序号。行为:造一个子块。输出:``Chunk``。"""
    from project_rag.domain import Chunk

    return Chunk(f"c{doc_id}-{index}", "正文", "uploads", doc_id, "", "")


def _disk(doc_id: str, *, size: int = 1000, source: str = "uploads"):
    """输入:文档 ID 与磁盘读数。行为:造一条 ``DiskFile``(不碰真磁盘)。输出:``DiskFile``。"""
    from project_rag.adapters.document_loader import DiskFile

    return DiskFile(
        path=Path("/tmp") / doc_id, size_bytes=size, source=source, doc_ids=(doc_id,)
    )


def _client(
    *,
    docs=None,
    folders=(),
    chunks=(),
    files=(),
    auth=False,
    access: dict[str, tuple[list[str], list[str]]] | None = None,
):
    """输入:登记行 / 目录 / 库内块 / 磁盘文件 / 要不要认证 / 可见范围规则。行为:建 TestClient。输出:client。"""
    from fastapi.testclient import TestClient

    from project_rag.api import create_app
    from project_rag.cli import Deps

    store = docs if docs is not None else _FakeDocs([], folders)
    vectors = _FakeVectors(chunks)
    fields: dict[str, Any] = {
        "document_store_factory": lambda: store,
        "vector_store_factory": lambda: vectors,
        "disk_files_factory": lambda: tuple(files),
        # 上传那条路要先读后缀白名单(它在收字节**之前**),缺了会 503 而不是验目录
        "suffixes_factory": lambda: (".md", ".pdf", ".txt"),
    }
    if auth:
        fields["auth_store_factory"] = lambda: _FakeAuth()
    if access is not None:
        fields["access_store_factory"] = lambda: _FakeAccess(access)
    return TestClient(create_app(Deps(**fields)))


def _login(client, username: str = "boss") -> dict[str, str]:
    """输入:客户端、用户名。行为:走真登录接口。输出:带令牌的请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _FakeAuth.PASSWORDS[username]},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _folders_of(client, headers=None) -> dict:
    """输入:客户端与可选请求头。行为:调目录清单。输出:JSON 正文。"""
    response = client.get("/api/v1/folders", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _row(body: dict, folder_id: str) -> dict:
    """输入:目录清单正文与目录 ID。行为:取那一格。输出:该行(取不到就抛,带上全部 ID)。"""
    for row in body["folders"]:
        if row["folder_id"] == folder_id:
            return row
    raise AssertionError(f"清单里没有 {folder_id!r},只有 {[row['folder_id'] for row in body['folders']]}")


# ══ 一、领域:ID 形状与名字规则 ═══════════════════════════════════════════════
@check("目录 ID：形状恒为 f_ + 12 位十六进制，永远不可能等于保留 ID `unfiled`")
def _id_shape() -> str:
    """保留 ID 与生成 ID 必须在**形状上**就分得开 —— 靠"调用方小心"迟早撞一次。"""
    from project_rag.domain import UNFILED_ID
    from project_rag.domain import new_folder_id

    pattern = re.compile(r"^f_[0-9a-f]{12}$")
    sample = {new_folder_id() for _ in range(500)}
    bad = [value for value in sample if not pattern.match(value)]
    assert not bad, f"生成的目录 ID 形状不对:{bad[:3]}"
    assert UNFILED_ID not in sample, "生成了一个等于保留 ID 的目录 ID"
    assert len(sample) == 500, "500 次生成出现了重复 —— 碰撞概率不该这么高"
    return f"500 个 ID 全部匹配 {pattern.pattern};`{UNFILED_ID}` 不在其中"


@check("目录名规则：空 / 超长 / 保留名 / 斜杠 / 控制字符一律驳回，正常名字去首尾空白")
def _name_rules() -> str:
    """名字是唯一一个"人给、人看、还要当命令行参数"的标识(``ingest --folder 名字``)。"""
    from project_rag.domain import FOLDER_NAME_MAX, UNFILED_NAME
    from project_rag.domain import check_folder_name

    for bad in ("   ", "", "x" * (FOLDER_NAME_MAX + 1), UNFILED_NAME, "a/b", "a\\b", "a\nb"):
        try:
            check_folder_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 该被驳回")
    assert check_folder_name("  产品资料  ") == "产品资料", "首尾空白没有去掉"
    assert check_folder_name("x" * FOLDER_NAME_MAX) == "x" * FOLDER_NAME_MAX, "上界值本身要能过"
    return f"7 种非法名全驳回;`  产品资料  ` → `产品资料`;{FOLDER_NAME_MAX} 字可用"


@check("登记行：`folder` 默认是未分类（老调用点一个字都不用改）")
def _record_default() -> str:
    from project_rag.domain import UNFILED, DocRecord

    record = DocRecord(
        document_id="a.md", source="uploads", filename="a.md", status="processed",
        chunks=1, size_bytes=1, created_at="t",
    )
    assert record.folder == UNFILED == "", f"默认归属该是未分类(空串),实际 {record.folder!r}"
    return "DocRecord(...) 不传 folder → 空串(未分类),老代码零改动"


# ══ 二、HTTP:目录的增删改查 ══════════════════════════════════════════════════
@check("建目录：201、计数为 0、名字去空白；重名 409；非法名 400（三种错三种下一步）")
def _create() -> str:
    client = _client()
    created = client.post("/api/v1/folders", json={"name": "  公司制度 ", "description": "员工手册"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "公司制度", f"名字没去空白:{body['name']!r}"
    assert body["description"] == "员工手册" and body["documents"] == 0, body
    assert re.match(r"^f_[0-9a-f]{12}$", body["folder_id"]), body["folder_id"]

    duplicate = client.post("/api/v1/folders", json={"name": "公司制度"})
    assert duplicate.status_code == 409, f"重名该 409,实际 {duplicate.status_code}:{duplicate.text}"
    assert "已存在" in duplicate.json()["detail"], duplicate.json()

    for bad, expected in (("", 400), ("未分类", 400), ("a/b", 400)):
        response = client.post("/api/v1/folders", json={"name": bad})
        assert response.status_code == expected, f"{bad!r} 该 {expected},实际 {response.status_code}"

    listed = _folders_of(client)
    assert [row["name"] for row in listed["folders"]] == ["公司制度"], listed["folders"]
    return "201 + 0 份;重名 409;空名/保留名/斜杠 400;清单里只有一个目录"


@check("目录清单：份数/块数/占用/需处理 都是**对账后**的读数；未分类恒定在最后")
def _index() -> str:
    docs = _FakeDocs(
        [
            _record("handbook.md", folder="f_aaa", chunks=3, size_bytes=100),
            _record("policy.md", folder="f_aaa", chunks=5, size_bytes=200),
            _record("loose.md", folder="", chunks=1, size_bytes=50),
        ],
        [_folder("f_aaa", "公司制度", "手册与政策"), _folder("f_bbb", "空目录")],
    )
    chunks = [
        *(_chunk("handbook.md", i) for i in range(3)),
        *(_chunk("policy.md", i) for i in range(5)),
        _chunk("loose.md"),
    ]
    files = [
        _disk("handbook.md", size=100),
        _disk("policy.md", size=200),
        _disk("loose.md", size=50),
    ]
    body = _folders_of(_client(docs=docs, chunks=chunks, files=files))

    assert [row["name"] for row in body["folders"]] == ["公司制度", "空目录"], body["folders"]
    handbook = _row(body, "f_aaa")
    assert handbook["documents"] == 2, handbook
    assert handbook["chunks"] == 8, f"块数该是库内实际 8,实际 {handbook['chunks']}"
    assert handbook["size_bytes"] == 300, handbook
    assert handbook["issues"] == 0, f"三处一致的行不该记进「需要处理」:{handbook}"
    assert handbook["last_activity"] == "2026-10-01T10:00:00", handbook

    empty = _row(body, "f_bbb")
    assert empty["documents"] == 0 and empty["last_activity"] == "", empty

    unfiled = body["unfiled"]
    assert unfiled["unfiled"] is True and unfiled["name"] == "未分类", unfiled
    assert unfiled["folder_id"] == "unfiled", "未分类对外的 ID 应该是 `unfiled`"
    assert unfiled["documents"] == 1 and unfiled["chunks"] == 1, unfiled
    assert body["summary"] == {"folders": 2, "used": 1, "unfiled": 1}, body["summary"]
    return "公司制度 2 份 / 8 块 / 300 字节;空目录 0;未分类 1 份且 unfiled=true;汇总 2/1/1"


@check("目录清单：库内没有块 → 计入「需要处理」，而不是记成 0 块就完事")
def _issues() -> str:
    docs = _FakeDocs(
        [_record("ghost.md", folder="f_aaa", chunks=677), _record("ok.md", folder="f_aaa", chunks=2)],
        [_folder("f_aaa", "公司制度")],
    )
    files = [_disk("ghost.md"), _disk("ok.md")]
    chunks = [_chunk("ok.md", 0), _chunk("ok.md", 1)]
    row = _row(_folders_of(_client(docs=docs, chunks=chunks, files=files)), "f_aaa")
    assert row["issues"] == 1, f"集合被清空的那一份该计入需要处理,实际 {row}"
    assert row["chunks"] == 2, f"块数按库内实际算(2),不是登记表说的 679:{row['chunks']}"
    return "登记 677 块、库内 0 块 → issues=1;目录的块数按库内实际(2)"


@check("目录详情：只含这一格的文档；`unfiled` 可读；不存在的 404 且话术给出下一步")
def _detail() -> str:
    docs = _FakeDocs(
        [
            _record("handbook.md", folder="f_aaa"),
            _record("loose.md", folder=""),
        ],
        [_folder("f_aaa", "公司制度")],
    )
    files = [_disk("handbook.md"), _disk("loose.md")]
    chunks = [_chunk("handbook.md"), _chunk("loose.md")]
    client = _client(docs=docs, chunks=chunks, files=files)

    detail = client.get("/api/v1/folders/f_aaa")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["folder"]["name"] == "公司制度", body["folder"]
    assert [row["document_id"] for row in body["documents"]] == ["handbook.md"], body["documents"]
    assert body["summary"]["total"] == 1, body["summary"]
    assert body["documents"][0]["folder"] == "f_aaa", "每行要带归属(前端不必自己再推一遍)"

    unfiled = client.get("/api/v1/folders/unfiled")
    assert unfiled.status_code == 200, unfiled.text
    assert [row["document_id"] for row in unfiled.json()["documents"]] == ["loose.md"], unfiled.text

    missing = client.get("/api/v1/folders/f_nope")
    assert missing.status_code == 404, missing.text
    assert "GET /api/v1/folders" in missing.json()["detail"], "404 的话术没有指一条能走的路"
    return "详情只给那一格(1 份);unfiled 可读;不存在的 404 + 指路话术"


@check("改名：ID 不变、文档不掉、重名 409、`unfiled` 400")
def _rename() -> str:
    docs = _FakeDocs([_record("a.md", folder="f_aaa")], [_folder("f_aaa", "旧名字")])
    client = _client(docs=docs, chunks=[_chunk("a.md")], files=[_disk("a.md")])
    response = client.patch("/api/v1/folders/f_aaa", json={"name": "新名字", "description": "新说明"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["folder_id"] == "f_aaa", f"改名把 ID 也改了:{body}"
    assert body["name"] == "新名字" and body["description"] == "新说明", body
    assert body["documents"] == 1, f"改名之后目录里的文档不见了:{body}"
    assert docs.rows["a.md"].folder == "f_aaa", "改名把文档的归属改了 —— 那等于把它们全脱钩"

    client.post("/api/v1/folders", json={"name": "另一个"})
    conflict = client.patch("/api/v1/folders/f_aaa", json={"name": "另一个"})
    assert conflict.status_code == 409, f"改成已有的名字该 409,实际 {conflict.status_code}"

    virtual = client.patch("/api/v1/folders/unfiled", json={"name": "x"})
    assert virtual.status_code == 400, f"虚拟目录不给改名,实际 {virtual.status_code}"
    return "ID 不变 / 名字说明都改了 / 文档还在;重名 409;unfiled 400"


@check("删目录：非空 409 且点出份数；空的删掉；不存在 deleted=0；`unfiled` 400")
def _delete() -> str:
    docs = _FakeDocs(
        [_record("a.md", folder="f_busy")],
        [_folder("f_busy", "有文档"), _folder("f_empty", "空的")],
    )
    client = _client(docs=docs, chunks=[_chunk("a.md")], files=[_disk("a.md")])

    busy = client.delete("/api/v1/folders/f_busy")
    assert busy.status_code == 409, f"非空目录该 409,实际 {busy.status_code}:{busy.text}"
    assert "1 份" in busy.json()["detail"], f"话术要点出份数:{busy.json()}"
    assert "f_busy" in docs.folders, "被拒绝的删除却真的删了"
    assert "a.md" in docs.rows, "删目录把文档也删了 —— 删目录不删文档"

    empty = client.delete("/api/v1/folders/f_empty")
    assert empty.status_code == 200 and empty.json()["deleted"] == 1, empty.text
    assert "f_empty" not in docs.folders, "空的目录没删掉"

    again = client.delete("/api/v1/folders/f_empty")
    assert again.status_code == 200 and again.json()["deleted"] == 0, "删除要幂等(重复点不该报错)"

    virtual = client.delete("/api/v1/folders/unfiled")
    assert virtual.status_code == 400, f"虚拟目录不给删,实际 {virtual.status_code}"
    return "非空 409 + 点名 1 份;空目录删掉;再删 deleted=0;unfiled 400"


@check("挪文档：只改归属（其它字段一个不动）；目标不存在 404；未登记的文件 404")
def _move() -> str:
    docs = _FakeDocs(
        [_record("a.md", folder="f_one"), _record("b.md", folder="f_one")],
        [_folder("f_one", "来源"), _folder("f_two", "目标")],
    )
    client = _client(docs=docs, chunks=[_chunk("a.md"), _chunk("b.md")],
                     files=[_disk("a.md"), _disk("b.md")])

    moved = client.patch("/api/v1/documents/a.md", json={"folder_id": "f_two"})
    assert moved.status_code == 200, moved.text
    assert moved.json() == {"document_id": "a.md", "folder": "f_two"}, moved.json()
    assert docs.rows["a.md"].folder == "f_two", "归属没改"
    assert docs.rows["a.md"].chunks == 3 and docs.rows["a.md"].status == "processed", (
        "挪个目录把块数或状态改了 —— 挪动只该动归属"
    )
    assert docs.rows["b.md"].folder == "f_one", "把同一目录里的另一份也挪了"

    back = client.patch("/api/v1/documents/a.md", json={"folder_id": ""})
    assert back.status_code == 200 and docs.rows["a.md"].folder == "", "挪回未分类该用空串"

    missing_folder = client.patch("/api/v1/documents/b.md", json={"folder_id": "f_nope"})
    assert missing_folder.status_code == 404, missing_folder.text
    missing_doc = client.patch("/api/v1/documents/nope.md", json={"folder_id": "f_two"})
    assert missing_doc.status_code == 404, f"没这份文档该 404,实际 {missing_doc.status_code}"
    return "归属改了、块数/状态不动、同目录其它行不动;挪回未分类 = 空串;两种 404 分开报"


@check("权限：读写不是一个门 —— 员工/运维能读目录，建/改名/删/挪一律 403")
def _roles() -> str:
    """目录是**组织方式**,读它只要求「知识库」这一档;改它是管理动作。

    与文档同一条线(``require_knowledge`` 读 / ``require_admin`` 写):把读也收到 admin,
    员工就说不清"我能检索的东西分在哪几格"。
    """
    docs = _FakeDocs([_record("a.md", folder="f_aaa")], [_folder("f_aaa", "公司制度")])
    client = _client(docs=docs, chunks=[_chunk("a.md")], files=[_disk("a.md")], auth=True)

    assert client.get("/api/v1/folders").status_code == 401, "没有令牌竟然能读目录"
    for who in ("worker", "ops"):
        headers = _login(client, who)
        assert client.get("/api/v1/folders", headers=headers).status_code == 200, f"{who} 该能读目录"
        assert client.get("/api/v1/folders/f_aaa", headers=headers).status_code == 200
        writes = {
            "create": client.post("/api/v1/folders", headers=headers, json={"name": "x"}),
            "rename": client.patch("/api/v1/folders/f_aaa", headers=headers, json={"name": "y"}),
            "delete": client.delete("/api/v1/folders/f_aaa", headers=headers),
            "move": client.patch(
                "/api/v1/documents/a.md", headers=headers, json={"folder_id": "f_aaa"}
            ),
        }
        leaked = {name: r.status_code for name, r in writes.items() if r.status_code != 403}
        assert not leaked, f"{who} 不该能改目录,实际 {leaked}"

    admin = _login(client)
    assert client.post("/api/v1/folders", headers=admin, json={"name": "管理员建的"}).status_code == 201
    return "无令牌 401;员工/运维读 200、四种写全 403;管理员能建"


@check("可见范围：看不见的文档不计入目录份数（份数不是「这一格有几份」而是「你能看见几份」）")
def _acl() -> str:
    """目录不承载权限,但**计数要按看得见的算** —— 否则"公司制度有 3 份"这句话就在泄露
    "还有 1 份你看不到"。
    """
    docs = _FakeDocs(
        [_record("公开.md", folder="f_aaa"), _record("机密.md", folder="f_aaa")],
        [_folder("f_aaa", "公司制度")],
    )
    client = _client(
        docs=docs,
        chunks=[_chunk("公开.md"), _chunk("机密.md")],
        files=[_disk("公开.md"), _disk("机密.md")],
        auth=True,
        access={"机密.md": ([], ["g-finance"])},
    )
    employee = _login(client, "worker")
    body = _folders_of(client, employee)
    assert _row(body, "f_aaa")["documents"] == 1, (
        f"员工该只数到 1 份(机密那份看不见),实际 {_row(body, 'f_aaa')['documents']}"
    )
    detail = client.get("/api/v1/folders/f_aaa", headers=employee).json()
    assert [row["document_id"] for row in detail["documents"]] == ["公开.md"], detail["documents"]

    admin = _folders_of(client, _login(client))
    assert _row(admin, "f_aaa")["documents"] == 2, "管理员该数到 2 份"
    assert _row(admin, "f_aaa")["restricted"] == 1, "收窄过的份数要如实标注"
    return "员工数到 1 / 详情 1 行;管理员数到 2 且 restricted=1"


@check("上传落点：目录不存在 → 404 且**一个字节都不落盘**（先验目录,再收字节）")
def _upload_target() -> str:
    """先收字节再验目录的写法会把文件落进上传目录,然后回一个 404 ——
    那份没人登记的文件会以「未入库」出现在清单里,用户只能自己去磁盘上删。
    """
    with tempfile.TemporaryDirectory() as raw:
        uploads = Path(raw)
        from project_rag.api import app as api_app

        original = api_app._uploads_dir
        api_app._uploads_dir = lambda: uploads  # type: ignore[assignment]
        try:
            client = _client(auth=True)
            headers = _login(client)
            response = client.post(
                "/api/v1/documents?filename=a.md&folder=f_nope",
                headers={**headers, "Content-Type": "application/octet-stream"},
                content=b"hello",
            )
        finally:
            api_app._uploads_dir = original  # type: ignore[assignment]
        assert response.status_code == 404, f"目录不存在该 404,实际 {response.status_code}:{response.text}"
        assert list(uploads.iterdir()) == [], f"404 之后上传目录里多了文件:{list(uploads.iterdir())}"
    return "404;上传目录保持空(没有半截文件、也没有孤儿文件)"


@check("上传：同名文件已在**另一个目录** → 409（文档 ID 就是文件名，两格不能各有一份）")
def _upload_conflict() -> str:
    """同名重传在**同一目录**里是覆盖(正常用法);换一个目录就是两个 ID 撞在一起。

    撞了会怎样:块以 ``doc_id`` 为键,删块/挪目录/幂等清理都用它 —— 两个目录各有一份同名文件时,
    重灌或删除会**互相删对方的块**,而且是静默的。所以这一条必须在收字节之前挡住。
    """
    docs = _FakeDocs(
        [_record("手册.md", folder="f_one")],
        [_folder("f_one", "公司制度"), _folder("f_two", "产品资料")],
    )
    with tempfile.TemporaryDirectory() as raw:
        uploads = Path(raw)
        from project_rag.api import app as api_app

        original = api_app._uploads_dir
        api_app._uploads_dir = lambda: uploads  # type: ignore[assignment]
        try:
            client = _client(docs=docs, chunks=[_chunk("手册.md")], files=[_disk("手册.md")], auth=True)
            headers = _login(client)
            response = client.post(
                "/api/v1/documents?filename=手册.md&folder=f_two",
                headers={**headers, "Content-Type": "application/octet-stream"},
                content=b"hello",
            )
        finally:
            api_app._uploads_dir = original  # type: ignore[assignment]
        # 临时目录在 `with` 里读:出了这个块它就被删了(第一版把断言写在块外,报 FileNotFoundError)
        leftovers = list(uploads.iterdir())
    assert response.status_code == 409, f"同名换目录该 409,实际 {response.status_code}:{response.text}"
    detail = response.json()["detail"]
    assert "公司制度" in detail, f"话术要点名那一份在哪个目录:{detail}"
    assert "改名" in detail and "删掉" in detail, f"话术要给两条能走的路:{detail}"
    assert leftovers == [], f"409 之后上传目录里多了文件:{leftovers}"
    assert docs.rows["手册.md"].folder == "f_one", "被拒绝的上传改了那一份的归属"
    return "409 + 点名「公司制度」+ 两条出路;上传目录保持空;原文档归属没动"


# ══ 三、CLI：入库时指定目录 ═══════════════════════════════════════════════════
def _run_ingest(path: Path, *extra: str):
    """输入:要灌的目录 + 额外参数。行为:用假依赖走一遍真 CLI ingest。输出:(退出码, 登记表替身)。"""
    import argparse

    from project_rag.adapters import HashingEmbedder
    from project_rag.cli import Deps
    from project_rag.cli.commands import ingest as ingest_cmd

    docs = _FakeDocs()
    vectors = _FakeVectors()
    parser = argparse.ArgumentParser()
    ingest_cmd.add_parser(parser.add_subparsers())
    args = parser.parse_args([ingest_cmd.NAME, "--path", str(path), *extra])
    args.deps = Deps(
        embedder_factory=HashingEmbedder,
        vector_store_factory=lambda: vectors,
        document_store_factory=lambda: docs,
    )
    return ingest_cmd.run(args), docs


@check("CLI：`--folder` 显式建目录；不给时**语料之外的路径落未分类**（不按来源偷偷建格）")
def _ingest_folder() -> str:
    """默认落点从"来源标签"改成了"文件在语料根下的一级目录"。

    语料之外的路径(上传目录、临时目录、别人给的压缩包解开的地方)**没有一级目录** ——
    老规则会按来源建一个同名格子,于是界面上冒出 `uploads`、`corpus` 这种"根本不是一个分类"
    的目录。那些文档现在落未分类,由人决定放进哪一格。
    """
    with tempfile.TemporaryDirectory() as raw:
        # 目录名**刻意不叫 bagu_ai_data**:叫了就会与本机真实语料里的那格撞名,
        # 这条检查就变成了在验"碰巧同名"。
        root = Path(raw) / "package_data"
        root.mkdir(parents=True)
        (root / "x.md").write_text("装饰器有什么用?" * 200, encoding="utf-8")

        code, docs = _run_ingest(root, "--folder", "产品资料")
        assert code == 0, f"ingest 该成功,实际退出码 {code}"
        created = docs.folder_named("产品资料")
        assert created is not None, f"目录没建成,现在是 {[f.name for f in docs.list_folders()]}"
        assert docs.rows["x.md"].folder == created.folder_id, (
            f"文档没有挂进指定目录:{docs.rows['x.md'].folder!r}"
        )

        code, docs = _run_ingest(root)
        assert code == 0, f"不给 --folder 也该成功,实际退出码 {code}"
        assert docs.rows["x.md"].folder == "", (
            f"语料之外的路径该落未分类,实际落进 {docs.rows['x.md'].folder!r}"
        )
        assert docs.folder_named("package_data") is None, "又按来源偷偷建了一个目录"
    return "`--folder 产品资料` → 建成并挂进去;不给 → 未分类(没有凭空出现的格)"


@check("目录名口径：来源是语料根下的一级目录才成一格；语料根本身/上传目录都不算")
def _corpus_source_mapping() -> str:
    """判据只用"目录还在不在" —— 来源标签本来就是语料顶层目录名的口径。

    这一条挡住了两种"根本不是一个分类"的格子:``corpus``(所有格的父亲)与
    ``uploads``(上传目录,不在语料根下)。
    """
    from project_rag.application.folders import corpus_folder_of_source

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        (corpus / "bagu_ai_data").mkdir(parents=True)
        (corpus / "doc").mkdir()
        assert corpus_folder_of_source(corpus, "bagu_ai") == "bagu_ai", "带 _data 的目录没认出来"
        assert corpus_folder_of_source(corpus, "doc") == "doc", "不带 _data 的目录没认出来"
        assert corpus_folder_of_source(corpus, "corpus") is None, "语料根本身不该是一格"
        assert corpus_folder_of_source(corpus, "uploads") is None, "语料之外的来源不该是一格"
        assert corpus_folder_of_source(corpus, "") is None, "空来源不该是一格"
    return "bagu_ai / doc 成一格;corpus / uploads / 空 → None"


@check("语料目录同步：每个一级子目录一格，语料根本身不建；重复同步不重复建")
def _sync_folders() -> str:
    """``data/corpus`` 下有哪几格,界面上就该有哪几格 —— 而 ``corpus`` 自己是父亲,不是一格。"""
    from project_rag.application.folders import sync_corpus_folders

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        for name in ("bagu_ai_data", "doc_data", "pdf_data"):
            (corpus / name).mkdir(parents=True)
        (corpus / "散落的一份.md").write_text("根下的文件", encoding="utf-8")

        store = _FakeDocs()
        first = sync_corpus_folders(store, corpus)
        names = sorted(folder.name for folder in first)
        assert names == ["bagu_ai", "doc", "pdf"], f"同步出来的格子不对:{names}"
        assert store.folder_named("corpus") is None, "语料根本身被当成一格了"
        assert "散落的一份" not in names, "语料根下的文件被当成目录了"

        again = sync_corpus_folders(store, corpus)
        assert len(store.list_folders()) == 3, "重复同步又建了一遍"
        assert [folder.folder_id for folder in again] == [
            folder.folder_id for folder in first
        ], "重复同步换了 ID"
    return "3 个子目录 → 3 格(bagu_ai/doc/pdf);语料根与根下文件都不成格;二次同步幂等"


@check("登记根：同一个文件从语料根或子目录灌，doc_id 与 source 相同（两种入口之间也幂等）")
def _registration_anchor() -> str:
    """``--path data/corpus`` 与 ``--path data/corpus/bagu_ai_data`` 必须给出同一份文档。

    不然同一份文件会有两个 ``doc_id``、两批 ``chunk_id``(source 也不同)—— 库里两份,
    检索时互相竞争,而界面上看是"两个同名文件"。
    """
    from project_rag.adapters.document_loader import load_documents

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        target = corpus / "bagu_ai_data" / "x.md"
        target.parent.mkdir(parents=True)
        target.write_text("正文" * 50, encoding="utf-8")

        from_root = load_documents(corpus, corpus_root=corpus).documents
        from_subdir = load_documents(corpus / "bagu_ai_data", corpus_root=corpus).documents
        assert [doc.doc_id for doc in from_root] == ["x.md"], [doc.doc_id for doc in from_root]
        assert [doc.doc_id for doc in from_subdir] == ["x.md"], [doc.doc_id for doc in from_subdir]
        assert [doc.source for doc in from_root] == ["bagu_ai"], [doc.source for doc in from_root]

        # 不给语料根时保持老口径(相对本次的 path):这是给"语料之外"的路径留的后路
        legacy = load_documents(corpus).documents
        assert [doc.doc_id for doc in legacy] == ["bagu_ai_data/x.md"], [doc.doc_id for doc in legacy]
        assert [doc.source for doc in legacy] == ["corpus"], [doc.source for doc in legacy]
    return "从语料根/子目录灌都是 x.md + source=bagu_ai;不给语料根时保持老口径"


@check("CLI：灌整个语料根时按子目录分发（语料根不是一格）；根下的散文件落未分类")
def _ingest_corpus_tree() -> str:
    """这是作者那张截图里的那个 bug:灌过一次语料根,界面上多出一格 `corpus`。

    正确行为:里面每份文档各回各家,``corpus`` 这一格不该出现;直接躺在语料根下的文件
    没有一级目录,落未分类。
    """
    from project_rag.application.folders import corpus_folder_of_source  # noqa: F401 - 文档用
    from project_rag.config import get_settings

    from _harness import env

    with tempfile.TemporaryDirectory() as raw:
        corpus = Path(raw) / "corpus"
        (corpus / "bagu_ai_data").mkdir(parents=True)
        (corpus / "bagu_ai_data" / "x.md").write_text("装饰器有什么用?" * 120, encoding="utf-8")
        (corpus / "top.md").write_text("直接躺在语料根下的一份", encoding="utf-8")

        try:
            with env(RAG_API__CORPUS_DIR=str(corpus)):
                get_settings.cache_clear()  # 配置是 lru_cache:换了环境变量必须清一次
                code, docs = _run_ingest(corpus)
        finally:
            get_settings.cache_clear()
        assert code == 0, f"ingest 该成功,实际退出码 {code}"
        rows = docs.rows
        bagu = docs.folder_named("bagu_ai")
        assert bagu is not None, f"子目录那一格没建出来:{[f.name for f in docs.list_folders()]}"
        assert rows["x.md"].folder == bagu.folder_id, "子目录里的文档没进对应那一格"
        assert rows["top.md"].folder == "", "语料根下的散文件该落未分类"
        assert docs.folder_named("corpus") is None, "语料根被建成了一格(就是那个 bug)"
        assert docs.folder_named("bagu_ai_data") is None, "目录名没去掉 _data 后缀"
    return "x.md → bagu_ai;top.md → 未分类;没有 corpus / bagu_ai_data 这两格"


# ══ 四、真 MySQL：加列 + 一次性回填 ═══════════════════════════════════════════
def _mysql_connect(autocommit: bool = True):
    """输入:是否自动提交。行为:连真 MySQL。输出:连接;连不上抛 SkipCheck。"""
    import pymysql

    target = f"{MYSQL_ENV['host']}:{MYSQL_ENV['port']}"
    try:
        return pymysql.connect(connect_timeout=5, autocommit=autocommit, **MYSQL_ENV)
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题,不是契约失败
        raise SkipCheck(
            f"MySQL 连不上({target}):{type(exc).__name__}: {exc};"
            "先看 Windows 侧 Docker Desktop 是否开着,或跑 check_env_services.py 定位"
        ) from exc


# 与 ``adapters/documents_mysql.py`` 的 ``_DDL`` **迁移前**那一版是同一份契约(没有 folder 列)
OLD_DOCUMENTS_DDL = (
    "CREATE TABLE {table} ("
    "document_id VARCHAR(64) PRIMARY KEY, "
    "source VARCHAR(64) NOT NULL, "
    "filename VARCHAR(255) NOT NULL, "
    "status VARCHAR(16) NOT NULL, "
    "chunks INT NOT NULL DEFAULT 0, "
    "size_bytes BIGINT NOT NULL DEFAULT 0, "
    "created_at VARCHAR(32) NOT NULL, "
    "error VARCHAR(512) NULL, "
    "KEY idx_documents_created (created_at)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


def _probe_store():
    """输入:无。行为:在**临时表**上造一个登记适配器(不碰真的 documents 表)。输出:(store, 表名, 目录表名)。"""
    from project_rag.adapters.documents_mysql import MySQLDocumentStore
    from project_rag.config import get_settings

    suffix = uuid.uuid4().hex[:8]
    table, folders = f"documents_m7_5_{suffix}", f"folders_m7_5_{suffix}"
    store = MySQLDocumentStore(get_settings().mysql, table, folders)
    return store, table, folders


def _drop(*tables: str) -> None:
    """输入:表名。行为:删掉(**测试自己的临时表**)。输出:无。"""
    try:
        conn = _mysql_connect()
    except SkipCheck:
        return
    try:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        conn.close()


@check("迁移：老表补 `folder` 列并按来源回填；**只回填一次**（移回未分类后重启不许被塞回去）")
def _migration() -> str:
    """这一条是"改造数据库内容"那句话的可执行版本。

    ``ADD COLUMN IF NOT EXISTS`` 在 MySQL 8 上不存在,所以补齐靠 information_schema 先问;
    而"这次是不是新加的列"正好就是"要不要回填老行"的判据 —— 用"凡是 folder 为空就塞回去"
    会有一个静默的坏处:用户把文档移回未分类,重启一次又被塞回原目录。
    """
    store, table, folders = _probe_store()
    try:
        conn = _mysql_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(OLD_DOCUMENTS_DDL.format(table=table))
                cur.execute(
                    f"INSERT INTO {table} (document_id, source, filename, status, chunks,"
                    " size_bytes, created_at) VALUES"
                    " ('a.pdf', 'pdf', 'a.pdf', 'processed', 5, 100, 't1'),"
                    " ('b.md', 'bagu_ai', 'b.md', 'processed', 2, 50, 't2')"
                )
        finally:
            conn.close()

        store.init_schema()
        names = {folder.name for folder in store.list_folders()}
        assert names == {"pdf", "bagu_ai"}, f"老数据该按来源建出两个目录,实际 {names}"
        rows = {row.document_id: row for row in store.list_all()}
        assert rows["a.pdf"].folder and rows["b.md"].folder, "老行没有回填归属"
        assert rows["a.pdf"].folder != rows["b.md"].folder, "两份来源不同的文档被归进了同一格"

        # 移回未分类,再跑一次启动路径 —— **不许**被重新塞回原目录
        assert store.set_folder("a.pdf", "") == 1
        store.init_schema()
        moved = store.get("a.pdf")
        assert moved is not None, "初始化之后登记行没了"
        assert moved.folder == "", "重启又把用户移回未分类的文档塞回了原目录"
        return "老表补列 + 按来源建 2 个目录;移回未分类后二次 init 仍留在未分类"
    finally:
        _drop(table, folders)


@check("迁移：目录名在库里唯一（两条 SQL 都撞同一个约束，命令行按名字解析才可靠）")
def _unique_name() -> str:
    store, table, folders = _probe_store()
    try:
        store.init_schema()
        from project_rag.domain import Folder

        store.add_folder(
            Folder("f_000000000001", "公司制度", "", "2026-10-01T09:00:00", "2026-10-01T09:00:00")
        )
        try:
            store.add_folder(
                Folder("f_000000000002", "公司制度", "", "2026-10-01T09:00:00", "2026-10-01T09:00:00")
            )
        except ValueError:
            pass
        else:
            raise AssertionError("同名目录插进去了 —— `--folder 公司制度` 会变成掷硬币")
        found = store.folder_named("公司制度")
        assert found is not None and found.folder_id == "f_000000000001", "按名字查回来的不是第一行"
    finally:
        _drop(table, folders)
    return "第二行同名 → ValueError;按名字查回第一行"


# ══ 五、前端（静态契约） ═════════════════════════════════════════════════════
@check("前端：路由有 /knowledge/:folder；一级页渲染目录卡,二级页是文档表格")
def _frontend_pages() -> str:
    router = _read("router.ts")
    assert "path: '/knowledge/:folder'" in router, "没有二级页面的路由"
    assert "FolderView" in router, "路由里没有挂二级页面组件"

    index = _strip_comments(_read("views/KnowledgeView.vue"))
    assert "FolderCard" in index, "一级页面没有用目录卡片"
    assert "refreshFolders" in index, "一级页面没有拉目录清单"
    # ``"documents(" not in index`` 抓不到 ``refreshDocuments()``(大写 D),反向测试当场
    # 证明了那条断言是摆设 —— 所以判据写成"这个函数名一次都不许出现"。
    assert "refreshDocuments" not in index, "一级页面自己拉了文档清单 —— 它只该列目录"

    folder = _strip_comments(_read("views/FolderView.vue"))
    for needle in ("面包屑", "stat-strip", "DocumentTable", "EmptyState", "UploadPanel"):
        assert needle in folder, f"二级页面少了 {needle}"
    assert "selected" in folder and "removeSelected" in folder, "没有批量选择/批量删除"
    assert "setScope" in folder, "二级页面没有把作用域切到当前目录"
    assert "stopPolling" in folder, "离开页面没有停轮询"
    return "路由 /knowledge/:folder;一级目录卡;二级面包屑 + 统计条 + 表格 + 批量 + 空状态 + 侧栏"


@check("前端：类型与接口一一对应（FolderRecord / 7 个目录接口 / DocumentRecord.folder）")
def _frontend_types() -> str:
    types = _read("api/types.ts")
    for name in ("FolderRecord", "FoldersOut", "FolderDetail", "FolderSummary"):
        assert f"interface {name}" in types, f"types.ts 少了 {name}"
    assert re.search(r"^\s+folder: string", types, re.M), "DocumentRecord 上没有 folder"
    assert "unfiled: FolderRecord" in types, "未分类那一格不在类型里"

    endpoints = _read("api/endpoints.ts")
    for name in (
        "folders",
        "folderDetail",
        "createFolder",
        "updateFolder",
        "deleteFolder",
        "moveDocument",
    ):
        assert f"export function {name}" in endpoints, f"endpoints.ts 少了 {name}"
    assert "?folder=" in endpoints, "清单没有把目录过滤带上"
    assert "&folder=" in endpoints, "上传没有把落点带上"
    return "4 个目录类型 + 6 个目录接口 + 清单/上传两处带 folder"


def main() -> int:
    return report(
        "知识库两级目录（一级目录 + 二级页面的文档清单）",
        hint="前端改动后先 `cd web && npm run build`；迁移那两条要 MySQL（没有会 SKIP 并点名）。",
    )


if __name__ == "__main__":
    sys.exit(main())

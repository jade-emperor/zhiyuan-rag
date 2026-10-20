#!/usr/bin/env python
"""契约：知识库管理页（上传 / 状态轮询 / 删除连带删向量 / 预览）+ 它新加的预览接口。

用法：

    uv run python scripts/acceptance/check_m7_2_kb.py

**这一份在堵什么洞**：聊天页那会儿「知识库」还只是一张路线图页 —— 后端其实已经有
清单 / 上传 / 删除三条接口，缺的是**界面**；而「预览」这条**根本没有**：
文档入库成了什么样，只能去翻原文件或查库。这一批补上 `GET /api/v1/documents/{id}/chunks`，
并把那一页做成能用的。

**预览的口径**（这几条最容易做错，而且错了看不出来）：

    GET /api/v1/documents/{document_id}/chunks?limit=20&chars=400
      -> {document_id, parent_blocks, child_chunks, returned, truncated,
          blocks: [{index, page, last_page, chars, text}]}

    1. **显示的是父块,不是子块**:向量库里写的是子块,父块正文挂在每个子块的
       `parent_content` 上 —— 所以预览要按 `parent_id` **折叠**。折叠回去的那段才是
       「模型真正读到的东西」,也是引用清单里对齐的那段。
    2. **两个数都要给**:`child_chunks`(入库写进去的)与 `parent_blocks`(折叠后的),
       差着切分比例。只给一个数,前端把"入库 42 块 / 预览 15 段"读成丢数据。
    3. **`chars` 是截断前的长度**:显示"1180 字"而正文只有 400 字,用户才知道后面还有。
    4. **老数据没有父块**(`parent_id` 为空串)时那一段就是它自己 —— 与检索时
       「有父块用父块,没有就用自己」同一条口径。
    5. **没登记就不给预览**(404 + 可执行的话术):库里的孤儿块照样能被检索到,
       但"这份文档"在管理页上不存在 —— 给一份没有主人的预览比报错糟得多。
    6. **权限与清单一致(Bearer,不是 admin)**:员工本来就能看清单(他要知道自己能检索什么),
       预览是同一份数据的另一种看法;真正的管理动作(上传/删除)才是 admin。

**前端那半是静态契约**(读源码):上传框的 `accept`/上限来自 capabilities、删除两步确认、
轮询开关、预览页面的两个数与页码形态。不需要容器 / 模型 / 网络。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

from _harness import check, report

if True:  # 只为了 import 类型:运行期不需要它们(脚本要能在没装模型时也跑)
    from project_rag.domain.documents import DocStatus

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"


# ── 前端源码 ────────────────────────────────────────────────────────────────
def _read(relative: str) -> str:
    """输入:相对 `web/src/` 的路径。行为:读它。输出:正文(不在就报错,不静默给空串)。"""
    path = WEB_SRC / relative
    if not path.is_file():
        raise AssertionError(f"前端源码不在 {path} —— 知识库页的界面还没落地?")
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """输入:一份前端源码。行为:去掉块注释与整行注释。输出:剩下的文本。

    只去整行 ``//``(不去行尾的):注释里出现的 ``.pdf`` 这类字样不该判红。
    """
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", without_block)


# ── 假件 ─────────────────────────────────────────────────────────────────────
def _hits():
    """输入:无。行为:造三子块(两个父块、跨页)+ 一份别的文档的块。输出:RetrievedChunk 列表。"""
    from project_rag.domain import Chunk, RetrievedChunk

    return [
        RetrievedChunk(
            Chunk("c1", "子1", "uploads", "handbook.pdf", "p1", "父块一" * 200, page=7), 0.9
        ),
        RetrievedChunk(Chunk("c2", "子2", "uploads", "handbook.pdf", "p1", "", page=8), 0.8),
        RetrievedChunk(Chunk("c3", "子3", "uploads", "handbook.pdf", "p2", "父块二", page=2), 0.7),
        RetrievedChunk(
            Chunk("x1", "别的", "bagu_ai", "other.md", "", "别的文档的正文"), 0.5
        ),
    ]


class _FakeDocs:
    """登记表的内存替身(端口 `DocumentStore` 的最小可判定实现)。"""

    def __init__(self, rows=()) -> None:
        self.rows = {row.document_id: row for row in rows}

    def init_schema(self) -> None: ...

    def add(self, record) -> None:
        self.rows[record.document_id] = record

    def list_all(self):
        return list(self.rows.values())

    def get(self, document_id):
        return self.rows.get(document_id)

    def set_status(self, document_id, status, *, chunks=None, error=None) -> None:
        record = self.rows.get(document_id)
        if record is not None:
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


def _record(document_id: str = "handbook.pdf", status: DocStatus = "processed"):
    """输入:文档 ID、状态。行为:造一条登记记录。输出:DocRecord。"""
    from project_rag.domain import DocRecord

    return DocRecord(
        document_id=document_id,
        source="uploads",
        filename=document_id,
        status=status,
        chunks=3,
        size_bytes=1234,
        created_at="<ISO 时间>",
    )


class _FakeAuth:
    """认证存储的内存替身(**只实现预览那两条要用的动作**)。"""

    PASSWORDS = {"boss": "pw-123456", "worker": "pw-123456"}

    def __init__(self) -> None:
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
        self.owners: dict[str, str] = {}

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


def _client(*, auth: bool = False, docs=(), hits=None):
    """输入:要不要认证、登记行、向量库里的块。行为:建一个注入了假依赖的 TestClient。输出:client。"""
    from fastapi.testclient import TestClient

    from _fakes import FakeVectorStore
    from project_rag.api import create_app
    from project_rag.cli import Deps

    store = _FakeDocs(docs)
    fields: dict[str, Any] = {
        "document_store_factory": lambda: store,
        "vector_store_factory": lambda: FakeVectorStore(_hits() if hits is None else hits),
    }
    if auth:
        fields["auth_store_factory"] = lambda: _FakeAuth()
    return TestClient(create_app(Deps(**fields)))


def _login(client, username: str = "boss") -> dict[str, str]:
    """输入:客户端、用户名。行为:走真登录接口。输出:带令牌的请求头。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _FakeAuth.PASSWORDS[username]},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _preview(client, document_id: str = "handbook.pdf", **params):
    """输入:客户端、文档 ID、查询参数。行为:调预览接口。输出:响应。"""
    query = "&".join(f"{key}={value}" for key, value in params.items())
    suffix = f"?{query}" if query else ""
    return client.get(f"/api/v1/documents/{document_id}/chunks{suffix}")


# ══ 一、预览接口（行为契约） ═════════════════════════════════════════════════
@check("预览：子块折叠成父块（3 子 → 2 父），页码取这一段的首页与末页")
def _() -> str:
    body = _preview(_client(docs=[_record()]), limit=10).json()
    assert body["parent_blocks"] == 2, body
    assert body["child_chunks"] == 3, body
    assert [block["page"] for block in body["blocks"]] == [2, 7], body["blocks"]
    assert body["blocks"][1]["last_page"] == 8, "跨页那一段的末页应该是 8"
    assert body["blocks"][0]["last_page"] == 2, "没跨页时末页等于首页"
    assert body["truncated"] is False and body["returned"] == 2, body

    capped = _preview(_client(docs=[_record()]), limit=1).json()
    assert capped["returned"] == 1 and capped["truncated"] is True, capped
    assert capped["parent_blocks"] == 2, "截断的是返回条数,不是总数"
    return "3 子块 → 2 段;页码 [2, 7],跨页那段 7–8;limit=1 时截断但总数仍是 2"


@check("预览：chars 是**截断前**的完整字数，正文才是截断后的")
def _() -> str:
    body = _preview(_client(docs=[_record()]), chars=60).json()
    long_block = body["blocks"][-1]
    assert long_block["chars"] == 600, f"父块一×200 应该是 600 字,实际 {long_block['chars']}"
    assert len(long_block["text"]) == 61, f"截断后应是 60 字 + 省略号,实际 {len(long_block['text'])}"
    assert long_block["text"].endswith("…"), long_block["text"][-5:]
    return f"chars={long_block['chars']}(完整)、text={len(long_block['text'])} 字(截断+省略号)"


@check("预览：老数据（没有父块）一段就是它自己 —— 与检索时同一条口径")
def _() -> str:
    from project_rag.domain import Chunk, RetrievedChunk

    flat = [
        RetrievedChunk(Chunk("f1", "扁平块正文", "uploads", "old.md", "", ""), 0.5),
    ]
    body = _preview(_client(docs=[_record("old.md")], hits=flat), "old.md").json()
    assert body["parent_blocks"] == 1 and body["child_chunks"] == 1, body
    assert body["blocks"][0]["text"].startswith("扁平块正文"), body["blocks"][0]
    assert body["blocks"][0]["page"] is None, "老数据没有页码,给 None 而不是 0"
    return "1 子块 → 1 段;正文取自己;页码 None"


@check("预览：limit 与 chars 越界 → 422（上界与实现同源，前端夹得到）")
def _() -> str:
    client = _client(docs=[_record()])
    # 直接拼 query 而不是 `**{"limit": 0}`:动态展开会让类型检查把 int 往
    # `document_id: str` 上套(报一堆假错),而这里本来就是在测"非法参数"
    for query in ("limit=0", "limit=101", "chars=49", "chars=2001"):
        response = client.get(f"/api/v1/documents/handbook.pdf/chunks?{query}")
        assert response.status_code == 422, f"{query} 应该 422,实际 {response.status_code}"
    edge = _preview(client, limit=100, chars=2000)
    assert edge.status_code == 200, edge.text
    assert edge.json()["returned"] == 2, "上界值本身要能跑"
    return "limit 0/101 与 chars 49/2001 都 422;上界 100/2000 可用"


@check("预览：没登记的文档 → 404，而且话术点名能照着做的那条路")
def _() -> str:
    response = _preview(_client(docs=[]), document_id="nobody.pdf")
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert "project-rag doctor" in detail, f"话术里没有可执行的命令:{detail}"
    assert "--prune-orphans" in detail, f"孤儿块那条路没写出来:{detail}"
    return "404 + 话术含 `project-rag doctor` 与 `--prune-orphans`"


@check("预览：还在入库（库里还没有块）→ 200 空块，不是错误")
def _() -> str:
    body = _preview(_client(docs=[_record("pending.pdf", status="pending")], hits=[]),
                    document_id="pending.pdf").json()
    assert body["parent_blocks"] == 0 and body["child_chunks"] == 0, body
    assert body["blocks"] == [] and body["truncated"] is False, body
    return "0 段 / 不截断 —— 「还没入库」不是错误,状态由清单那条负责"


@check("预览：无令牌 401、员工令牌 200（权限与清单同口径,不是 admin 专属）")
def _() -> str:
    client = _client(auth=True, docs=[_record()])
    anonymous = _preview(client)
    assert anonymous.status_code == 401, f"没有令牌竟然能读文档内容:{anonymous.status_code}"
    allowed = client.get(
        "/api/v1/documents/handbook.pdf/chunks", headers=_login(client, "worker")
    )
    assert allowed.status_code == 200, f"员工该能预览(与清单同口径):{allowed.text}"
    return "无令牌 401;员工令牌 200(上传/删除才是 admin)"


# ══ 二、知识库页（静态契约） ═════════════════════════════════════════════════
@check("知识库两级页面：一级列目录、二级接上四条文档接口（不再是路线图页）")
def _() -> str:
    """**这一批把页面拆成了两层**:一级是目录清单,二级是某一格里的文档。

    所以"页面接上了哪条接口"这条断言也跟着拆 —— 一级只读 `/folders`,
    四条文档接口全在二级(``views/FolderView.vue``)。断言本身没变松:四个 action 一个不少。
    """
    index = _read("views/KnowledgeView.vue")
    folder = _read("views/FolderView.vue")
    endpoints = _read("api/endpoints.ts")
    for name in ("documents", "uploadDocument", "deleteDocument", "documentPreview", "folders"):
        assert f"export function {name}" in endpoints, f"endpoints.ts 少了 {name}"
    assert "refreshFolders" in index, "一级页面没有拉目录清单"
    assert "FolderCard" in index, "一级页面没有用目录卡片(那它是按什么分两级的?)"
    for call in ("refreshDocuments", "removeDocument", "openPreview"):
        assert call in folder, f"二级页面没有调 {call}"
    assert "DocumentTable" in folder, "二级页面的主体不是文档表格"
    assert "uploadFiles" in _read("components/UploadPanel.vue"), "上传面板没有接上传"
    assert "路线图" not in index and "路线图" not in folder, "路线图页的文案还在 —— 这两页现在都是真页面"
    return "一级拉 /folders 并渲染目录卡;二级接上清单/删除/预览/上传四个 action"


@check("上传：accept 与大小上限来自 capabilities；拖放与点选走同一条路")
def _() -> str:
    panel = _read("components/UploadPanel.vue")
    assert ":accept=\"accept\"" in panel, "文件框的 accept 没有绑定(会写死后缀)"
    assert "caps.data?.suffixes" in panel and "max_upload_mb" in panel, "没从 capabilities 取能力"
    assert "onDrop" in panel and "onPicked" in panel and "submit(Array.from" in panel, (
        "拖放与点选各写一条路 —— 迟早有一条忘了校验"
    )
    literal = re.search(r'accept="[^"]*\.(pdf|docx|md|txt)', panel)
    assert literal is None, f"accept 里出现了写死的后缀:{literal.group(0) if literal else ''}"
    return "accept/上限来自 capabilities;拖放与点选共用 submit()"


@check("删除：两步确认，且不用 window.confirm（删除会连带删向量,不该一键就没）")
def _() -> str:
    """卡片换成了表格(二级页面),**但"两步确认"这条口径一个字都没变**。

    断言从"卡片里有一个 ``confirming = true``"变成"表格里有一个 ``askRemove`` 把这一行
    记进待确认集合":判据仍是"第一次点击只进确认态、真正发删除的地方只有一处、
    而且它在「确认删除?」之后"。
    """
    table = _read("components/DocumentTable.vue")
    assert re.search(r'@click="askRemove\(doc\)"', table), (
        "第一步的按钮没有把这一行切到「待确认」—— 直接 emit('remove') 就是一键删除"
    )
    assert "confirming" in table, "没有待确认状态(那一行点一下就直接删了)"
    calls = [match.start() for match in re.finditer(r"emit\('remove'", table)]
    assert len(calls) == 1, f"真正发删除的地方应该只有一处,实际 {len(calls)} 处"
    assert "确认删除?" in table, "第二步的按钮文案不在"
    assert calls[0] > table.index("确认删除?"), "发删除的按钮出现在确认那一步之前"
    # 去注释之后再看 `window.confirm`:组件的 docstring 里正 在解释「为什么不用它」——
    # 不剥注释的话,解释禁令的那句话本身会把检查判红(注释被当成代码)
    assert "window.confirm" not in _strip_comments(table), (
        "用了阻塞式 confirm:没法测、也没法显示后端返回的细节"
    )
    view = _read("views/FolderView.vue")
    assert "deleted_chunks" in view and "removed_file" in view and "removed_record" in view, (
        "删完没有如实转述后端的结果(删了几块 / 文件在不在 / 登记行在不在)"
    )
    return "两步确认 + 转述 deleted_chunks/removed_file/removed_record"


@check("轮询：还有入库中的就开、离开页面就关（不然切走还在每 3 秒打一次）")
def _() -> str:
    store = _read("stores/documents.ts")
    assert "POLL_MS" in store and "setInterval" in store and "clearInterval" in store, (
        "轮询没有开/关两端"
    )
    assert "pending" in store and "processing" in store, "轮询条件没有覆盖两种在途状态"
    view = _read("views/FolderView.vue")
    assert re.search(r"onUnmounted\(\(\) => \{[^}]*stopPolling\(\)", view, re.S), (
        "onUnmounted 里没有真的调 stopPolling —— 只 import 不算(切走之后还在打清单)"
    )
    return "3 秒轮询,条件是 pending|processing,onUnmounted 里 stopPolling"


@check("预览界面：同时显示子块与父块两个数；页码区分跨页与「未知」")
def _() -> str:
    dialog = _read("components/PreviewDialog.vue")
    assert "child_chunks" in dialog and "parent_blocks" in dialog, (
        "只显示一个数 → 用户会把「入库 N 块 / 预览 M 段」读成丢数据"
    )
    assert "页码未知" in dialog, "page=null 没有话术(不许显示成 0 或空白)"
    assert "–" in dialog or "-" in dialog, "跨页没区分(第 7 页 与 第 7–8 页 要看得出来)"
    assert "truncated" in dialog, "截断了却不说,用户以为看到的是全部"
    return "两个数 + 页码未知 + 跨页区间 + 截断提示"


def main() -> int:
    return report(
        "知识库管理页 + 文档预览接口",
        hint="前端改动后先 `cd web && npm run build`；产物要入库（Python 侧与镜像都不装 node）",
    )


if __name__ == "__main__":
    sys.exit(main())

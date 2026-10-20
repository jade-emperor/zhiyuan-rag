"""
用例:一级目录(知识库的两层结构)。

**只有两层,而且这一层不做权限**:目录是**组织方式**,不是可见范围 ——
授权仍然按 ``document_id`` 逐份收窄(见 ``application/access.py``)。把可见范围挂到目录上
会立刻遇到"这份在目录 A 里、但只给三个人看"这种组合,而那种组合的语义没人说得清。
目录只回答一个问题:**这些文档归在哪一格**。

五条口径:

0. **目录来自语料目录**:``data/corpus`` 的每个一级子目录就是一格(见
   ``sync_corpus_folders``),语料根本身不是一格。目录表不是"管理员手工维护的一棵分类树",
   而是**这个语料库的组织方式在界面上的投影** —— 两者不一致时,以后者为准;
1. **空串是合法的目录归属**("未分类")。磁盘上有、登记表没有的文件也只能是它 ——
   没有行可写。所以未分类**不是错误状态**,前端不许把它画成告警;
2. **删目录不删文档**。目录里还有文档时**拒绝删**(带着份数回 409):一次误点就清空
   一整个目录是不可逆的,而这个操作长得太像"删掉这一格";空的目录随便删;
3. **改名不改 ID**。``folder_id`` 是前端路由与 ``documents.folder`` 的键,变了就等于
   把目录里的文档全丢了;
4. **``--folder`` 按名字解析**:命令行给的是一句人话,所以连库用的键是名字,
   而"用哪个名字"由 ``check_folder_name`` 一处管(空格/斜杠/保留字都在那里驳回)。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from project_rag.domain.documents import (
    STATE_IN_FLIGHT,
    STATE_MISMATCHES,
    UNFILED,
    UNFILED_ID,
    UNFILED_NAME,
    Folder,
    check_folder_description,
    check_folder_name,
    new_folder_id,
)
from project_rag.observability import get_logger
from project_rag.ports import DocumentStore

from .documents import DocumentView

_logger = get_logger("application.folders")


def _now() -> str:
    """输入:无。行为:登记时间(本机时间、秒级)。输出:ISO-8601 文本。

    与 ``documents.created_at`` 同一个口径(文本、不带时区):这两个字段会并排显示,
    一个带时区一个不带,读起来就像两个系统的数据。
    """
    return datetime.now().isoformat(timespec="seconds")


class FolderNotEmpty(Exception):
    """目录里还有文档。**删目录不删文档**,所以这不是"再确认一次"就能过去的错。"""

    def __init__(self, folder: Folder, documents: int) -> None:
        super().__init__(f"目录 {folder.name!r} 里还有 {documents} 份文档,先移走或删掉再删目录")
        self.folder = folder
        self.documents = documents


class FolderExists(Exception):
    """同名目录已经存在(名字唯一,``--folder`` 靠它解析)。"""


def create_folder(
    store: DocumentStore, name: str, description: str = ""
) -> Folder:
    """输入:登记表、名字、说明。行为:校验后建一个目录。输出:目录。同名抛 ``FolderExists``。

    校验在**用例层**而不是接口层:命令行与接口两条路都要建目录,规则写在两条路上的话
    迟早一条松一条紧(而紧的那条会被用户撞见,松的那条会被数据库撞见)。
    """
    clean_name = check_folder_name(name)
    clean_description = check_folder_description(description)
    if store.folder_named(clean_name) is not None:
        raise FolderExists(f"目录名 {clean_name!r} 已存在")
    now = _now()
    folder = Folder(
        folder_id=new_folder_id(),
        name=clean_name,
        description=clean_description,
        created_at=now,
        updated_at=now,
    )
    store.add_folder(folder)
    _logger.info("新建目录 %s(%s)", folder.name, folder.folder_id)
    return folder


def ensure_folder(store: DocumentStore, name: str) -> Folder:
    """输入:登记表、名字。行为:按名字取目录,没有就建。输出:目录。

    这是 ``project-rag ingest --folder`` 的那条路:管理员在命令行敲的是一个名字,
    不该需要先知道它的 ID、也不该因为"目录还不存在"而先失败一次再重来。
    """
    clean = check_folder_name(name)
    existing = store.folder_named(clean)
    if existing is not None:
        return existing
    try:
        return create_folder(store, clean)
    except FolderExists:  # 两个进程同时建同名目录:后到的那个认前一个
        existing = store.folder_named(clean)
        if existing is None:
            raise
        return existing


def corpus_folder_of_source(corpus_dir: Path, source: str) -> str | None:
    """输入:语料根 + 一份文档的来源标签。行为:判断它是不是语料根下的一个一级目录。输出:目录名或 None。

    来源标签本来就是这个口径(``source_of``:顶层语料目录去掉 ``_data``),所以"它在不在
    语料根下"只要看目录还在不在 —— 两种"没有目录"都返回 ``None``:

    * 来源是 ``uploads`` / 任意临时目录 —— 语料之外的东西没有一级目录;
    * 来源是语料根自己(``corpus``)—— 那是"所有格的父亲",不该是一格。

    磁盘上的目录名与界面上的目录名是同一个词,用户对得上,粘到 ``--folder`` 也能用。
    """
    if not source or not corpus_dir.is_dir():
        return None
    for candidate in (f"{source}_data", source):
        if (corpus_dir / candidate).is_dir():
            return source
    return None


def sync_corpus_folders(store: DocumentStore, corpus_dir: Path) -> tuple[Folder, ...]:
    """输入:登记表 + 语料根。行为:语料根下**每个一级子目录**对应一个一级目录(缺的建上)。输出:目录。

    为什么要它:一级目录不是一张凭空的表 —— 它就是这个语料库的组织方式,
    ``data/corpus/`` 下有哪几个目录,界面上就该有哪几格。少了这一步,新加一个语料目录
    要么看不见(得先灌一遍),要么被"按来源标签自动建目录"的老规则建出一个名字不对的格子。

    **只建不删**:磁盘上少了一个目录时,它里面的登记行还在 —— 删目录会让那些行的
    ``folder`` 指向不存在的 id(比"多一格空的"糟得多)。空的、指向已消失目录的格子,
    由管理员在界面上删(删目录不删文档)。

    目录名不合法(带斜杠、叫"未分类"…)时**跳过并记一条 WARNING**:一个奇怪的目录名
    不该让整次同步失败 —— 那几份文档会显示在未分类里,人能看见并处理。
    """
    if not corpus_dir.is_dir():
        return ()
    synced: list[Folder] = []
    for child in sorted(corpus_dir.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        name = child.name.removesuffix("_data") or child.name
        try:
            synced.append(ensure_folder(store, name))
        except ValueError as exc:
            _logger.warning("语料目录 %r 不能当目录名(%s),跳过", child.name, exc)
    return tuple(synced)


def rename_folder(
    store: DocumentStore,
    folder_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Folder:
    """输入:登记表、目录 ID、可选的新名字与新说明。行为:校验后改名/改说明。输出:新目录。

    ``folder_id`` 不在改动范围内(见模块开头第 3 条)。目录不存在抛 ``LookupError``。
    """
    if folder_id == UNFILED_ID or folder_id == UNFILED:
        raise ValueError(f"{UNFILED_NAME!r} 是虚拟目录,不能改名")
    folder = store.get_folder(folder_id)
    if folder is None:
        raise LookupError(f"没有这个目录:{folder_id!r}")
    clean_name = folder.name if name is None else check_folder_name(name)
    clean_description = (
        folder.description
        if description is None
        else check_folder_description(description)
    )
    if clean_name != folder.name and store.folder_named(clean_name) is not None:
        raise FolderExists(f"目录名 {clean_name!r} 已存在")
    updated = Folder(
        folder_id=folder.folder_id,
        name=clean_name,
        description=clean_description,
        created_at=folder.created_at,
        updated_at=_now(),
    )
    store.update_folder(updated)
    return updated


def delete_folder(store: DocumentStore, folder_id: str, *, documents: int) -> int:
    """输入:登记表、目录 ID、这个目录里还有几份文档。行为:空的才删。输出:删除条数。

    ``documents`` 由调用方(接口层)从**同一份清单**里数出来 —— 让适配器自己再查一遍
    会得到第二个数,而"数出来是 0、删的时候不是"之间正好够一次上传落进来。
    """
    if folder_id == UNFILED_ID or folder_id == UNFILED:
        raise ValueError(f"{UNFILED_NAME!r} 是虚拟目录,不能删")
    folder = store.get_folder(folder_id)
    if folder is None:
        return 0
    if documents > 0:
        raise FolderNotEmpty(folder, documents)
    removed = store.remove_folder(folder_id)
    if removed:
        _logger.info("已删除目录 %s(%s)", folder.name, folder.folder_id)
    return removed


def move_document(store: DocumentStore, document_id: str, folder_id: str) -> None:
    """输入:登记表、文档 ID、目标目录 ID(空串 = 未分类)。行为:改归属。输出:无。

    目标目录必须存在(未分类除外),文档必须已登记:两个条件都在这里挡,而不是让
    ``UPDATE`` 影响 0 行之后报一句"成功"。0 行的两种情况(没这份文档 / 目录不存在)
    对调用方意味着完全不同的下一步,所以分开抛。
    """
    if folder_id not in (UNFILED, UNFILED_ID):
        if store.get_folder(folder_id) is None:
            raise LookupError(f"没有这个目录:{folder_id!r}")
        target = folder_id
    else:
        target = UNFILED
    if store.set_folder(document_id, target) == 0:
        raise LookupError(f"没有这份文档:{document_id!r}")


# ══ 一级页面的数据 ═══════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class FolderView:
    """目录列表上的一格:**目录自己的名字** + **里面文档的真实读数**。

    ``chunks`` 是**库内实际块数**(不是登记表说多少)—— 与清单那一行同一条口径:
    目录卡片上写"677 块"而集合被清空过,是同一个谎换了个地方说。
    """

    folder_id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    documents: int
    chunks: int
    size_bytes: int
    issues: int  # 需要处理:三处不一致 + 在途/失败
    restricted: int  # 里面有几份是收窄过的
    last_activity: str  # 目录里最新一份文档的登记时间(空串 = 还没有文档)
    unfiled: bool = False


def folder_views(
    views: Sequence[DocumentView],
    folders: Sequence[Folder],
    *,
    restricted: Collection[str] = (),
) -> tuple[FolderView, ...]:
    """输入:清单行(``DocumentView``)、目录表、收窄过的文档 ID。行为:按目录聚合。输出:目录格(含未分类)。

    ``views`` 用的就是**清单那一份行**:让目录另查一次库,两个页面上的"共 N 份"就会
    在两次点击之间对不上 —— 而那正是用户最先注意到的那种不一致。

    指向**不存在的目录**的行(数据被手工改过)折进未分类,并记一条 WARNING:
    让它们从界面上消失比放错格子糟得多(用户会以为文档丢了)。
    """
    known = {folder.folder_id: folder for folder in folders}
    groups: dict[str, list[DocumentView]] = {}
    for view in views:
        folder_id = getattr(view, "folder", "")
        if folder_id and folder_id not in known:
            _logger.warning("文档 %s 指向不存在的目录 %r,按未分类显示", view.document_id, folder_id)
            folder_id = UNFILED
        groups.setdefault(folder_id, []).append(view)

    def _view(folder: Folder | None, rows: Sequence[DocumentView]) -> FolderView:
        return FolderView(
            folder_id=UNFILED_ID if folder is None else folder.folder_id,
            name=UNFILED_NAME if folder is None else folder.name,
            description="" if folder is None else folder.description,
            created_at="" if folder is None else folder.created_at,
            updated_at="" if folder is None else folder.updated_at,
            documents=len(rows),
            chunks=sum(row.indexed_chunks for row in rows),
            size_bytes=sum(
                row.disk_size_bytes if row.on_disk else row.size_bytes for row in rows
            ),
            issues=sum(
                1
                for row in rows
                if row.state in STATE_MISMATCHES or row.state in STATE_IN_FLIGHT
            ),
            restricted=sum(1 for row in rows if row.document_id in restricted),
            last_activity=max((row.created_at for row in rows), default=""),
            unfiled=folder is None,
        )

    out = [
        _view(folder, groups.pop(folder.folder_id, ()))
        for folder in sorted(folders, key=lambda item: item.name)
    ]
    # 未分类排在最后:它是一个兜底格子,不是第一等的组织方式
    out.append(_view(None, groups.pop(UNFILED, ())))
    for leftover in groups.values():
        # 理论上到不了这儿(上面已经把陌生 ID 折进未分类了);留下断言性的日志,
        # 免得将来有人改了上面那段而行的去向静默变化
        _logger.warning("有 %d 行没能归进任何目录", len(leftover))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class FolderCounts:
    """目录页汇总。与 ``documents.summarize`` 分开:那个数的是**状态**,
    这个数的是**组织** —— 混在一个结构里,前端迟早把"未分类 3 份"读成"失败 3 份"。
    """

    folders: int  # 真实目录数(不含未分类)
    used: int  # 其中有文档的
    unfiled: int  # 未分类的文档份数


def summary_of(views: Sequence[FolderView]) -> FolderCounts:
    """输入:目录格。行为:数一遍。输出:``FolderCounts``。"""
    return FolderCounts(
        folders=sum(1 for view in views if not view.unfiled),
        used=sum(1 for view in views if not view.unfiled and view.documents > 0),
        unfiled=sum(view.documents for view in views if view.unfiled),
    )

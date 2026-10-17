#!/usr/bin/env python
"""契约:页码从 PDF 一路走到**引用**上,中间每一步都不许静默错位。

用法：

    uv run python scripts/acceptance/check_p0_5_pages.py

**这是最危险的一环**:页码错了**不会报错** —— 引用里那个页号看着完全合法,只是不对。
所以整条链路分段钉死(夹具现造,不需要模型;Milvus 那两条要容器,连不上就 SKIP):

第一环 · 适配器(区间表怎么来):
1. **每页的独有词映射回它自己那一页**(核心不变式:偏移 → 页码);
2. 区间**首尾相接且覆盖全文**,段与段之间只隔 ``\\n\\n`` 那两个字符(不许有洞);
3. **整页没内容的页不留位置** —— 它不贡献字符,算偏移时也就不该占位,
   否则它之后所有页的页码统统前移(这就是 off-by-N 的经典成因);
4. ``read()`` 与 ``read_with_pages()`` 的正文**逐字符一致**,且非 PDF 源**没有**这个方法
   (那正是「md/txt 的引用页码给 None」的来源);
5. 装载器把区间表**带进 ``Document``**(md/txt 是空元组,不是「第 0 页」)。

第二环 · 切分与写入(偏移怎么变成页码):
6. 切分器给的**每块起始偏移都切得出它自己**(父/子两级,含重叠段),且与 ``split_text`` 分段一致
   —— 后者是「这次改造没动块内容,所以 ``chunk_id`` 不变」的证据;
7. ``chunk_documents`` 用偏移算出页码:PDF 三页各自命中;**md 给 None**;
   不报偏移的切分器也给 None(**不许猜**);横跨页边界的块取**起始页**;
8. 引用上的页码**跟着子块走**:折叠 / 重排重建 ``Citation`` 之后仍在(所以它是 property,不是字段);
9. Milvus 往返:有页码给页码、没页码给 ``None``;存储层的哨兵**不许漏到领域层**;
10. **老集合**（之前建的、没有 page 列):读得回来(页码 None),但**写入当场报错**
    —— 静默丢掉页码等于这次优化白做;报错里点名的两条出路(`collections` / `drop`)各有契约:
11. `collections` 列得出块数 / 有没有 page 列 / 当前配置指向哪个(撞上限要渲染成 `16384+`);
12. `drop` **不给 `--yes` 一块都不删**,给了才点名删那一个(不是当前配置那个)。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from _harness import SkipCheck, check, report

# **必须用 ASCII**:PyMuPDF 的内置字体不渲染 CJK,insert_text 写中文抽出来是空的
# (这一条踩过一次 —— 检查报"正文里没有这个标记",看着像渲染坏了,其实是夹具字体问题)。
MARKERS = ("ALPHA-PAGE-ONE", "BETA-PAGE-TWO", "GAMMA-PAGE-THREE")


def _make_pdf(path: Path, *, blank_between: bool) -> None:
    """输入:落盘路径、第 2 与第 3 页之间要不要插一张空白页。行为:造一份 3~4 页的 PDF。输出:无。"""
    import pymupdf

    document = pymupdf.open()
    for index, marker in enumerate(MARKERS):
        page = document.new_page()
        page.insert_text((72, 72), f"{marker} body {index + 1}", fontsize=12)
        if blank_between and index == 1:
            document.new_page()  # 整页空白:没有任何文字块
    document.save(path)
    document.close()


def _require_milvus() -> None:
    """输入:无。行为:探一下 Milvus 活没活。输出:无(连不上抛 SkipCheck)。"""
    from pymilvus import MilvusClient

    from project_rag.config import get_settings

    settings = get_settings()
    target = f"http://{settings.milvus.host}:{settings.milvus.port}"
    try:
        MilvusClient(uri=target).list_databases()  # 只探活:决定这条是 SKIP 还是真判
    except Exception as exc:  # noqa: BLE001 - 容器没起是环境问题,不是契约失败
        raise SkipCheck(
            f"Milvus 连不上（{target}）：{type(exc).__name__}: {str(exc)[:100]}；"
            "先在 Windows 侧 docker compose up -d"
        ) from exc


# ── 第一环:适配器(区间表与正文逐字符对齐) ─────────────────────────────────
@check("页码：每页的独有词映射回它自己那一页（偏移 → 页码）")
def _markers_map_back() -> str:
    from project_rag.adapters.pdf_files import PdfFileSource
    from project_rag.domain import page_of

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "三段.pdf"
        _make_pdf(path, blank_between=False)
        text, spans = PdfFileSource().read_with_pages(path)
        found = []
        for index, marker in enumerate(MARKERS, start=1):
            offset = text.find(marker)
            if offset < 0:
                raise AssertionError(f"正文里没有 {marker} —— 夹具或渲染坏了")
            page = page_of(spans, offset)
            if page != index:
                raise AssertionError(
                    f"{marker} 在第 {index} 页,却映射到 {page} —— 页码错位是**静默**的,必须钉死"
                )
            found.append(f"{marker}→p{page}")
        if page_of(spans, len(text) + 10) is not None:
            raise AssertionError("偏移越界该给 None,不许夹到末页")
        if page_of(spans, -1) is not None:
            raise AssertionError("负偏移该给 None")
        # 拼页用的 \n\n 不属于任何一页:偏移正好落在它上面时给 None,而不是"前一页的尾巴"
        gap = spans[0][1]
        if page_of(spans, gap) is not None:
            raise AssertionError(
                f"偏移 {gap} 是两页之间的分隔符,不该属于任何一页,却映射到 {page_of(spans, gap)}"
            )
        return "；".join(found) + "；越界与段间分隔符 → None"


@check("页码：区间首尾相接且覆盖全文（段间只隔 \\n\\n，不许有洞）")
def _spans_cover_text() -> str:
    from project_rag.adapters.pdf_files import PdfFileSource

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "三段.pdf"
        _make_pdf(path, blank_between=False)
        text, spans = PdfFileSource().read_with_pages(path)
        if spans[0][0] != 0 or spans[-1][1] != len(text):
            raise AssertionError(f"区间该从 0 到 {len(text)},实际 {spans[0][0]}..{spans[-1][1]}")
        for left, right in zip(spans, spans[1:]):
            if right[0] != left[1] + 2:
                raise AssertionError(
                    f"两段之间该正好隔 2 个字符(\\n\\n),实际 {left} → {right} —— 这中间有个洞"
                )
        return f"{len(spans)} 段首尾相接、覆盖 {len(text)} 字符"


@check("页码：整页空白的页不留位置（它不贡献字符，就不该占偏移）")
def _blank_page_takes_no_room() -> str:
    from project_rag.adapters.pdf_files import PdfFileSource
    from project_rag.domain import page_of

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "带空白页.pdf"
        _make_pdf(path, blank_between=True)
        text, spans = PdfFileSource().read_with_pages(path)
        # **测行为不测条数**:空白页哪怕留下一个"零长度区间"也无害(它一个字符都不占,
        # `page_of` 永远落不到它上面)。真正要钉的是"它不许占字符"。
        for start, end, page in spans:
            if page == 3 and end != start:
                raise AssertionError(
                    f"空白页占了 {end - start} 个字符 —— 它之后所有页都会前移"
                )
        offset = text.find(MARKERS[2])
        page = page_of(spans, offset)
        if page != 4:
            raise AssertionError(
                f"空白页是原文第 3 页,所以「丙页独有词」在第 4 页;实际 {page} —— "
                f"若给 3,说明空白页被算进了偏移,它之后所有页都会前移一位"
            )
        return f"空白页占 0 字符;{MARKERS[2]} → p{page}(原文第 4 页)"


@check("页码：read() 与 read_with_pages() 正文逐字符一致；非 PDF 源没有这个方法")
def _read_matches_and_only_pdf() -> str:
    from project_rag.adapters import PdfFileSource
    from project_rag.adapters import text_files as text_module

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "三段.pdf"
        _make_pdf(path, blank_between=True)
        source = PdfFileSource()
        text, _spans = source.read_with_pages(path)
        if source.read(path) != text:
            raise AssertionError(
                "两处渲染不一致 —— 迟早会漂,而漂的后果正是页码整体错位"
            )
        text_source = next(
            obj for name, obj in vars(text_module).items()
            if isinstance(obj, type) and getattr(obj, "suffixes", None)
        )
        if hasattr(text_source, "read_with_pages"):
            raise AssertionError(
                "md/txt 不该有页码表(引用里它们的页码是 None,不是 0)—— 有这个方法就说明口径混了"
            )
        return "read() == read_with_pages()[0];md/txt 无页码表(引用页码为 None)"


@check("页码：装载器把区间表带进 Document（PDF 带上、md/txt 是空元组而不是「第 0 页」）")
def _loader_carries_pages() -> str:
    from project_rag.adapters.document_loader import load_documents
    from project_rag.domain import page_of

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _make_pdf(tmp / "三段.pdf", blank_between=False)
        (tmp / "说明.md").write_text("# 标题\n正文一段。", encoding="utf-8")
        loaded = load_documents(tmp)
        by_id = {doc.doc_id: doc for doc in loaded.documents}
        pdf = by_id["三段.pdf"]
        if not pdf.pages:
            raise AssertionError("PDF 的 Document 没带页码区间 —— 装载器没走 read_with_pages")
        if pdf.pages[-1][1] != len(pdf.text):
            raise AssertionError(
                f"带的区间表与 text 对不上:区间到 {pdf.pages[-1][1]},正文 {len(pdf.text)} 字符"
            )
        marker_page = page_of(pdf.pages, pdf.text.find(MARKERS[1]))
        if marker_page != 2:
            raise AssertionError(f"装载之后仍该能查出 {MARKERS[1]} 在第 2 页,实际 {marker_page}")
        md = by_id["说明.md"]
        if md.pages != ():
            raise AssertionError(
                f"md 没有页码概念,该给空元组(引用里给 None,不是第 0 页),实际 {md.pages}"
            )
        return f"PDF {len(pdf.pages)} 段区间且与 text 对齐;md 页码为空元组"


# ── 第二环:偏移 → 页码 → 引用 → 落库 ───────────────────────────────────────
@check("偏移：切分器给的每块起始偏移都切得出它自己（两级、含重叠；分段与 split_text 一致）")
def _chunker_offsets_round_trip() -> str:
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker

    # 刻意用**大量重复的句子**:重复文本正是"拿块文本回原文 find"会踩的坑,
    # 而切分器给的偏移来自它自己切的过程,重复多少遍都不受影响。
    body = "这一句在语料里出现很多次,用来逼出事后查找的歧义。"
    text = "\n\n".join(f"第 {index} 段({body*2})" for index in range(1, 60))
    chunker = ChineseRecursiveChunker(300, 90, 30)
    groups = chunker.split(text)
    if not groups:
        raise AssertionError("切不出任何父块 —— 夹具坏了")

    # ① 分段与 split_text 完全一致:证明这次改造只加了元数据,块内容与 chunk_id 都没动
    from_split_text = chunker._parent_splitter.split_text(text)
    if [group.parent_text for group in groups] != from_split_text:
        raise AssertionError("父块分段变了 —— 那 chunk_id 会跟着变,这不是'只加页码'")

    seen = 0
    for group in groups:
        if len(group.child_starts) != len(group.children):
            raise AssertionError(
                f"父块报的偏移数 {len(group.child_starts)} 与子块数 {len(group.children)} 不等 —— "
                "页码会张冠李戴(而 ports/chunker.py 的契约是同序等长)"
            )
        if list(group.child_starts) != sorted(group.child_starts):
            raise AssertionError(f"偏移必须递增,实际 {group.child_starts}")
        for piece, start in zip(group.children, group.child_starts):
            if text[start : start + len(piece)] != piece:
                raise AssertionError(
                    f"偏移 {start} 处切出来的不是这一块 —— 页码会落在别的页上(静默)"
                )
            seen += 1
    return f"{len(groups)} 个父块 / {seen} 个子块,两级偏移逐字符对得上(文本 {len(text)} 字,含重叠与重复句)"


@check("页码：chunk_documents 用偏移算页码（PDF 三页各自命中；其余一律 None，不许猜）")
def _ingest_computes_page() -> str:
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import load_documents
    from project_rag.application import chunk_documents
    from project_rag.domain import ChunkGroup, Document

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        _make_pdf(tmp / "三段.pdf", blank_between=False)
        (tmp / "说明.md").write_text(
            "# 标题\n正文一段。\n\n## 小节\n" + "中文内容。" * 200, encoding="utf-8"
        )
        loaded = load_documents(tmp)
        # 子块**必须比一页的正文小**:默认 300 字会把三页并进同一块,而那块按"起始页"算
        # 本来就该报第 1 页 —— 那样这个夹具根本验不到第二、三页(踩过一次)。
        # 夹具一页 21~23 字,子块 30 字 ⇒ 每页各自成块。
        chunks = chunk_documents(loaded.documents, ChineseRecursiveChunker(200, 30, 0))

    pdf_chunks = [chunk for chunk in chunks if chunk.doc_id.endswith(".pdf")]
    md_chunks = [chunk for chunk in chunks if chunk.doc_id.endswith(".md")]
    if not pdf_chunks or not md_chunks:
        raise AssertionError("夹具没切出块来")
    for chunk in pdf_chunks:
        if chunk.page is None:
            raise AssertionError(f"PDF 的块 {chunk.chunk_id[:8]}… 没算出页码")
    hits = {}
    for index, marker in enumerate(MARKERS, start=1):
        holders = [chunk.page for chunk in pdf_chunks if marker in chunk.text]
        if not holders:
            raise AssertionError(f"没有块含 {marker} —— 夹具或切分坏了")
        hits[marker] = holders[0]
        if set(holders) != {index}:
            raise AssertionError(
                f"含 {marker} 的块页码是 {holders},而 {marker} 在第 {index} 页"
            )
    stray = [(chunk.chunk_id[:8], chunk.page) for chunk in md_chunks if chunk.page is not None]
    if stray:
        raise AssertionError(
            f"md 没有页码概念,该给 None;实际有 {len(stray)} 块带了页码:{stray[:3]} —— "
            "给 0 或 1 都是在编"
        )

    # ② 不报偏移的切分器(老实现/第三方实现)→ None。**不许拿块文本回原文 find 来猜**:
    #    重复句子在语料里到处都是,猜出来的位置看着合法但可能是别的页。
    class NoOffsetChunker:
        def split(self, text: str):
            return [ChunkGroup(parent_text=text, children=(text[:20], text[20:40]))]

    blind = chunk_documents(
        [Document(doc_id="d", text="甲" * 40, source="s", pages=((0, 40, 9),))],
        NoOffsetChunker(),
    )
    if any(chunk.page is not None for chunk in blind):
        raise AssertionError("切分器没报偏移,却算出了页码 —— 那是猜的")

    # ③ 偏移数与子块数对不上 → 当场报错(否则后面所有子块的页码整体错位)
    class MismatchChunker:
        def split(self, text: str):
            return [ChunkGroup(parent_text=text, children=(text[:20], text[20:40]), child_starts=(0,))]

    try:
        chunk_documents(
            [Document(doc_id="d", text="乙" * 40, source="s", pages=((0, 40, 9),))],
            MismatchChunker(),
        )
    except ValueError as exc:
        mismatch = str(exc)
    else:
        raise AssertionError("偏移比子块少,却一声不吭地入库了 —— 页码会张冠李戴")

    # ④ 横跨页边界的块取**起始页**(页码只有一个,不写区间)
    spanning = Document(
        doc_id="跨页.md", text="丙" * 30 + "\n\n" + "丁" * 30, source="s",
        pages=((0, 30, 1), (32, 62, 2)),
    )
    wide = chunk_documents([spanning], ChineseRecursiveChunker(200, 100, 0))
    if [chunk.page for chunk in wide] != [1]:
        raise AssertionError(
            f"横跨第 1、2 页的块该取起始页 1,实际 {[chunk.page for chunk in wide]}"
        )

    # ⑤ 边界:块从一页的**最后几个字符**开始,也算**那一页**
    #    —— 真语料上真踩到过:某块起于第 62 页最后 2 个字符("48"),开头 30 字全在第 63 页上。
    #    按「报开始的那一页」,它就该是 62;反过来(报结束页)则会让"翻到第 62 页"翻错。
    class TailChunker:
        def split(self, text: str):
            return [ChunkGroup(parent_text=text, children=(text[29:],), child_starts=(29,))]

    tail = chunk_documents(
        [Document(
            doc_id="尾巴.md", text="甲" * 30 + "\n\n" + "乙" * 30, source="s",
            pages=((0, 30, 1), (32, 62, 2)),
        )],
        TailChunker(),
    )
    if [chunk.page for chunk in tail] != [1]:
        raise AssertionError(
            f"只剩 1 个字符在第 1 页上,也该报第 1 页(块开始的地方);实际 {[c.page for c in tail]}"
        )

    return (
        "；".join(f"{marker}→p{page}" for marker, page in hits.items())
        + f"；md {len(md_chunks)} 块全 None；无偏移/数目不符各自的判据都成立；"
        + "跨页块取起始页(含只剩 1 个字符在起始页的边界)"
        + f"（对不上时报的是：{mismatch[:40]}…）"
    )


@check("页码：引用上的页码跟着子块走（折叠/重排重建 Citation 之后仍在）")
def _citation_keeps_page() -> str:
    import dataclasses

    from project_rag.application import rerank_citations
    from project_rag.application.answer_rag import collapse_parents
    from project_rag.domain import Chunk, Citation

    from _fakes import FakeReranker

    if "page" in {field.name for field in dataclasses.fields(Citation)}:
        raise AssertionError(
            "Citation.page 该是 property(取自它带着的子块),不该是字段 —— "
            "折叠/重排每次都重建 Citation,字段就得靠人记得抄一遍,漏了页面上就少一个页码"
        )

    same_parent = (
        Citation(index=1, chunk=Chunk("c1", "子块甲", "ai", "d1", "pa", "父块甲", page=3), score=0.4),
        Citation(index=2, chunk=Chunk("c2", "子块乙", "ai", "d1", "pa", "父块甲", page=3), score=0.9),
        Citation(index=3, chunk=Chunk("c3", "子块丙", "ai", "d2", page=None), score=0.5),
    )
    collapsed = collapse_parents(same_parent)
    if [item.page for item in collapsed] != [3, None]:
        raise AssertionError(
            f"折叠后页码该是 [3, None](跟着留下的那条子块),实际 {[item.page for item in collapsed]}"
        )
    reranked = rerank_citations("问题", collapsed, reranker=FakeReranker())
    for item in reranked:
        if item.page != item.chunk.page:
            raise AssertionError(
                f"重排重建引用之后页码掉队了:{item.page} != 子块的 {item.chunk.page}"
            )
    return "折叠留最高分那条(页码 3);重排重建之后每条仍等于自己子块的页码;None 一路保持 None"


@check("页码：Milvus 往返（有页码给页码、没页码给 None；存储层哨兵不许漏出来）")
def _milvus_round_trip() -> str:
    import uuid

    from pymilvus import MilvusClient

    from project_rag.adapters import HashingEmbedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import get_settings
    from project_rag.domain import Chunk

    _require_milvus()
    settings = get_settings()
    name = f"rag_acceptance_p05_{uuid.uuid4().hex[:8]}"
    store = MilvusVectorStore(settings.milvus.model_copy(update={"collection": name}))
    embedder = HashingEmbedder(dim=64)  # 只验读写契约,不验语义 → 不加载 2G 模型
    try:
        rows = [
            Chunk("c1", "有页码的块", "ai", "d1", page=7),
            Chunk("c2", "没页码的块", "ai", "d2"),
        ]
        store.upsert(rows, embedder.embed([row.text for row in rows]))
        back = {chunk.chunk_id: chunk.page for chunk in store.load_all()}
        if back != {"c1": 7, "c2": None}:
            raise AssertionError(
                f"load_all 回来的页码该是 {{'c1': 7, 'c2': None}},实际 {back} —— "
                "None 变成了别的值(0/-1 会被读成一个真实页号)"
            )
        hits = {hit.chunk.chunk_id: hit.chunk.page for hit in store.search(embedder.embed(["有页码的块"])[0], k=2)}
        if hits.get("c1") != 7:
            raise AssertionError(f"search 回来的页码该带上 7,实际 {hits}")

        # 存储层长什么样也钉一下:Milvus 2.4.4 **不支持 nullable**(建集合时收下 nullable=True、
        # 插入缺列的行时却拒收,实测),所以列里存哨兵 -1。
        # 等到升上 ≥2.5 换成 nullable 时,这一条会和适配器一起改 —— 那时它会**红**,正是提醒。
        raw = {
            row["chunk_id"]: row["page"]
            for row in MilvusClient(
                uri=f"http://{settings.milvus.host}:{settings.milvus.port}"
            ).query(
                collection_name=name, filter="", output_fields=["chunk_id", "page"],
                limit=10, consistency_level="Strong",
            )
        }
        if raw.get("c2") != -1:
            raise AssertionError(
                f"没页码的块在存储层该是哨兵 -1(Milvus 2.4.4 没有 nullable),实际 {raw.get('c2')!r}；"
                "升级到 ≥2.5 之后改成 nullable 并同步这条断言"
            )
        return f"c1→p7、c2→None 往返一致;存储层哨兵 {raw.get('c2')}(2.4.4 无 nullable)"
    finally:
        store.drop()


@check("页码：老集合没有 page 列时读得回来（None）、写入当场报错（不许静默丢页码）")
def _legacy_collection_refuses_write() -> str:
    import uuid

    from pymilvus import DataType, MilvusClient

    from project_rag.adapters import HashingEmbedder
    from project_rag.adapters.milvus_store import MilvusVectorStore
    from project_rag.config import get_settings
    from project_rag.domain import Chunk

    _require_milvus()
    settings = get_settings()
    name = f"rag_legacy_p05_{uuid.uuid4().hex[:8]}"
    uri = f"http://{settings.milvus.host}:{settings.milvus.port}"
    client = MilvusClient(uri=uri)
    # 手搓一个**加 page 列之前**的集合:字段与老 schema 一致,就是没有 page
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("text", DataType.VARCHAR, max_length=512)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=64)
    schema.add_field("source", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
    schema.add_field("parent_content", DataType.VARCHAR, max_length=512)
    index = client.prepare_index_params()
    index.add_index(field_name="dense_vector", index_type="FLAT", metric_type="IP")
    client.create_collection(collection_name=name, schema=schema, index_params=index)

    store = MilvusVectorStore(settings.milvus.model_copy(update={"collection": name}))
    embedder = HashingEmbedder(dim=64)
    try:
        client.insert(
            name,
            [
                {
                    "chunk_id": "old", "text": "老块", "dense_vector": [1.0] + [0.0] * 63,
                    "source": "ai", "doc_id": "d1", "parent_id": "", "parent_content": "",
                }
            ],
        )
        legacy = store.load_all()
        if [chunk.page for chunk in legacy] != [None]:
            raise AssertionError(
                f"老集合读回来该给 None(那时还没有页码这回事),实际 {[c.page for c in legacy]}"
            )
        try:
            store.upsert([Chunk("new", "新块", "ai", "d2", page=5)], embedder.embed(["新块"]))
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError(
                "往没有 page 列的集合里写,居然成功了 —— 页码会被静默丢掉,"
                "而这一批的全部意义就是让页码落库"
            )
        if name not in message or "page" not in message:
            raise AssertionError(f"报错里该说清是哪个集合缺哪一列,实际:{message[:120]}")
        # 报错必须**给出路**:以前只说"换名字或删掉",而"删掉"当时根本没有接口 ——
        # 用户拿着这句话只能自己去翻 pymilvus(实测被问过一次)。现在两个命令都要点名。
        for remedy in ("project-rag collections", "project-rag drop --collection"):
            if remedy not in message:
                raise AssertionError(
                    f"报错里该点名可执行的出路 {remedy!r}(否则用户只能自己想办法),实际:{message[:200]}"
                )
        return f"老集合读回来 page=None;写入报错并点名集合与列（{message[:36]}…）"
    finally:
        store.drop()


# ── 集合级运维（迁移的配套接口:列现状 / 删老集合) ──────────────────────
@check("集合运维：collections 列得出「块数 / 有没有 page 列 / 当前配置指向哪个」")
def _collections_command() -> str:
    """之后「哪个集合能用」是个必须先看一眼的问题：往没有 ``page`` 列的集合写会被拒，
    而删集合是破坏性动作。这条钉**看得到**：块数、有没有 page 列、哪个是当前配置指向的。

    夹具里那条 16384 块的是**撞上限**的样子（必须渲染成 ``16384+``，
    不然人会以为库里正好这么多 —— 上限就是 Milvus 的查询窗口上限）。
    """
    import argparse
    import contextlib
    import io

    from project_rag.adapters.milvus_store import CollectionInfo
    from project_rag.cli.commands import collections as collections_cmd
    from project_rag.cli.deps import Deps
    from project_rag.config import get_settings

    current = get_settings().milvus.collection
    infos = (
        CollectionInfo(name="old_no_page", rows=10, capped=False, has_page=False, dim=1024),
        CollectionInfo(name=current, rows=1283, capped=False, has_page=True, dim=1024),
        CollectionInfo(name="huge", rows=16384, capped=True, has_page=True, dim=1024),
    )
    args = argparse.Namespace(deps=Deps(collections_factory=lambda: infos))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = collections_cmd.run(args)
    out = buffer.getvalue()
    if code != 0:
        raise AssertionError(f"collections 该返回 0，实际 {code}：{out}")
    # 新版的 collections 还会打稀疏列与索引类型(check_m3_sparse 那一路加的),
    # 所以这里只钉"它必须把 page 的有无说清楚"——措辞变了断言跟着变,别钉死整句
    for want in ("old_no_page", "1283", "缺 page", "16384+", "← 当前配置"):
        if want not in out:
            raise AssertionError(f"collections 输出里少了 {want!r}：\n{out}")
    if out.count("← 当前配置") != 1:
        raise AssertionError(f"「当前配置指向」该只标一个，实际标了 {out.count('← 当前配置')} 个：\n{out}")
    return f"3 个集合都列出来了；当前指向 {current} 标了 1 次；撞上限渲染成 16384+"


@check("集合运维：drop 不给 --yes 一块都不删；给了才点名删那一个")
def _drop_command() -> str:
    """删集合是**不可恢复**的动作，所以契约钉三件事（三件都是"删错了"的形态）：

    1. 不给 ``--yes`` → **一个都不删**，而且返回非 0（脚本里不会把"我拒绝了"读成"删成功了"）；
    2. 给了 ``--yes`` → 删的必须是**点名的那个**，不是当前配置指向的那个
       （迁移时要删的恰恰常常不是配置里那个，写反了会删掉正在用的库）；
    3. 点名一个不存在的集合 → 不删、点名报错、列出有哪些（不然只能猜）。
    """
    import argparse
    import contextlib
    import io

    from project_rag.adapters.milvus_store import CollectionInfo
    from project_rag.cli.commands import drop as drop_cmd
    from project_rag.cli.deps import Deps
    from project_rag.config import get_settings

    calls: list[str] = []

    def _drop(name: str) -> int:
        calls.append(name)
        return 10

    infos = (
        CollectionInfo(name="old", rows=10, capped=False, has_page=True, dim=1024),
        CollectionInfo(name=get_settings().milvus.collection, rows=5, capped=False, has_page=True, dim=1024),
    )
    deps = Deps(collections_factory=lambda: infos, drop_collection_factory=_drop)

    def run(collection: str, yes: bool) -> tuple[int, str, str]:
        args = argparse.Namespace(collection=collection, yes=yes, deps=deps)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = drop_cmd.run(args)
        return code, out.getvalue(), err.getvalue()

    # ① 只说不做
    code, out, _err = run("old", yes=False)
    if calls or code == 0:
        raise AssertionError(f"不给 --yes 时一块都不该删，实际调用 {calls}、退出码 {code}：{out}")
    if "old" not in out or "--yes" not in out:
        raise AssertionError(f"拒绝时该说清「会删哪个、要加 --yes」，实际：{out}")

    # ② 点名删（而且删的不是当前配置那个）
    code, out, _err = run("old", yes=True)
    if calls != ["old"] or code != 0:
        raise AssertionError(f"--yes 该只删点名的 old，实际 {calls}、退出码 {code}：{out}")

    # ③ 点名的集合不存在
    calls.clear()
    code, _out, err = run("不存在的集合", yes=True)
    if calls or code == 0 or "不存在的集合" not in err:
        raise AssertionError(f"不存在的集合不该删任何东西、且要点名报错，实际 {calls} / {code} / {err}")

    return "不给 --yes 零删除(退出码 1)；--yes 只删点名的 old；不存在则报错并列出可选"


def main() -> int:
    return report(
        "契约:PDF 页码从区间表一路到引用 + 集合运维",
        notes=(
            "说明:第一环与第二环前半用现造夹具(不需要模型/网络);Milvus 那两条要容器,连不上就 SKIP;"
            "集合运维那两条(collections / drop)全用假件。",
            "真语料抽检:add_start_index 的偏移在 md(124081 字 → 父 113/子 606)与",
            "  pdf(136406 字、148 段 → 父 119/子 591)上逐字符对得上,见 pdf_files.read_with_pages 的注释。",
        ),
        hint="提示:页码错了不会报错,只会给出一个看着合法的错页号 —— 所以这一层必须钉死。",
    )


if __name__ == "__main__":
    sys.exit(main())

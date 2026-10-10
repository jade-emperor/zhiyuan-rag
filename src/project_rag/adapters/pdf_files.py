"""
PDF 来源:``.pdf``(PyMuPDF)。

**为什么用 PyMuPDF 而不是 unstructured**:``unstructured`` 的 PDF 分支缺依赖
(``pdfminer.six`` 没装,实测 import 就报 ``ModuleNotFoundError``;补装又要联网,
而它的 pdf extra 还会拖进 ``pdf2image`` / ``google-cloud-vision`` 一串用不上的东西);
PyMuPDF 是**已经在 pyproject 里的**依赖,还多给一样 unstructured 给不了的东西 —— **书签目录**。
实测两份语料:``大模型数学原理介绍.pdf`` 308 条书签(4 级)、``大语言模型-LLMBook.pdf`` 224 条(3 级),
标题层级是出版社排好的,不用猜。父块正是按标题切的,这层结构直接决定切分质量。

**扫描件抽不出文字**:没有文字层的 PDF(整页是图片)在这里得到空串,由调用方报「没抽出文字」。
OCR 是另一件事(``rapidocr-onnxruntime`` 装了但没接),见 ``docs/deferred.md`` §1.4。

**已知取舍**:书签只记「标题 → 第几页」,不记页内位置,所以同一页上的多个标题会被提到这一页的开头。
要精确到行得拿标题文本回页里找,遇到排版差异(空格/断行)反而更脆 —— 先不做。

PyMuPDF 是 AGPL 许可(本就在项目依赖里,不是这次新引入的)。

**页码(P0-5)**:``read_with_pages`` 是**唯一的**页码来源,它给出 ``(起始, 结束, 页码)`` 区间;
分块器用 ``add_start_index=True`` 拿到每块的偏移,再二分查这张表就知道块在哪一页
(二分那个纯函数在 ``domain/pages.py`` —— 算页码的是用例层,而用例层不许 import 适配器)。
**页码不进 ``chunk_id``** —— 身份是「内容 + 结构位置」,页码是源文件的排版属性;
混进身份意味着"PDF 重新导出一次就整库重灌、历史引用全部失效"(决策见 STATUS §7 P0-5)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

from project_rag.domain import page_of

from ._text_blocks import MAX_HEADING_LEVEL, join_wrapped


def _paragraphs(page) -> Iterator[str]:
    """输入:PDF 的一页。行为:取文字块(跳过图片),每块接成一行。输出:段落序列。

    用 ``blocks`` 而不是 ``get_text()``:后者给的是**每一行**,行与行之间没有段落边界,
    切分器找不到 ``\\n\\n``,父块就会在任意位置断开(而且图片会以空块的形式混进来)。
    """
    for block in page.get_text("blocks"):
        # (x0, y0, x1, y1, 文字, 块号, 类型):类型 0 是文字,1 是图片
        if block[6] != 0:
            continue
        text = join_wrapped(block[4])
        if text:
            yield text


def _headings_by_page(document) -> dict[int, list[str]]:
    """输入:PyMuPDF 文档。行为:把书签目录按页码分桶,翻成 markdown 标题。输出:页码 → 标题行序列。

    页码越界或标题为空的条目直接丢掉:目录里偶尔有指向不存在的页/只有空白标题的条目,
    留着会插进一个找不到出处的标题。
    """
    headings: dict[int, list[str]] = {}
    for level, title, page in document.get_toc():
        title = (title or "").strip()
        if not title or not 1 <= page <= document.page_count:
            continue
        hashes = "#" * min(max(level, 1), MAX_HEADING_LEVEL)
        headings.setdefault(page, []).append(f"{hashes} {title}")
    return headings


# 一页的正文片段:(页码, 该页渲染出来的文本)。页码从 1 开始(与书签目录同一套)。
_Piece = tuple[int, str]


def _pieces(path: Path) -> list[_Piece]:
    """输入:一份 .pdf 的路径。行为:逐页取段落,并按书签目录插入标题。输出:``(页码, 该页文本)`` 列表。

    **``read`` 与 ``read_with_pages`` 共用它** —— 两处各渲染一遍迟早会漂,
    而漂的后果是页码整体错位(静默:引用的页号看着合法,只是不对)。
    """
    import pymupdf

    with pymupdf.open(path) as document:
        headings = _headings_by_page(document)
        pieces: list[_Piece] = []
        # 用 load_page 而不是 iterate/下标:PyMuPDF 的 Document 靠 __getitem__ 兜出可迭代,
        # 类型存根里没有 __iter__,写成 for page in document 会被判成不可迭代(运行期却没事)。
        for number in range(1, document.page_count + 1):
            blocks = list(headings.get(number, []))
            blocks += list(_paragraphs(document.load_page(number - 1)))
            if blocks:
                # **整页没内容就整页跳过**:它不往正文里贡献任何字符,
                # 所以算偏移时也不能给它留位置(留了后面所有页的页码全部前移一位)。
                pieces.append((number, "\n\n".join(blocks)))
    return pieces


# ``page_of``(偏移 → 页码的二分)搬去了 ``project_rag.domain.pages``:
# 它是个纯函数、与 PDF 无关,而**用它的是用例层**(chunk_documents 拿切分器给的偏移查表)——
# application 不许 import adapters,留在适配器里就等于逼用例层破层或者再写一遍二分。
# 这个模块只负责**产出**区间表(见 read_with_pages),不负责查。


class PdfFileSource:
    """``.pdf`` → 带书签层级的 markdown(可选带上「每段正文在第几页」)。"""

    suffixes: ClassVar[frozenset[str]] = frozenset({".pdf"})

    def read(self, path: Path) -> str:
        """输入:一份 .pdf 的路径。行为:逐页取段落,并按书签目录插入标题。输出:markdown 文本。

        页与页之间也空一行:上一页的末段与下一页的首段不能粘成一个段落。
        """
        return "\n\n".join(text for _page, text in _pieces(path))

    def read_with_pages(self, path: Path) -> tuple[str, tuple[tuple[int, int, int], ...]]:
        """输入:一份 .pdf 的路径。行为:渲染正文,并给出每个片段落在哪一页。输出:``(正文, 页码区间)``。

        区间是 ``(起始偏移, 结束偏移, 页码)``,左闭右开、**首尾相接且覆盖全文** ——
        于是「某个块在第几页」= 拿它的偏移做一次二分(见 ``page_of``)。

        **偏移必须与 ``read()`` 的正文逐字符对得上**(两处共用 ``_pieces`` 就是这个原因):
        对不上的话页码会整体错位,而且是**静默**的 —— 页号看着合法,只是不对。
        这一段 在真语料上量过:切分器带 ``add_start_index=True`` 时,
        父/子两级的偏移都能逐个对回原文(md ``JBL常见AI面试题-火箭.md`` 124081 字 → 父 113 / 子 606;
        pdf ``大模型数学原理介绍.pdf`` 136406 字、148 段页码区间 → 父 119 / 子 591),
        且分段与 ``split_text`` 完全一致 —— 所以块内容与 ``chunk_id`` 都不会因为这次改造而变。
        """
        text_parts: list[str] = []
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        for page, piece in _pieces(path):
            if text_parts:  # 分隔符也要算进偏移:"\n\n".join 的两个字符是真实存在的
                text_parts.append("\n\n")
                cursor += 2
            spans.append((cursor, cursor + len(piece), page))
            text_parts.append(piece)
            cursor += len(piece)
        return "".join(text_parts), tuple(spans)

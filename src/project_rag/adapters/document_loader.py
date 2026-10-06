"""
语料装载:目录 → ``Document`` 序列(**格式分派 + 命名在这里,只有这一处**)。

分工:每种格式一个 ``DocumentSource`` 适配器,只管「读懂一份文件」;遍历目录、决定
``doc_id`` / ``source``、跳过不认识的文件都由这里做。这些规则对每种格式都一样 ——
写在适配器里就会一个格式漂一个版本,而症状是「重灌后块 id 变了」这种极难查的静默漂移。

两条必须守住的语义(``chunk_id`` 的幂等建立在它们上面):

* **按路径排序** —— 同一份输入跑两次顺序必须一样,否则「同一份语料 → 同一批 id」无从谈起;
* **``source`` = 顶层目录名去掉 ``_data``** —— ``bagu_ai_data/`` → ``bagu_ai``,可以直接当
  向量库的 source 过滤字段用(单份文件时取它所在目录)。

**不认识的文件只报告、不抛错**:语料目录里躺着 ``.xlsx`` / ``.zip`` / ``.DS_Store`` 很正常,
为它们中断整个入库没有道理;但也不能一声不吭 —— 少入库一份文档比报错更难发现,
所以 ``skipped`` / ``empty`` 都要原样交回给调用方去打印。

**解析出错则相反,直接抛**:文件损坏 / 格式不对是**这一份文档进不去库**,得让人当场看见
(而不是「这次只入库了 299 份」这种要数一数才知道的漏)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from project_rag.domain import Document
from project_rag.ports import DocumentSource

from .office_files import OfficeFileSource
from .pdf_files import PdfFileSource
from .text_files import TextFilesSource

# 装配处:加一种格式 = 往这里加一个适配器(顺序只影响报错信息,分派按扩展名)
SOURCES: tuple[DocumentSource, ...] = (
    TextFilesSource(),
    OfficeFileSource(),
    PdfFileSource(),
)

_BY_SUFFIX: dict[str, DocumentSource] = {
    suffix: source for source in SOURCES for suffix in source.suffixes
}


@dataclass(frozen=True, slots=True)
class LoadedCorpus:
    """一次装载的结果:读进来的文档 + 两份「没读进来」的清单。

    ``skipped`` 是不认识的扩展名,``empty`` 是认了但没抽出文字(扫描件 PDF / 空文档)。
    分开报是因为修法不同:前者要加一种格式,后者要 OCR 或者把文件清掉。
    """

    documents: tuple[Document, ...]
    skipped: tuple[str, ...]
    empty: tuple[str, ...]


def supported_suffixes() -> tuple[str, ...]:
    """输入:无。行为:汇总所有适配器认识的扩展名。输出:排序后的扩展名元组。

    给报错信息用:说「不支持」的时候必须同时说清「支持哪些」,否则用户只能猜。
    """
    return tuple(sorted(_BY_SUFFIX))


def load_documents(path: Path) -> LoadedCorpus:
    """输入:一个文件或目录。行为:递归收集文件、按扩展名分派、读成带结构的正文。输出:装载结果(含跳过清单)。

    深度优先递归 + 按相对路径排序:``source`` 只看顶层目录名,子目录不再影响来源标签
    (``bagu_ai_data/lecture/01.md`` 的 source 仍是 ``bagu_ai``)。
    """
    if path.is_file():
        root, candidates = path.parent, [path]
    elif path.is_dir():
        root, candidates = path, [found for found in path.rglob("*") if found.is_file()]
    else:
        raise FileNotFoundError(f"路径不存在:{path}")

    name = root.name.removesuffix("_data") or root.name
    documents: list[Document] = []
    skipped: list[str] = []
    empty: list[str] = []

    for file in sorted(candidates, key=lambda found: found.relative_to(root).as_posix()):
        relative = file.relative_to(root).as_posix()
        source = _BY_SUFFIX.get(file.suffix.lower())
        if source is None:
            skipped.append(relative)
            continue
        # P0-5:**有页码能力的源就用它**(结构判据,不用 isinstance):
        # `read_with_pages` 不是端口成员(只有 PDF 有页码),所以这里不该写死类型。
        # 拿到的 (正文, 区间表) 与 read() 的正文逐字符一致,适配器那边有契约钉着。
        pages: tuple[tuple[int, int, int], ...] = ()
        reader = getattr(source, "read_with_pages", None)
        if reader is not None:
            text, pages = reader(file)
        else:
            text = source.read(file)
        if not text.strip():
            empty.append(relative)
            continue
        documents.append(Document(doc_id=relative, text=text, source=name, pages=pages))

    return LoadedCorpus(tuple(documents), tuple(skipped), tuple(empty))

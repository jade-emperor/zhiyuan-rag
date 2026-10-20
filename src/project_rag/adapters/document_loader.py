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

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from project_rag.domain import Document, DocumentImage
from project_rag.observability import get_logger
from project_rag.ports import DocumentSource, ImageStore

from .office_files import OfficeFileSource
from .pdf_files import PdfFileSource
from .text_files import TextFilesSource

_logger = get_logger("adapters.document_loader")

# 装配处:加一种格式 = 往这里加一个适配器(顺序只影响报错信息,分派按扩展名)
SOURCES: tuple[DocumentSource, ...] = (
    TextFilesSource(),
    OfficeFileSource(),
    PdfFileSource(),
)

_BY_SUFFIX: dict[str, DocumentSource] = {
    suffix: source for source in SOURCES for suffix in source.suffixes
}


def doc_id_of(root: Path, file: Path) -> str:
    """输入:入库根 + 根下的一个文件。行为:算它相对根的路径。输出:posix 相对路径(``doc_id`` 的口径)。"""
    return file.relative_to(root).as_posix()


def source_of(root: Path) -> str:
    """输入:入库根。行为:去掉 ``_data`` 后缀。输出:来源标签。

    ``bagu_ai_data/`` → ``bagu_ai``:顶层目录名去掉 ``_data``,可以直接当向量库的
    ``source`` 过滤字段用。**这个规则只此一处** —— 装载(``load_documents``)与磁盘索引
    (``list_files``)共用它,否则两边会各漂一个版本,而症状是"重新灌一遍块 id 全变了"。
    """
    return root.name.removesuffix("_data") or root.name


@dataclass(frozen=True, slots=True)
class DiskFile:
    """磁盘上的一个语料文件,**还没读正文**(清单/对账只要路径、大小、名字)。

    ``doc_ids`` 是「从各个可能的入库根看它」的候选 id,**从具体到宽泛**排 ——
    同一个文件既能被 ``--path data/corpus/bagu_ai_data`` 当 ``x.md`` 灌进去,
    也能被 ``--path data/corpus`` 当 ``bagu_ai_data/x.md`` 灌进去,
    两种登记行都该认出磁盘上这同一份文件(否则会有一边被误报成「文件不在」)。
    """

    path: Path
    size_bytes: int
    source: str  # 来源标签:第一个(最具体的)入库根的名字去掉 _data
    doc_ids: tuple[str, ...]

    @property
    def doc_id(self) -> str:
        """输入:无。行为:取首选的那个候选 id(最具体的入库根)。输出:doc_id。"""
        return self.doc_ids[0]


def list_files(root: Path, *, suffixes: Collection[str] | None = None) -> tuple[DiskFile, ...]:
    """输入:一个入库根 + 可选的后缀白名单。行为:递归列出文件(按相对路径排序)。输出:``DiskFile`` 元组。

    **只列不读**:清单与对账要知道"磁盘上有什么、多大",不需要正文 ——
    走 ``load_documents`` 会把 13MB 的 PDF 解析一遍,而那是分钟级的。

    ``suffixes=None`` = 全都收(调用方想自己过滤);给了白名单就只收白名单里的 ——
    用的是**同一个** ``supported_suffixes()``,别处不许再抄一份扩展名清单。

    **读不到的文件跳过并记一条 WARNING,不抛也不当成"不在"**:把"读不到"报成
    "文件不在",会让整个知识库清单误报一片 ``file_missing``(那比少列一个文件糟得多)。
    """
    if not root.is_dir():
        return ()
    found: list[DiskFile] = []
    for file in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            if not file.is_file():
                continue
            if suffixes is not None and file.suffix.lower() not in suffixes:
                continue
            size = file.stat().st_size
        except OSError as exc:  # 权限 / 半删状态 / 坏符号链接
            _logger.warning("列语料时读不到 %s,跳过(不当作「不在」):%s", file, exc)
            continue
        found.append(
            DiskFile(
                path=file,
                size_bytes=size,
                source=source_of(root),
                doc_ids=(doc_id_of(root, file),),
            )
        )
    return tuple(found)


def ingest_roots(uploads_dir: Path, corpus_dir: Path) -> tuple[Path, ...]:
    """输入:上传目录 + 语料根。行为:按**从具体到宽泛**给出入库根。输出:路径元组。

    入库根 = ``--path`` 可能指到的那些目录(``doc_id`` 是**相对入库根**的路径,
    所以"这个文件叫什么"完全取决于当时指向哪一层)。真实用到的只有这几种:

    1. 上传目录(``data/uploads``)—— 上传的原件,名字就是文件名;
    2. 语料根的**每个直接子目录**(``data/corpus/bagu_ai_data``)—— 按来源一份份灌;
    3. 语料根本身(``data/corpus``)—— 整个语料一次灌。

    顺序就是**优先级**:同一个文件有多个候选 id 时,取最具体的那个当 ``doc_id``
    (那也是 ``project-rag ingest --path data/corpus/<来源>_data`` 会写进登记表的那个)。
    从更深一层目录灌进去的 doc_id 对不上 —— 那种情况按「未登记」列出来,不会静默漏掉。
    """
    roots: list[Path] = [uploads_dir]
    if corpus_dir.is_dir():
        roots += sorted((child for child in corpus_dir.iterdir() if child.is_dir()),
                        key=lambda path: path.name)
        roots.append(corpus_dir)
    return tuple(roots)


def index_files(
    roots: Sequence[Path], *, suffixes: Collection[str] | None = None
) -> tuple[DiskFile, ...]:
    """输入:若干入库根(顺序即优先级)+ 可选后缀白名单。行为:合并成一份磁盘索引。输出:``DiskFile`` 元组。

    同一个文件被多个根看见时**合成一条**,``doc_ids`` 里按 ``roots`` 的顺序列全候选;
    多个文件抢同一个候选 id 时(两个子目录里有同名文件)**先出现的赢** ——
    按路径排序是确定的,所以两次调用给同一个答案(清单不能自己抖)。
    """
    merged: dict[Path, DiskFile] = {}
    for root in roots:
        for file in list_files(root, suffixes=suffixes):
            key = file.path.resolve()
            previous = merged.get(key)
            if previous is None:
                merged[key] = file
            else:
                merged[key] = DiskFile(
                    path=previous.path,
                    size_bytes=previous.size_bytes,
                    source=previous.source,
                    doc_ids=(*previous.doc_ids, *file.doc_ids),
                )
    return tuple(merged[key] for key in sorted(merged, key=lambda path: path.as_posix()))


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


def registration_anchor(file: Path, root: Path, corpus_root: Path | None) -> Path:
    """输入:一个文件、本次的入库根、语料根(可 None)。行为:算它的**登记根**。输出:一个目录。

    **为什么"登记根"不总是本次的入库根**:``project-rag ingest --path data/corpus`` 与
    ``--path data/corpus/bagu_ai_data`` 指的是同一批文件,但相对路径不同
    (``bagu_ai_data/x.md`` vs ``x.md``)—— 于是 ``doc_id`` 不同、``chunk_id`` 不同、
    ``source`` 也不同,同一份文档在库里变成两份(检索时还会被当成两条互相竞争的证据,
    界面上则是"两个同名文件")。

    规则只有一条:**文件在语料根下时,取它的一级子目录当登记根**。那正好是磁盘索引给出的
    首选候选 id(见 ``ingest_roots``),两条入库路径从此同口径;文件不在语料根下
    (上传目录、任意临时目录)时,仍然是本次的入库根 —— 老行为不变。
    """
    if corpus_root is not None:
        try:
            relative = file.resolve().relative_to(corpus_root.resolve())
        except (ValueError, OSError):
            relative = None
        if relative is not None and len(relative.parts) >= 2:
            return corpus_root / relative.parts[0]
    return root


def load_documents(
    path: Path,
    *,
    image_store: ImageStore | None = None,
    pdf_source: DocumentSource | None = None,
    image_min_bytes: int = 0,
    image_max_bytes: int = 0,
    image_max_per_doc: int = 0,
    corpus_root: Path | None = None,
) -> LoadedCorpus:
    """输入:一个文件或目录(+ 可选图片上传器、可选 PDF 解析器)。行为:递归收集、按扩展名分派、读成带结构的正文。输出:装载结果(含跳过清单)。

    深度优先递归 + 按相对路径排序:``source`` 只看顶层目录名,子目录不再影响来源标签
    (``corpus_alpha/notes/01.md`` 的 source 仍是 ``corpus_alpha``)。

    ``pdf_source`` 用来替换内置的 PyMuPDF 适配器(图片模态开启时换成 MinerU 那一份)。
    只用 ``isinstance(source, PdfFileSource)`` 判一次,别的格式不受影响。

    ``image_store`` 给了才上传图片并得到 ``DocumentImage.key``;不给时**图不落库**,
    但正文/页码照常装载(dry-run 与纯文本验收不需要 OSS)。给了却上传失败会原样抛,
    不静默丢图 —— 那是入库失败,管理员应该看到 ``failed``。

    ``image_min_bytes`` / ``image_max_bytes`` / ``image_max_per_doc`` 是图片的**资源闸**:
    太小的是图标,太大的是扫描整页,超过张数的先到先得。被闸掉的图记日志,
    不阻断正文入库 —— 图片是增强,不该让一份有 51 张图的 PDF 整份失败。
    """
    if path.is_file():
        root, candidates = path.parent, [path]
    elif path.is_dir():
        root, candidates = path, [found for found in path.rglob("*") if found.is_file()]
    else:
        raise FileNotFoundError(f"路径不存在:{path}")

    sources = (
        tuple(
            pdf_source if isinstance(source, PdfFileSource) else source
            for source in SOURCES
        )
        if pdf_source is not None
        else SOURCES
    )
    by_suffix = {suffix: source for source in sources for suffix in source.suffixes}

    documents: list[Document] = []
    skipped: list[str] = []
    empty: list[str] = []

    def anchor_of(found: Path) -> Path:
        """输入:一个候选文件。行为:算它的登记根(见 ``registration_anchor``)。输出:目录。"""
        return registration_anchor(found, root, corpus_root)

    for file in sorted(candidates, key=lambda found: doc_id_of(anchor_of(found), found)):
        anchor = anchor_of(file)
        relative = doc_id_of(anchor, file)
        name = source_of(anchor)
        source = by_suffix.get(file.suffix.lower())
        if source is None:
            skipped.append(relative)
            continue
        # **有图片/页码能力的源优先**:`read_with_assets` 是 MinerU 这类源的扩展,
        # 不是所有格式都有,所以用结构判据而不是写死类型。
        pages: tuple[tuple[int, int, int], ...] = ()
        images = ()
        rich_reader = getattr(source, "read_with_assets", None)
        if rich_reader is not None:
            parsed = rich_reader(file)
            text, pages = parsed.text, parsed.pages
            # 先判空再上传:空文档的图传上去只会变成没人引用的孤儿对象。
            if not text.strip():
                empty.append(relative)
                continue
            if parsed.images and image_store is None:
                _logger.warning(
                    "%s 解析出 %d 张图,但没装配图片上传器 —— 图片不会进库",
                    relative,
                    len(parsed.images),
                )
            uploaded = []
            if image_store is not None:
                for image in parsed.images:
                    if image_max_per_doc > 0 and len(uploaded) >= image_max_per_doc:
                        _logger.warning(
                            "%s 的图片超过上限 %d 张,后面的不再上传",
                            relative,
                            image_max_per_doc,
                        )
                        break
                    try:
                        size = image.path.stat().st_size
                    except OSError as exc:
                        _logger.warning("读不到图片大小,跳过 %s:%s", image.path, exc)
                        continue
                    if image_min_bytes > 0 and size < image_min_bytes:
                        _logger.info(
                            "%s 的图片只有 %d 字节(< %d),按小图标跳过",
                            relative,
                            size,
                            image_min_bytes,
                        )
                        continue
                    if image_max_bytes > 0 and size > image_max_bytes:
                        _logger.warning(
                            "%s 的图片 %d 字节超过上限 %d,跳过",
                            relative,
                            size,
                            image_max_bytes,
                        )
                        continue
                    key = image_store.put(image.path)
                    uploaded.append(
                        DocumentImage(
                            key=key,
                            offset=image.offset,
                            page=image.page,
                            alt=image.alt,
                        )
                    )
            images = tuple(uploaded)
        else:
            # `read_with_pages` 不是端口成员(只有 PDF 有页码),所以这里也不该写死类型。
            # 拿到的 (正文, 区间表) 与 read() 的正文逐字符一致,适配器那边有契约钉着。
            page_reader = getattr(source, "read_with_pages", None)
            if page_reader is not None:
                text, pages = page_reader(file)
            else:
                text = source.read(file)
        if not text.strip():
            empty.append(relative)
            continue
        documents.append(
            Document(
                doc_id=relative,
                text=text,
                source=name,
                pages=pages,
                images=images,
            )
        )

    return LoadedCorpus(tuple(documents), tuple(skipped), tuple(empty))

"""
ingest 子命令:把文档切块入库。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from project_rag.observability import get_logger

_logger = get_logger("cli.ingest")

NAME = "ingest"

# 默认语料路径
_DEFAULT_PATH = "data/corpus/pdf_data"

# 真实写入需要的两个工厂
_REQUIRED_FACTORIES = ("embedder_factory", "vector_store_factory")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 ingest,声明 --path / --dry-run / --batch-size。输出:该子命令的解析器。"""
    parser = sub.add_parser(NAME, help="把文档切块入库(先用 --dry-run 看切分质量)")
    parser.add_argument(
        "--path",
        default=_DEFAULT_PATH,
        help=f"文件或目录(相对项目根);默认 {_DEFAULT_PATH}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只加载 + 切分并打印统计,不加载模型、不写库",
    )
    parser.add_argument(
        "--purge-source",
        action="store_true",
        help="先把这些文档所属 source 的旧块全删掉(目录里有文件被删掉时用)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="每次嵌入的块数;不给就用 rag.ingest_batch_size(默认 64)",
    )
    return parser


def _resolve(path_text: str) -> Path:
    """输入:命令行给的路径。行为:相对路径挂到项目根下。输出:绝对路径。"""
    # 延迟 import:--help 不该为了一个常量把 pydantic / yaml 拖进来
    from project_rag.config import project_path

    return project_path(path_text)


def _print_chunk_stats(chunks, chunk_size: int, chunk_overlap: int) -> None:
    """输入:子块列表 + 配置尺寸。行为:打印块长分布与前两块预览。输出:无。"""
    lengths = [len(chunk.text) for chunk in chunks]
    parents = {chunk.parent_id for chunk in chunks}
    print(
        f"切分:{len(parents)} 个父块 → {len(chunks)} 个子块  "
        f"min={min(lengths)}  中位={int(statistics.median(lengths))}  "
        f"max={max(lengths)}  (child_size={chunk_size} overlap={chunk_overlap})"
    )
    print("前 2 块预览:")
    for index, chunk in enumerate(chunks[:2], start=1):
        preview = chunk.text[:60].replace("\n", "⏎")
        print(f"  [{index}] {preview}…")
        print(f"      id={chunk.chunk_id[:12]}…  source={chunk.source}")


def run(args) -> int:
    """输入:解析好的参数(含 deps)。行为:加载 → 切分;dry-run 到此为止,否则走嵌入与入库。输出:退出码 0/1。"""
    # 延迟 import:把 langchain 切分器挡在 --help 之外
    from project_rag.adapters.chunker_recursive import ChineseRecursiveChunker
    from project_rag.adapters.document_loader import load_documents, supported_suffixes
    from project_rag.application import chunk_documents, ingest_documents
    from project_rag.config import get_settings

    settings = get_settings()
    path = _resolve(args.path)

    if not path.exists():
        print(f"路径不存在:{path}", file=sys.stderr)
        return 1
    loaded = load_documents(path)
    # 两份「没进来」的清单都要报:少入库一份文档,不打印的话没人会发现
    for label, names in (("不认识的文件", loaded.skipped), ("没抽出文字", loaded.empty)):
        if names:
            preview = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
            print(f"{label} {len(names)} 份:{preview}", file=sys.stderr)
    if not loaded.documents:
        supported = "、".join(supported_suffixes())
        print(f"{path} 下没有可入库的文档(认识的后缀:{supported})", file=sys.stderr)
        return 1
    documents = loaded.documents

    total_chars = sum(len(document.text) for document in documents)
    print(f"加载 {len(documents)} 份文档,共 {total_chars} 字 ← {path}")

    chunker = ChineseRecursiveChunker(
        settings.rag.parent_chunk_size,
        settings.rag.child_chunk_size,
        settings.rag.chunk_overlap,
    )
    chunks = chunk_documents(documents, chunker)
    _print_chunk_stats(
        chunks, settings.rag.child_chunk_size, settings.rag.chunk_overlap
    )

    if args.dry_run:
        print("\n--dry-run:识别到这个参数,直接退出,不进行下面的入库操作")
        return 0

    deps = args.deps
    missing = [name for name in _REQUIRED_FACTORIES if getattr(deps, name) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:检查 build_real_deps 有没有装配嵌入模型与向量库;"
            "只想看切分质量就加 --dry-run",
            file=sys.stderr,
        )
        return 1

    # 只造一次向量库:两个工厂各调一次会开两个 Milvus 客户端
    store = deps.vector_store_factory()
    try:
        if args.purge_source:
            sources = sorted({document.source for document in documents})
            purged = sum(store.delete_by_source(source) for source in sources)
            print(f"--purge-source:清掉 {sources} 的旧块 {purged} 条")

        # None 才回落到配置:显式传 0 应当让 ingest_documents 抛 ValueError,
        # 而不是被 `or` 悄悄换成默认值(那是静默纠正用户的输入)
        batch_size = (
            settings.rag.ingest_batch_size
            if args.batch_size is None
            else args.batch_size
        )
        report = ingest_documents(
            documents,
            chunker=chunker,
            embedder=deps.embedder_factory(),
            store=store,
            batch_size=batch_size,
            # `keyword.backend=milvus` 那一档才装配稀疏编码器(见 cli/deps.py)。
            # 这一次 ingest 的正文就是"全库"(整份重灌),所以 corpus_texts 不传 ——
            # 传了反而会把口径搞成"统计的是别的语料"。
            sparse_encoder=(
                None if deps.sparse_encoder_factory is None else deps.sparse_encoder_factory()
            ),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 只给短消息,细节进日志
        _logger.error("ingest 失败:%s", exc)
        _logger.debug("ingest 失败详情:", exc_info=True)
        print(f"ingest 失败:{exc}", file=sys.stderr)
        return 1

    print(
        f"\n入库完成:文档 {report.documents} 份 → 切出 {report.chunks} 块 → "
        f"清旧 {report.removed} 块 → {report.batches} 批 → 库内 {report.in_store} 块"
    )
    if report.sparse_vocabulary:
        print(
            f"稀疏向量(关键字那一路):按全库 {report.chunks} 块统计,"
            f"词表 {report.sparse_vocabulary} 维,平均块长 {report.sparse_avg_len:.1f} 词"
        )

    # ── 登记回来:不然"后端灌的语料"在前端永远是「未登记」 ──────────────────────
    # 登记表是知识库页的骨架,而 ingest 以前**根本不碰它** —— CLI 灌进去的文档
    # 只能靠 `doctor --backfill` 或者被上传过一次才出现在清单上。
    # 这里写的是**这次真的入库了的结果**:块数(与刚写进库里的同口径)、磁盘真实字节数
    # (下次判"文件改没改"就靠它)、来源标签(与装载器同口径)。
    if deps.document_store_factory is None:
        print("提示:这次没接登记表(document_store_factory 未装配),清单上看不到这一批。")
        return 0
    from datetime import datetime

    from project_rag.adapters.document_loader import list_files, supported_suffixes
    from project_rag.application.documents import register_ingested

    # 大小按**同一个入库根**算(与 load_documents 的 doc_id 口径一致):
    # 单份文件时根是它所在目录,目录时根就是它自己。
    root = path if path.is_dir() else path.parent
    sizes = {
        file.doc_id: file.size_bytes
        for file in list_files(root, suffixes=supported_suffixes())
    }
    rows = register_ingested(
        deps.document_store_factory(),
        documents=documents,
        chunks=chunks,
        sizes=sizes,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    print(f"已登记 {len(rows)} 份(状态 processed + 块数 + 字节数),知识库清单上现在看得到它们。")
    unknown = [row.document_id for row in rows if row.size_bytes == 0]
    if unknown:
        print(
            f"⚠️ {len(unknown)} 份没算到磁盘大小({unknown[:3]}):登记表里大小是 0 "
            "= 判不了「文件后来改没改」,清单不会把它们报成 changed。",
            file=sys.stderr,
        )
    return 0

"""
ingest 子命令:把文档切块入库。

M3.4 阶段只有 ``--dry-run`` 能从头跑到尾(加载 + 切分 + 打印统计)——
真实写入要等 M3.5 的 Milvus 适配器与组合根装配,所以那条路径现在会 fail-fast 并说清楚原因,
而不是悄悄写进内存实现(那是「静默退回假实现」,本项目明令禁止)。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from project_rag.observability import get_logger

_logger = get_logger("cli.ingest")

NAME = "ingest"

# 默认语料:课程大纲(纯 Markdown,26K)—— M3.4 的首选,先跑通再谈多格式
_DEFAULT_PATH = "data/corpus/ai_data"

# 真实写入需要的两个工厂(M3.5 之前都是 None)
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
        default=64,
        help="每次嵌入的块数(CPU 上批量越大越划算,默认 64)",
    )
    return parser


def _resolve(path_text: str) -> Path:
    """输入:命令行给的路径。行为:相对路径挂到项目根下。输出:绝对路径。"""
    # 延迟 import:--help 不该为了一个常量把 pydantic / yaml 拖进来
    from project_rag.config import PROJECT_ROOT

    path = Path(path_text)
    return path if path.is_absolute() else PROJECT_ROOT / path


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
    from project_rag.adapters.text_files import load_text_files
    from project_rag.application import chunk_documents, ingest_documents
    from project_rag.config import get_settings

    settings = get_settings()
    path = _resolve(args.path)

    if not path.exists():
        print(f"路径不存在:{path}", file=sys.stderr)
        return 1
    documents = load_text_files(path)
    if not documents:
        print(f"{path} 下没有 .md / .txt 文件", file=sys.stderr)
        return 1

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
        print("\n--dry-run:没有加载模型、没有写库")
        return 0

    deps = args.deps
    missing = [name for name in _REQUIRED_FACTORIES if getattr(deps, name) is None]
    if missing:
        print(
            f"缺少依赖 {missing}:真实写入要等 M3.5 的 Milvus 适配器与组合根装配;"
            "现在请用 --dry-run 看切分质量",
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

        report = ingest_documents(
            documents,
            chunker=chunker,
            embedder=deps.embedder_factory(),
            store=store,
            batch_size=args.batch_size,
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
    return 0

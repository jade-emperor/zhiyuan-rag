"""
collections 子命令:列出库里有哪些向量集合,各自多少块、有没有 ``page`` / 稀疏列、稠密索引是哪一种。

**为什么要有它**:``page`` 列让"哪个集合能用"变成了一个必须先看一眼的问题 ——
往没有这一列的集合写会被**拒收**(见 ``milvus_store._require_page_column``),而删集合是破坏性动作。
先把现状列清楚,再决定切哪个、删哪个。

``--reindex`` 是它的第二个用途:索引类型(**FLAT / IVF_FLAT**)是建集合那一刻定格的,
改了 ``milvus.index.type`` 不会自动生效 —— 用这个开关按当前配置**重建索引**。
数据不动(索引是数据之上的结构),所以不用重灌;代价是重建期间这一路检索不可用。
"""

from __future__ import annotations

import argparse
import sys

NAME = "collections"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 collections(可选 --reindex)。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="列出向量集合(块数 / page 列 / 稀疏列 / 索引类型);--reindex 按配置重建索引"
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="按当前配置重建**当前集合**的索引(FLAT ⇄ IVF_FLAT、改 nlist);数据不动",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="与 --reindex 搭配:真的执行(不带它只打印会做什么,然后退出码 1)",
    )
    return parser


def _reindex(args) -> int:
    """输入:解析好的参数。行为:打印计划(默认)或按配置重建当前集合的索引。输出:退出码。"""
    from project_rag.config import get_settings

    settings = get_settings()
    index = settings.milvus.index
    target = f"{index.type}" + (
        f"(nlist={index.nlist}, 检索时 nprobe={index.nprobe})"
        if index.type == "IVF_FLAT"
        else "(精确检索,无参数)"
    )
    print(f"当前集合:{settings.milvus.collection}")
    print(f"配置里的稠密索引:{target}")
    if not args.yes:
        print(
            "\n这会**先删掉集合上已有的索引再按配置重建**(数据不动,但重建期间检索不可用)。\n"
            "确认无误后加 --yes 真跑。"
        )
        return 1

    deps = args.deps
    if deps.vector_store_factory is None:
        print(
            "collections --reindex 需要向量库(deps 没装配 vector_store_factory)",
            file=sys.stderr,
        )
        return 1
    store = deps.vector_store_factory()
    try:
        steps = store.rebuild_index()
    except Exception as exc:  # noqa: BLE001 - 连不上/集合不在时给出能照着查的错
        print(f"重建索引失败({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    for step in steps:
        print(f"  ✔ {step}")
    return 0


def run(args) -> int:
    """输入:解析好的参数(含 args.deps)。行为:列集合或重建索引。输出:退出码。"""
    from project_rag.config import get_settings

    if getattr(args, "reindex", False):
        return _reindex(args)

    deps = args.deps
    if deps.collections_factory is None:
        print("collections 需要向量库(deps 没装配 collections_factory)", file=sys.stderr)
        return 1
    try:
        infos = deps.collections_factory()
    except Exception as exc:  # noqa: BLE001 - 连不上库时给出能照着查的错
        print(f"列不出集合({type(exc).__name__}): {exc}", file=sys.stderr)
        print("提示:先确认容器起没起(docker compose ps)。", file=sys.stderr)
        return 1

    current = get_settings().milvus.collection
    if not infos:
        print(f"库里一个集合都没有(当前配置指向 {current};第一次 ingest 会把它建出来)")
        return 0
    print(f"当前配置指向:{current}\n")
    for info in infos:
        if info.error:
            print(f"  {info.name:<24} {'—':>7}     读不出来:{info.error}")
            continue
        rows = f"{info.rows}{'+' if info.capped else ''}"
        mark = "  ← 当前配置" if info.name == current else ""
        page = "page ✔" if info.has_page else "缺 page(写进去会被拒)"
        sparse = "稀疏列 ✔" if info.has_sparse else "无稀疏列"
        index = info.index_type + (f"({info.index_params})" if info.index_params else "")
        print(
            f"  {info.name:<24} {rows:>7} 块   dim={info.dim}   {page}   {sparse}   索引={index}{mark}"
        )
    print(
        "\n切集合:改 config 里 milvus.collection 那一行(或用 RAG_MILVUS__COLLECTION 临时覆盖);"
        "\n换索引:改 milvus.index.type 之后 `project-rag collections --reindex --yes`(数据不动);"
        "\n删集合:project-rag drop --collection <名字> --yes"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 只为直接跑本文件时可用
    from project_rag.cli.main import cli

    raise SystemExit(cli([NAME]))

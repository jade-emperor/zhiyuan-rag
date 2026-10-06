"""
collections 子命令:列出库里有哪些向量集合,各自多少块、有没有 ``page`` 列。

**为什么要有它**(P0-5 之后):``page`` 列让"哪个集合能用"变成了一个必须先看一眼的问题 ——
往没有这一列的集合写会被**拒收**(见 ``milvus_store._require_page_column``),而删集合是破坏性动作。
先把现状列清楚,再决定切哪个、删哪个。

纯读:一个查询都不改。
"""

from __future__ import annotations

import argparse
import sys

NAME = "collections"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 collections(无参数)。输出:该子命令的解析器。"""
    return sub.add_parser(NAME, help="列出向量集合(块数 / 有没有 page 列 / 当前配置指向哪个)")


def run(args) -> int:
    """输入:解析好的参数(含 args.deps)。行为:列集合。输出:退出码。"""
    from project_rag.config import get_settings

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
        rows = f"{info.rows}{'+' if info.capped else ''}"
        mark = "  ← 当前配置" if info.name == current else ""
        page = "有 page 列 ✔" if info.has_page else "缺 page 列(写进去会被拒)"
        print(f"  {info.name:<24} {rows:>7} 块   dim={info.dim}   {page}{mark}")
    print(
        "\n切集合:改 config 里 milvus.collection 那一行(或用 RAG_MILVUS__COLLECTION 临时覆盖);"
        "\n删集合:project-rag drop --collection <名字> --yes"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 只为直接跑本文件时可用
    from project_rag.cli.main import cli

    raise SystemExit(cli([NAME]))

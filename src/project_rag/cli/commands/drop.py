"""
drop 子命令:删掉**一个向量集合**(不是删文档 —— 那个走 `doctor --forget-missing` 或 API)。

**默认只说不删**:不给 ``--yes`` 时只把"会删掉什么"打出来,然后返回 1 ——
脚本里不会把"我拒绝了"误读成"删成功了"。

**为什么单独一个命令、而不是 doctor 的开关**:doctor 管的是"登记表 vs 向量库"的对账,
这里删的是**集合本身**(P0-5 迁移时把老集合清掉)。破坏面差一个量级,混在一起会误导。

语料还在磁盘上:删掉集合之后 `project-rag ingest --path data/corpus/<source>_data`
能把它重新灌回来(CPU 上 PDF 那批约 8 分钟)。
"""

from __future__ import annotations

import argparse
import sys

NAME = "drop"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 drop,声明 --collection / --yes。输出:该子命令的解析器。"""
    parser = sub.add_parser(NAME, help="删掉一个向量集合(默认只说不删;要 --yes)")
    parser.add_argument(
        "--collection",
        required=True,
        metavar="名字",
        help="要删的集合名(先 `project-rag collections` 看有哪些)",
    )
    parser.add_argument("--yes", action="store_true", help="确认删除(不可恢复)")
    return parser


def run(args) -> int:
    """输入:解析好的参数(含 args.deps)。行为:**先报会删什么**,给了 --yes 才真删。输出:退出码。"""
    from project_rag.config import get_settings

    deps = args.deps
    name = args.collection
    if deps.collections_factory is None or deps.drop_collection_factory is None:
        print("drop 需要向量库(deps 没装配 collections/drop 工厂)", file=sys.stderr)
        return 1
    try:
        infos = {info.name: info for info in deps.collections_factory()}
    except Exception as exc:  # noqa: BLE001
        print(f"读不到集合清单({type(exc).__name__}): {exc}", file=sys.stderr)
        print("提示:先确认容器起没起(docker compose ps)。", file=sys.stderr)
        return 1

    info = infos.get(name)
    if info is None:
        print(f"没有集合 {name!r}(什么都没删)。现有:{sorted(infos) or '(空)'}", file=sys.stderr)
        return 1

    current = get_settings().milvus.collection
    rows = f"{info.rows}{'+' if info.capped else ''}"
    print(f"集合 {name}:{rows} 块,{'有' if info.has_page else '没有'} page 列")
    if name == current:
        print("  ⚠️ 这正是当前配置指向的集合 —— 删完之后 ingest 会重新建一个空的(检索会查不到东西)")
    if not args.yes:
        print(f"什么都没删。确认要删就加 --yes:project-rag drop --collection {name} --yes")
        return 1

    removed = deps.drop_collection_factory(name)
    print(f"已删掉集合 {name}(删前 {removed} 块)。删的是集合本身、不可恢复;语料还在磁盘上,重灌即可回来。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 只为直接跑本文件时可用
    from project_rag.cli.main import cli

    raise SystemExit(cli([NAME]))

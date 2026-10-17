"""doctor 子命令:对账 —— 登记表 vs 向量库,找孤儿块与缺块。

**默认只读**。三个会写库的动作,都必须**显式**给:

* ``--backfill``:给登记表出现之前就入库的老语料补登记行 —— 只**新增**,不删任何东西;
* ``--prune-orphans``:删掉**孤儿块**(库里有、登记表没有)。它们永远不会被任何一次删除清掉,
  而且照样会被检索到 —— 用户会看到"库里有这个内容,但清单里没有这份文件";
* ``--forget-missing``:删掉**缺块**的登记行(登记 ``processed``、库里一块没有)。语料还在磁盘上时
  该选重灌(内容能回来);语料已经不在(上传的临时文件被清掉、测试残留)时才删行。

**为什么叫 doctor 而不是 fix**:它只诊断,清理动作由人**点名**才做。自动"修复"孤儿块 = 自动删数据,
而自动删行会让「为什么会有孤儿/缺块」这个真问题跟着一起消失。**两个清理都只动已经列出来的那些
doc_id** —— "谁该清"只由 ``reconcile`` 判定一次,这里不再自己判一次。

**退出码**:有问题返回 1 —— 这样它可以进 cron / CI,不必有人盯着输出看;清理之后按**清理后的**
状态给码(清干净了就是 0)。
"""

from __future__ import annotations

import argparse
import sys

from project_rag.observability import get_logger

_logger = get_logger("cli.doctor")

NAME = "doctor"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 doctor 与它的三个动作开关。输出:该子命令的解析器。"""
    parser = sub.add_parser(
        NAME, help="对账:登记表 vs 向量库(找孤儿块与缺块);有问题退出码为 1"
    )
    parser.add_argument("--all", action="store_true", help="孤儿/缺块超过 20 条时也全部列出")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="给孤儿块补登记行(状态 processed)。只新增,不删任何东西;老语料首跑就该这么做",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="删掉库里的孤儿块(库里有、登记表没有的 doc_id)。删哪些由本次对账的清单决定",
    )
    parser.add_argument(
        "--forget-missing",
        action="store_true",
        help="删掉缺块的登记行(登记 processed、库里一块没有)。语料还在就改用它重灌",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    """输入:解析好的参数(含 args.deps)。行为:读两边、对账、打印。输出:退出码。"""
    from collections import Counter

    from project_rag.application.documents import list_documents
    from project_rag.application.reconcile import reconcile

    deps = args.deps
    if deps.document_store_factory is None or deps.vector_store_factory is None:
        print("doctor 需要文档登记表与向量库(deps 没装配)", file=sys.stderr)
        return 1

    try:
        records = list_documents(deps.document_store_factory())
        chunks = deps.vector_store_factory().load_all()
    except Exception as exc:  # noqa: BLE001 - 连不上库时给出能照着查的错
        print(f"对账读不到数据({type(exc).__name__}): {exc}", file=sys.stderr)
        print("提示:先确认容器起没起(docker compose ps)。", file=sys.stderr)
        return 1

    counts = Counter(chunk.doc_id for chunk in chunks)
    report = reconcile(records, counts)

    print(
        f"登记 {report.registered} 份 ｜ 库里有块的 doc_id {report.indexed_doc_ids} 个"
        f" ｜ 两边都对得上 {report.ok} 份 ｜ 库内块 {sum(counts.values())}"
    )
    limit = None if args.all else 20
    for title, items, hint in (
        (
            "孤儿块(库里有、登记表没有 —— 它们永远不会被任何一次删除清掉,而且照样会被检索到)",
            report.orphans,
            "成因:ID 口径不一致 / 登记行被手工删了 / 删除只删了行。用 `project-rag ingest` 重灌对应来源,"
            "或按 doc_id 清库(先确认它确实该走)",
        ),
        (
            "缺块(登记为 processed,库里一块没有 —— 前端显示已处理但搜不到)",
            report.missing,
            "成因:入库失败后状态没改 / 有人手工清了库。删掉登记行重传,或修好原因后重灌",
        ),
    ):
        if not items:
            continue
        shown = items if limit is None else items[:limit]
        print(f"\n{title}:{len(items)} 个")
        for name in shown:
            print(f"  - {name}")
        if len(shown) < len(items):
            print(f"  …还有 {len(items) - len(shown)} 个(加 --all 全列)")
        print(f"  {hint}")

    if args.backfill and report.orphans:
        from datetime import datetime

        from project_rag.application.reconcile import backfill_records

        grouped: dict[str, tuple[str, int]] = {}
        for chunk in chunks:
            if chunk.doc_id in set(report.orphans):
                grouped.setdefault(chunk.doc_id, (chunk.source, 0))
                grouped[chunk.doc_id] = (chunk.source, counts[chunk.doc_id])
        records_to_add = backfill_records(
            grouped, created_at=datetime.now().isoformat(timespec="seconds")
        )
        store = deps.document_store_factory()
        added = 0
        for record in records_to_add:
            try:
                store.add(record)
                added += 1
            except ValueError as exc:  # 已登记过(并发跑了两遍 / 手工补过)
                print(f"  跳过 {record.document_id}:{exc}")
        print(f"\n已补登记 {added} 份(状态 processed,size_bytes=0 因为当年没记大小)")
        print("再跑一次 `project-rag doctor` 应该是干净的。")

    # ── 两个显式的清理动作 ─────────────────────────────────────────────
    # 都用**本次对账已经读到的数据**算出清理后的状态,不再回库读第二遍:
    # 那一遍既慢,又会和刚打印的清单对不上(中间有人在写库时更明显)。
    if args.prune_orphans:
        from project_rag.application.reconcile import prune_orphans

        if not report.orphans:
            print("\n--prune-orphans:没有孤儿块,什么都没删")
        else:
            removed = prune_orphans(deps.vector_store_factory(), report.orphans)
            total = sum(count for _doc_id, count in removed)
            print(f"\n--prune-orphans:删掉 {len(removed)} 个孤儿 doc_id 的 {total} 块")
            for doc_id, count in removed:
                print(f"  - {doc_id}:{count} 块")
            if any(count == 0 for _doc_id, count in removed):
                print(
                    "  ⚠️ 有 doc_id 一块都没删到 —— 这次对账与刚才的删除之间有人在写库,"
                    "重跑一次 doctor 看看"
                )
            gone = set(report.orphans)
            chunks = [chunk for chunk in chunks if chunk.doc_id not in gone]
            counts = Counter(chunk.doc_id for chunk in chunks)
            report = reconcile(records, counts)

    if args.forget_missing:
        from project_rag.application.reconcile import forget_missing

        if not report.missing:
            print("\n--forget-missing:没有缺块的登记行,什么都没删")
        else:
            rows = forget_missing(deps.document_store_factory(), report.missing)
            total = sum(count for _doc_id, count in rows)
            print(f"\n--forget-missing:删掉 {len(rows)} 条登记行({total} 行)")
            for doc_id, count in rows:
                print(f"  - {doc_id}:{'已删' if count else '没删到(刚才已被别人删了?)'}")
            gone = set(report.missing)
            records = [record for record in records if record.document_id not in gone]
            report = reconcile(records, counts)

    if args.prune_orphans or args.forget_missing:
        print(
            f"\n清理后:登记 {report.registered} 份 ｜ 库里有块的 doc_id {report.indexed_doc_ids} 个"
            f" ｜ 两边都对得上 {report.ok} 份 ｜ 库内块 {sum(counts.values())}"
        )

    if report.healthy:
        print("\n两边一致 ✔")
        return 0
    print("\n对账不通过 —— 上面两类都要人工看一眼(不自动修:自动修复 = 自动删数据)")
    return 1

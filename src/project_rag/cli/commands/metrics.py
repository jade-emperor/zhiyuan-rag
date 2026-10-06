"""metrics 子命令:读账本表,把延迟收成 p50/p95/p99 + 比率 + 最慢几条。

**为什么不直接 grep 日志**:日志是"一次回答一行",回答不了"这周 p95 多少";它还会滚动,
还会混进别人的记录。报表要的是聚合,而聚合必须有口径 —— 口径写在
``application/metrics.py`` 的 docstring 里,由 ``check_m5_9_metrics.py`` 钉住。

**为什么每行都印 n、样本不够就印 ``-``**:分位数没有样本量就是耍流氓。
上一轮就是拿 28 条样本读 p95,把一个冷启动噪声当成了"系统很慢"。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from project_rag.observability import get_logger

_logger = get_logger("cli.metrics")

NAME = "metrics"

# 窗口名 → 小时数。用白名单而不是随便传数字:报表的窗口是要能互相对比的。
_WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 metrics,声明 --window / --top / --segment。输出:解析器。"""
    parser = sub.add_parser(NAME, help="延迟报表:从账本表聚合 p50/p95/p99、比率与最慢几条")
    parser.add_argument(
        "--window",
        default="24h",
        choices=tuple(_WINDOWS),
        help="时间窗(默认 24h);窗口越短样本越少,样本不够 p95/p99 会显示 -",
    )
    parser.add_argument("--top", type=int, default=5, help="最慢榜取几条(默认 5)")
    parser.add_argument(
        "--segment",
        action="append",
        default=None,
        help="只看这些分段(可重复,例如 --segment rerank_ms);默认全部",
    )
    return parser


def _ms(value: float | None) -> str:
    """输入:毫秒或 None。行为:渲染成一位小数。输出:文本;None 给 ``-``(不编数)。"""
    return "-" if value is None else f"{value:.1f}"


def run(args: argparse.Namespace) -> int:
    """输入:解析好的参数(含 args.deps)。行为:读窗口内的账本行并打印报表。输出:退出码。"""
    from project_rag.application.metrics import DEFAULT_SEGMENTS, summarize

    deps = args.deps
    if deps.ledger_store_factory is None:
        print("没有接上账本存储(ledger_store_factory 为空)", file=sys.stderr)
        return 1

    try:
        store = deps.ledger_store_factory()
        since = datetime.now() - timedelta(hours=_WINDOWS[args.window])
        rows = store.load_since(since)
    except Exception as exc:  # noqa: BLE001 - 报表读不到库,给出能照着查的错,而不是栈
        print(f"读账本失败({type(exc).__name__}): {exc}", file=sys.stderr)
        print("提示:账本表由启动时的 attach_ledger 自动建;连不上 MySQL 先看容器起没起。", file=sys.stderr)
        return 1

    summary = summarize(
        rows,
        window=args.window,
        segments=args.segment or DEFAULT_SEGMENTS,
        top_n=args.top,
    )

    rate = (lambda n: f"{n / summary.rows:.1%}") if summary.rows else (lambda n: "-")
    print(
        f"窗口 {summary.window} ｜ 账本 {summary.rows} 行"
        f"（冷 {summary.cold_rows} / 热 {summary.warm_rows}）"
    )
    print(
        f"  拒答 {summary.refused}（{rate(summary.refused)}）"
        f" ｜ 截断 {summary.truncated}（{rate(summary.truncated)}）"
        f" ｜ 中断 {summary.aborted}（{rate(summary.aborted)}）"
    )
    print(
        f"  通道：FAQ 直答 {summary.faq_hits} ｜ 知识库 {summary.rag_answers}"
        f"（其余是未定通道/异常，共 {summary.rows - summary.faq_hits - summary.rag_answers}）"
    )
    if summary.rows == 0:
        print("\n这个窗口里没有账 —— 先跑一次 `project-rag qa -q \"…\"` 或打几个请求。")
        return 0

    print(f"\n{'分段':<14}{'n':>6}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}   (毫秒;热行)")
    for seg in summary.segments:
        print(
            f"{seg.name:<14}{seg.n:>6}{_ms(seg.p50):>10}{_ms(seg.p95):>10}"
            f"{_ms(seg.p99):>10}{_ms(seg.maximum):>10}"
        )
    print("\n最慢几条(**含冷启动** —— 那是用户真实等到的):")
    for item in summary.slowest:
        cold = "冷" if item.cold else "热"
        print(f"  {item.request_id:<14}{item.channel:<6}{cold}  {item.total_ms:>9.1f} ms")
    print(
        "\np95/p99 样本不够时是 `-`（要 20 / 100 条）；样本量看 n 那一列。"
        "冷启动那几行不计入分段统计（它们被模型加载主导），单独数在冷/热里。"
    )
    return 0

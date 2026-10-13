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
from datetime import UTC, datetime, timedelta

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
        # 窗口下界给**带时区的绝对时刻**:账本表里存的是 UTC,宿主(CST)与容器(UTC)
        # 上的 `datetime.now()` 不是同一串数字,给裸钟点要靠存储层猜。
        since = datetime.now(UTC) - timedelta(hours=_WINDOWS[args.window])
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
    _print_cost(args, rows)
    return 0


def _print_cost(args: argparse.Namespace, rows) -> None:  # noqa: ANN001 - rows 是账本行序列
    """输入:命令行参数(含 deps)+ 已经读回来的账本行。行为:把消耗那一段打印出来。输出:无。

    **与延迟共用同一批行**:再读一次库会出现"延迟说 30 次调用、消耗说 42 次"这种对不上的画面,
    而那种不一致会让人怀疑两个数都是错的。

    **口径全在 ``application/cost.py``**(哪些行算调了模型、没配单价怎么显示),
    这一层只负责把它印成人能读的几行。
    """
    from project_rag.application.cost import summarize_cost

    deps = args.deps
    report = summarize_cost(
        rows,
        prices=deps.cost_prices,
        currency=deps.cost_currency,
        window=args.window,
        # **时段规则必须传下去**:不传的话所有调用都按高峰价算(贵一倍),
        # 而报表上只会显示一个"合理"的金额 —— 这条有契约盯着(CLI 与 HTTP 同口径)。
        rule=deps.cost_rule,
    )
    print(f"\n消耗（窗口 {report.window}；币种 {report.currency}）")
    print(
        f"  模型调用 {report.metered_calls} 次（量到 token）"
        f" ｜ 未量到 {report.unmetered_calls} 次"
    )
    print(
        f"  token：输入 {report.input_tokens}"
        f"（其中命中缓存 {report.cached_input_tokens}）"
        f" ｜ 输出 {report.output_tokens} ｜ 合计 {report.total_tokens}"
    )
    if deps.cost_rule.split:
        windows = "、".join(
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
            for start, end in deps.cost_rule.windows
        )
        days = "".join(str(day) for day in sorted(deps.cost_rule.weekdays)) or "每天"
        print(f"  时段口径：北京时间 星期{days} 的 {windows} 为高峰；法定节假日全天空闲")
    if not report.lines:
        print("  窗口内没有可算的模型调用（FAQ 命中与检索为空都不调模型）。")
        return
    print(
        f"\n  {'模型':<24}{'时段':<10}{'调用':>6}{'输入':>12}{'命中缓存':>10}"
        f"{'输出':>10}{'金额':>14}"
    )
    for line in report.lines:
        money = "未配置单价" if line.total_cost is None else f"{line.total_cost:.6f}"
        period = "空闲" if line.period == "off_peak" else "高峰"
        print(
            f"  {line.model:<24}{period:<10}{line.calls:>6}{line.input_tokens:>12}"
            f"{line.cached_input_tokens:>10}{line.output_tokens:>10}{money:>14}"
        )
    for item in report.periods:
        period = "空闲" if item.period == "off_peak" else "高峰"
        print(
            f"  {'':<24}{period + '合计':<10}{item.calls:>6}{item.input_tokens:>12}"
            f"{'':>10}{item.output_tokens:>10}{item.total_cost:>14.6f}"
        )
    print(f"\n  合计 {report.total_cost:.6f} {report.currency}")
    if report.unmetered_calls:
        print(
            f"  ⚠️ 有 {report.unmetered_calls} 次调用没量到 token（provider 没回报用量,"
            "或这一轮中途断了）—— 这部分**没有算进金额**。"
        )
    if report.period_unknown_calls:
        print(
            f"  ⚠️ 有 {report.period_unknown_calls} 次读不到发生时间,按**高峰价**计"
            "（偏保守,不会少报）。"
        )
    if report.zone_unknown_calls:
        print(
            f"  ⚠️ 有 {report.zone_unknown_calls} 次的时间戳**没带时区**（老账,或后端镜像还没重建）,"
            "判不出高峰/空闲,一律按**高峰价**计 —— 这部分是上界。"
        )
    if report.calendar_unknown_calls:
        print(
            f"  ⚠️ 有 {report.calendar_unknown_calls} 次落在那一年**没填节假日**的日子里,按**高峰价**计"
            "（宁可贵一点;把那一年的放假通知填进 config.yaml 的 cost.holidays 就能判准）。"
        )
    if report.cache_unknown_calls:
        print(
            f"  ⚠️ 有 {report.cache_unknown_calls} 次没回报缓存命中数,整段输入按**未命中价**计"
            "（这是上界,实际可能更低）。"
        )
    if report.unpriced_models:
        print(
            f"  ⚠️ 这些模型没配单价:{'、'.join(report.unpriced_models)}"
            " —— 把名字填进 config.yaml 的 cost.models 就有金额了。"
        )

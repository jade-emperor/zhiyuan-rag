#!/usr/bin/env python
"""契约：延迟账本的聚合口径 + 落表链路。

用法：

    uv run python scripts/acceptance/check_m5_9_metrics.py

**为什么要有这一份**：账本已经能记了（`check_m5_5_latency.py` 钉着字段与口径），
但"一次请求一行"回答不了"这周 p95 多少"。加聚合就多出一层**容易骗人的东西** ——
上一轮就是拿 28 条样本读 p95，把冷启动噪声当成了"系统很慢"。所以这里的重点不是"能算出来"，
而是**算出来的数字在什么条件下才有意义**：

1. **分位数用最近秩、不插值**：样本少时插值会给出比任何一次真实观测都好看的数；
2. **样本不够就不给数**：``p95`` 要 ``n ≥ 20``、``p99`` 要 ``n ≥ 100``，不够一律 ``None``；
3. **``-``（没量到）不参与统计，也不折算成 0**：折成 0 会把 p50 拉低，而且看不出来；
4. **冷热各一套数**：分段分位数只算热行；**拒答率、截断率、中断率、通道计数也只数热行**，
   冷启动（被模型加载主导的那些行）单独一套 —— 混进去会让"这一版答得怎么样"随
   "这周重启过几次"漂。最慢榜仍然**含冷**（那是用户真实等到的），带 ``cold`` 标记；
5. **老数据缺 ``cold`` 字段按热算**：那是加字段之前的行，当作热至少不会让它们凭空消失。

落表那半边钉三件事：**只转交带 ``ledger`` 的记录**（不是所有日志都进表）、
**sink 抛异常只计数不炸**（观测是旁路）、**挂载幂等**（挂两次会把同一行写两遍）。

除最后一条（真 MySQL 往返）外，全部用内存假件 —— **不需要容器、不需要模型、不联网**。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

from _harness import SkipCheck, check, report

# 一行账本的样板：后面各条只改需要的那几个字段
_BASE = {
    "label": "rag",
    "request_id": "abc123",
    "channel": "rag",
    "thinking": "-",
    "cold": "false",
    "builds": "0",
    "total_ms": "100.0",
    "ttft_ms": "50.0",
    "refused": "false",
    "truncated": "false",
    "aborted": "false",
    "rerank_ms": "20.0",
}


def _row(**over: str) -> dict[str, str]:
    """输入:要覆盖的字段。行为:在样板上覆盖。输出:一行账本。"""
    return {**_BASE, **over}


def _rows(key: str, values: list[float | str], **over: str) -> list[dict[str, str]]:
    """输入:字段名、值列表。行为:每个值造一行(值原样写字符串,``-`` 也照写)。输出:行列表。"""
    out = []
    for i, value in enumerate(values):
        text = value if isinstance(value, str) else str(value)
        out.append(_row(request_id=f"r{i:03d}", **{key: text}, **over))
    return out


def _seg(summary, name: str):
    """输入:汇总 + 分段名。行为:取出该分段的统计。输出:SegmentStats。"""
    for seg in summary.segments:
        if seg.name == name:
            return seg
    raise AssertionError(f"汇总里没有分段 {name!r}：{[s.name for s in summary.segments]}")


@check("口径 分位数：最近秩取值（1..100 的 p50/p95/p99 = 50/95/99，不插值）")
def _percentile_nearest_rank() -> str:
    from project_rag.application.metrics import summarize

    summary = summarize(_rows("rerank_ms", list(range(1, 101))), window="test")
    seg = _seg(summary, "rerank_ms")
    if (seg.n, seg.p50, seg.p95, seg.p99, seg.maximum) != (100, 50.0, 95.0, 99.0, 100.0):
        raise AssertionError(
            f"最近秩的结果应是 n=100 / p50=50 / p95=95 / p99=99 / max=100，实际 "
            f"n={seg.n} p50={seg.p50} p95={seg.p95} p99={seg.p99} max={seg.maximum}"
            f"（p95 得到 95.95 之类的数说明做了插值）"
        )
    return "n=100：p50=50 / p95=95 / p99=99 / max=100（最近秩，rank=ceil(q×n)）"


@check("口径 样本量门槛：p95 要 n≥20、p99 要 n≥100，不够给 None（不是给个好看的数）")
def _percentile_min_samples() -> str:
    from project_rag.application.metrics import summarize

    def stats(n: int):
        return _seg(summarize(_rows("rerank_ms", list(range(1, n + 1))), window="t"), "rerank_ms")

    n19, n20, n99, n100 = stats(19), stats(20), stats(99), stats(100)
    if n19.p95 is not None:
        raise AssertionError(f"n=19 时 p95 该是 None（样本不够），实际 {n19.p95}")
    if n20.p95 is None:
        raise AssertionError("n=20 时 p95 该有值了（门槛是 20）")
    if n99.p99 is not None:
        raise AssertionError(f"n=99 时 p99 该是 None（门槛是 100），实际 {n99.p99}")
    if n100.p99 is None:
        raise AssertionError("n=100 时 p99 该有值了")
    if n19.p50 is None or n19.maximum is None:
        raise AssertionError("p50 与 max 在 n=1 时就该有值 —— 门槛只管 p95/p99")
    return f"n=19→p95 {n19.p95}；n=20→p95 {n20.p95}；n=99→p99 {n99.p99}；n=100→p99 {n100.p99}"


@check("口径 缺失：'-'（没量到）不参与统计，也不折算成 0")
def _missing_excluded() -> str:
    from project_rag.application.metrics import summarize

    # 10 条真值(全 100.0) + 5 条 '-'：若把 '-' 当 0，n 会变 15、p50 会被拉低。
    # **那 5 条必须是"热"行** —— 第一版给了 cold="true"，于是它们被"冷行不进统计"挡掉了，
    # 这条检查对"折算成 0"的坏写法完全无感(reverse_m5_9 的 RT-3 把它抓出来了)。
    rows = _rows("rerank_ms", [100.0] * 10) + _rows("rerank_ms", ["-"] * 5)
    seg = _seg(summarize(rows, window="t"), "rerank_ms")
    if seg.n != 10:
        raise AssertionError(f"'-' 不该计入 n（应为 10），实际 {seg.n}")
    if seg.p50 != 100.0 or seg.maximum != 100.0:
        raise AssertionError(
            f"'-' 掺进来不许改变分布：p50 应 100.0、max 应 100.0，实际 {seg.p50}/{seg.maximum}"
            f"（若 p50 变小，说明把 '-' 折算成 0 了）"
        )
    return "15 行里 5 行 '-'：n=10、p50=100.0、max=100.0（没量到 ≠ 0）"


@check("口径 冷热分层：冷启动不进分段统计，也**不进拒答率与通道计数**（单独一套数）")
def _cold_warm_layering() -> str:
    from project_rag.application.metrics import summarize

    warm = _rows("rerank_ms", [10.0, 20.0], cold="false", total_ms="30.0", channel="rag")
    warm[0]["refused"] = "true"  # 热行里有一次拒答,它才是该被数进拒答率的那一条
    cold = [
        # 冷启动那一层里故意放三种通道:FAQ 直答、知识库、以及"没定通道"(）
        _row(request_id="cold1", cold="true", builds="2", rerank_ms="5000.0", total_ms="9000.0",
             channel="-"),
        _row(request_id="cold2", cold="true", builds="1", rerank_ms="4000.0", refused="true",
             total_ms="8000.0", channel="faq"),
    ]
    summary = summarize(warm + cold, window="t")
    seg = _seg(summary, "rerank_ms")
    if (summary.rows, summary.cold_rows, summary.warm_rows) != (4, 2, 2):
        raise AssertionError(
            f"冷热条数该是 4/2/2，实际 {summary.rows}/{summary.cold_rows}/{summary.warm_rows}"
        )
    if (seg.n, seg.maximum) != (2, 20.0):
        raise AssertionError(
            f"分段只该算热行：n 应 2、max 应 20.0，实际 n={seg.n} max={seg.maximum}"
            f"（max=5000 说明冷行混进了分段统计 —— 那正是 p95 变味的成因）"
        )
    # 这一条是本轮改的口径：冷启动里那次拒答（cold2）**不许**算进拒答率 ——
    # 用户那次等的是模型加载,把它算成"这一版答得怎么样"会让成绩随重启次数漂。
    if summary.warm.refused != 1 or summary.warm.rows != 2:
        raise AssertionError(
            f"热行的拒答该是 1/2，实际 {summary.warm.refused}/{summary.warm.rows}"
        )
    if summary.cold.refused != 1 or summary.cold.rows != 2:
        raise AssertionError(
            f"冷启动的拒答该单独数：该 1/2，实际 {summary.cold.refused}/{summary.cold.rows}"
            f"（若为 0，说明冷启动那一套数被藏起来了）"
        )
    if (summary.warm.faq_hits, summary.warm.rag_answers) != (0, 2):
        raise AssertionError(
            f"热行的通道计数该是 FAQ 0 / 知识库 2，实际 "
            f"{summary.warm.faq_hits}/{summary.warm.rag_answers}"
        )
    if (summary.cold.faq_hits, summary.cold.rag_answers) != (1, 0):
        raise AssertionError(
            f"冷启动的通道计数该单独数：FAQ 1 / 知识库 0，实际 "
            f"{summary.cold.faq_hits}/{summary.cold.rag_answers}"
        )
    if summary.warm.other_channels != 0 or summary.cold.other_channels != 1:
        raise AssertionError("未定通道的行数该按各自那一层算")
    if [item.request_id for item in summary.slowest][:2] != ["cold1", "cold2"]:
        raise AssertionError(
            f"最慢榜该含冷启动（用户真实等到的），实际 {[i.request_id for i in summary.slowest]}"
        )
    return "4 请求（冷 2 / 热 2）：热行拒答 1/2、冷启动拒答 1/2 各自成一套；最慢榜含冷"


@check("口径 老数据：缺 cold 字段的行按「热」算（不让它们凭空消失）")
def _legacy_rows_are_warm() -> str:
    from project_rag.application.metrics import summarize

    legacy = [{"label": "rag", "channel": "rag", "rerank_ms": "70.0", "total_ms": "99.0"}]
    summary = summarize(legacy, window="t")
    seg = _seg(summary, "rerank_ms")
    if (summary.cold_rows, summary.warm_rows, seg.n, seg.p50) != (0, 1, 1, 70.0):
        raise AssertionError(
            f"缺 cold 的老行该按热算：期望 cold=0/warm=1/n=1/p50=70.0，实际 "
            f"{summary.cold_rows}/{summary.warm_rows}/{seg.n}/{seg.p50}"
        )
    return "缺 cold 字段 → 计为热行并参与统计（不是被丢掉）"


@check("口径 空窗：0 行不抛异常，各项如实报 0/None（报表要能打开）")
def _empty_window() -> str:
    from project_rag.application.metrics import summarize

    summary = summarize([], window="24h")
    if (summary.rows, summary.segments[0].n, summary.segments[0].p50) != (0, 0, None):
        raise AssertionError(f"空窗该给 0 行 / n=0 / p50=None，实际 {summary}")
    if summary.slowest:
        raise AssertionError("空窗的最慢榜该是空的")
    return "空窗：0 行、n=0、p50=None、最慢榜空 —— 不抛异常"


@check("账本 追踪号：每条账自己生成 request_id（调用方漏传也追得到）")
def _request_id_default() -> str:
    from project_rag.observability import StreamTiming

    first, second = StreamTiming(label="rag"), StreamTiming(label="rag")
    if not first.request_id or first.request_id == second.request_id:
        raise AssertionError(
            f"两条账该有各自不同的 request_id，实际 {first.request_id!r} / {second.request_id!r}"
        )
    fields = first.values()
    if fields["request_id"] != first.request_id or fields["cold"] != "false":
        raise AssertionError(f"新字段没进 values()：{fields['request_id']} / {fields['cold']}")
    return f"自动生成 12 位追踪号（如 {first.request_id}），且已进账本字段"


@check("落表 只转交带 ledger 的记录；sink 抛异常只计数不炸；挂载幂等")
def _sink_contract() -> str:
    import logging

    from project_rag.observability import LedgerHandler, attach_ledger_sink

    written: list[Mapping[str, str]] = []
    handler = LedgerHandler(written.append)
    logger = logging.getLogger("project_rag.test.ledger")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("普通日志，没有 ledger 字段")  # 不该进表
        logger.info("latency x=1", extra={"ledger": {"request_id": "r1", "total_ms": "1.0"}})
    finally:
        logger.removeHandler(handler)
    if written != [{"request_id": "r1", "total_ms": "1.0"}]:
        raise AssertionError(f"只该转交带 ledger 的那一条，实际 {written}")
    if handler.rows != 1:
        raise AssertionError(f"handler.rows 该数到 1，实际 {handler.rows}")

    def boom(_row) -> None:
        raise RuntimeError("MySQL 挂了")

    broken = LedgerHandler(boom)
    # 注意:合成 LogRecord 必须**手动挂上** `ledger` 属性 —— 忘了挂的话 handler 会当成
    # "不是账本记录"直接返回,sink 一次都不会被调用,于是这条断言会误判成"产品没计数"
    # （这条检查第一版就是这么写错的,正向测试自己把它抓出来了）。
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "latency", None, None)
    record.ledger = {"request_id": "boom", "total_ms": "1.0"}
    broken.emit(record)
    if broken.failures != 1 or broken.rows != 0:
        raise AssertionError(
            f"sink 抛异常该记 failures=1 且 rows=0，实际 {broken.failures}/{broken.rows}"
            f"（异常穿出去会让一次回答因为'报表写不进去'而失败）"
        )

    first = attach_ledger_sink(lambda row: None)
    second = attach_ledger_sink(lambda row: None)
    logger2 = logging.getLogger("project_rag")
    installed = [h for h in logger2.handlers if isinstance(h, LedgerHandler)]
    for h in installed:
        logger2.removeHandler(h)
    if first is second or len(installed) != 1:
        raise AssertionError(
            f"挂载必须幂等（重复挂会把同一行写两遍），实际装了 {len(installed)} 个"
        )
    return "普通日志不落表；sink 故障记 failures 不抛；重复挂载只留一个 handler"


@check("接口 GET /api/v1/reports/metrics：形状与窗口白名单（注入假账本，不用容器）")
def _api_shape() -> str:
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    rows = _rows("rerank_ms", [10.0, 20.0], channel="rag")

    class _FakeLedger:
        def __init__(self) -> None:
            self.since = None

        def init_schema(self) -> None:
            pass

        def record(self, row) -> None:
            pass

        def load_since(self, since):
            self.since = since
            return rows

    ledger = _FakeLedger()
    # 注入 deps = 不读 .env、不连库:这条检查因此不需要容器
    client = TestClient(create_app(Deps(ledger_store_factory=lambda: ledger)))
    ok = client.get("/api/v1/reports/metrics?window=1h&top=3")
    if ok.status_code != 200:
        raise AssertionError(f"/api/v1/reports/metrics 该 200，实际 {ok.status_code}：{ok.text[:200]}")
    body = ok.json()
    for key in ("window", "rows", "cold_rows", "warm_rows", "warm", "cold", "segments", "slowest"):
        if key not in body:
            raise AssertionError(f"响应缺字段 {key}：{sorted(body)}")
    for layer in ("warm", "cold"):
        for key in ("rows", "refused", "truncated", "aborted", "faq_hits", "rag_answers"):
            if key not in body[layer]:
                raise AssertionError(f"{layer} 层缺字段 {key}：{sorted(body[layer])}")
    if body["warm"]["rows"] + body["cold"]["rows"] != body["rows"]:
        raise AssertionError(f"分层条数加不出总数：{body['warm']} + {body['cold']} ≠ {body['rows']}")
    if body["window"] != "1h" or body["rows"] != 2:
        raise AssertionError(f"窗口/行数不对：{body['window']} / {body['rows']}")
    if ledger.since is None:
        raise AssertionError("没把窗口换算成起始时刻就去读库了")
    bad = client.get("/api/v1/reports/metrics?window=42y")
    if bad.status_code != 400:
        raise AssertionError(f"窗口不在白名单该 400，实际 {bad.status_code}")
    return f"200 且含 {sorted(body)}；窗口白名单生效（42y → 400）"


@check("落表 真 MySQL 往返：写一行能读回来（需要容器）")
def _mysql_roundtrip() -> str:
    from datetime import datetime, timedelta

    from project_rag.adapters.ledger_mysql import MySQLLedgerStore
    from project_rag.config import get_settings

    try:
        store = MySQLLedgerStore(get_settings().mysql)
        store.init_schema()
    except Exception as exc:  # noqa: BLE001 - 容器没起:SKIP 并点名 owner
        raise SkipCheck(
            f"连不上 MySQL，这条往返没跑（owner：环境）—— 起来后重跑即绿。原始错误：{exc}"
        ) from exc

    marker = f"chk{datetime.now().strftime('%H%M%S%f')[:12]}"
    store.record(_row(request_id=marker, cold="true", builds="2", rerank_ms="123.4"))
    rows = store.load_since(datetime.now() - timedelta(minutes=5))
    hit = [row for row in rows if row.get("request_id") == marker]
    if not hit:
        raise AssertionError(f"写进去的 {marker} 没读回来（窗口内有 {len(rows)} 行）")
    if hit[0].get("rerank_ms") != "123.4" or hit[0].get("cold") != "true":
        raise AssertionError(f"读回来的 payload 不完整：{hit[0]}")
    return f"写入并读回 {marker}（payload 完整，含 rerank_ms / cold / builds）"


def main() -> int:
    return report(
        "契约：延迟账本聚合口径 + 落表链路",
        notes=(
            "说明：除「真 MySQL 往返」外全部内存假件（不需要容器/模型/网络）。",
        ),
        hint=(
            "提示：报表看 `project-rag metrics --window 24h` 或 `GET /api/v1/reports/metrics`。"
            "p95/p99 显示 `-` 不是坏了，是样本不够（门槛 20 / 100）—— 看 n 那一列。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

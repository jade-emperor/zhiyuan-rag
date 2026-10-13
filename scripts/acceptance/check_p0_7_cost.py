#!/usr/bin/env python
"""P0-7 契约:token 用量怎么采、怎么按**三档 × 两时段**折成钱、报表怎么说"不知道"。

用法::

    uv run python scripts/acceptance/check_p0_7_cost.py

**为什么要有这一份**:金额这种东西"看起来对"太容易了 —— 少算一半、双倍计入、
把没量到的当 0、把"不知道"画成一个数,四种错都只会让报表上出现一个**合理的数字**。
而作者的价目表还有两处会放大误差的维度(改口径 = 改契约)::

    输入(缓存命中)     空闲 0.02 / 高峰 0.04 元每百万   ← 命中与未命中差 50 倍
    输入(缓存未命中)   空闲 1    / 高峰 2
    输出              空闲 4    / 高峰 8               ← 高峰与空闲差 2 倍

**时段怎么判**(价目表原话,三个条件缺一不可)::

    空闲时段价格为高峰时段价格的一半。北京时间周一至周五(不含中国法定节假日)
    9:00 - 12:00、14:00 - 18:00 为高峰时段;其余时段,包括周末及中国法定节假日
    全天均为空闲时段。

漏一个条件的后果都是"贵一倍/便宜一半",而且报表上看不出来,所以这里逐条钉:
星期 / 钟点 / 节假日三个条件,时区(北京时间)、以及**六种"判不出来 / 报不出钱"的缺口**
(没量到 token / 没有时间戳 / 时间戳没时区 / 那一年没填节假日 / 缓存不明 / 没配单价)。

不需要容器 / 模型 / 网络:账本行是字面量,LLM 是桩。真 MySQL 往返见
``check_m5_history.py``(services 组)。
"""

from __future__ import annotations

import sys
from typing import Any

from _harness import check, report

# 作者的价目表(元 / 每百万 token)。**与 config.yaml 同一份数字**:
# 这张表变了,「算钱」那几条断言的分母也得跟着变 —— 那正是我们要盯的东西。
PEAK_HIT, PEAK_MISS, PEAK_OUT = 0.04, 2.0, 8.0
OFF_HIT, OFF_MISS, OFF_OUT = 0.02, 1.0, 4.0

MODEL = "DeepSeek-V4.1-Flash"

# 时段口径:与 config.yaml 的 cost.peak / cost.holidays 同一份(北京时间周一至周五的
# 09:00–12:00 与 14:00–18:00 是高峰;节假日整天算空闲)。
WEEKDAYS = [1, 2, 3, 4, 5]
WINDOWS = [["09:00", "12:00"], ["14:00", "18:00"]]
HOLIDAYS = [["2026-10-01", "2026-10-07"], ["2026-02-15", "2026-02-23"]]

# 2026-10-13 是**周二**(工作日),拿它做两笔账:
PEAK_TIME = "2026-10-13T10:00:00+08:00"  # 周二上午 10 点 = 高峰
OFF_TIME = "2026-10-13T03:00:00+08:00"  # 周二凌晨 3 点 = 空闲


def _rule(**override: Any):
    """输入:要覆盖的字段(weekdays / windows / holidays)。行为:建一条判定规则。输出:``PeriodRule``。"""
    from project_rag.application.cost import PeriodRule

    fields: dict[str, Any] = {"weekdays": WEEKDAYS, "windows": WINDOWS, "holidays": HOLIDAYS}
    fields.update(override)
    return PeriodRule.of(**fields)


class _Tier:
    """一个计价档(两个时段)。形状与 ``config.RateConfig`` 一致。"""

    def __init__(self, peak: float | None, off_peak: float | None) -> None:
        self.peak = peak
        self.off_peak = off_peak


class _Price:
    """一个模型的三档价。形状与 ``config.ModelPriceConfig`` 一致。"""

    def __init__(
        self,
        *,
        hit: tuple[float | None, float | None] = (PEAK_HIT, OFF_HIT),
        miss: tuple[float | None, float | None] = (PEAK_MISS, OFF_MISS),
        out: tuple[float | None, float | None] = (PEAK_OUT, OFF_OUT),
    ) -> None:
        self.input_cache_hit = _Tier(*hit)
        self.input_cache_miss = _Tier(*miss)
        self.output = _Tier(*out)


PRICES: dict[str, Any] = {MODEL: _Price()}


def _row(
    *,
    ts: str | None = PEAK_TIME,
    model: str = MODEL,
    inputs: str = "0",
    cached: str = "0",
    outputs: str = "0",
    called: bool = True,
    **extra: str,
) -> dict[str, str]:
    """输入:字段。行为:造一行账本。输出:那一行。

    ``called=False`` 造的是"压根没调模型"的行(FAQ 直答 / 拒答 / 寒暄)——
    它的 ``llm_ttft_ms`` 与三个 token 字段都是 ``-``。
    """
    row: dict[str, str] = {
        "channel": "rag",
        "llm_ttft_ms": "120.0" if called else "-",
        "llm_think_ms": "-",
        "llm_model": model if called else "-",
        "llm_input_tokens": inputs if called else "-",
        "llm_cached_input_tokens": cached if called else "-",
        "llm_output_tokens": outputs if called else "-",
    }
    if ts is not None:
        row["ts"] = ts
    row.update(extra)
    return row


def _report(rows, prices=None, currency: str = "CNY", rule=None):
    """输入:账本行 + 单价表(+币种 / 时段规则)。行为:跑一遍聚合。输出:``CostReport``。"""
    from project_rag.application.cost import summarize_cost

    return summarize_cost(
        rows,
        prices=PRICES if prices is None else prices,
        currency=currency,
        window="test",
        rule=_rule() if rule is None else rule,
    )


def _headers_of(client, username: str, password: str = "pw-123456") -> dict[str, str]:
    """输入:客户端 + 账号名。行为:登录。输出:``Authorization`` 头。"""
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    if response.status_code != 200:
        raise AssertionError(f"{username} 登录该 200,实际 {response.status_code}:{response.text[:200]}")
    return {"Authorization": f"Bearer {response.json()['token']}"}


# ══ 1. 算钱:三档 × 两时段 ═══════════════════════════════════════════════════
@check("算钱 高峰：三档分别乘，手算得出来（1M 输入含 0.8M 命中 + 0.1M 输出 = 1.232）")
def _money_peak() -> str:
    report_ = _report([_row(inputs="1000000", cached="800000", outputs="100000")])
    line = report_.lines[0]
    expected = round(0.8 * PEAK_HIT + 0.2 * PEAK_MISS + 0.1 * PEAK_OUT, 6)
    if line.total_cost != expected:
        raise AssertionError(
            f"0.8M 命中×{PEAK_HIT} + 0.2M 未命中×{PEAK_MISS} + 0.1M 输出×{PEAK_OUT} "
            f"该是 {expected},实际 {line.total_cost}"
        )
    if line.period != "peak":
        raise AssertionError(f"周二 10:00 该算高峰,实际 {line.period}")
    if (line.input_tokens, line.cached_input_tokens, line.output_tokens) != (
        1000000,
        800000,
        100000,
    ):
        raise AssertionError(f"token 拆分没照实记:{line}")
    return f"高峰:{expected} 元(0.8×{PEAK_HIT} + 0.2×{PEAK_MISS} + 0.1×{PEAK_OUT})"


@check("算钱 空闲：同一笔在凌晨正好是高峰的一半（单价差 2 倍）")
def _money_off_peak() -> str:
    peak = _report([_row(inputs="1000000", cached="800000", outputs="100000")])
    off = _report([_row(ts=OFF_TIME, inputs="1000000", cached="800000", outputs="100000")])
    line = off.lines[0]
    expected = round(0.8 * OFF_HIT + 0.2 * OFF_MISS + 0.1 * OFF_OUT, 6)
    if line.period != "off_peak" or line.total_cost != expected:
        raise AssertionError(f"周二 03:00 该算空闲且金额 {expected},实际 {line.period}/{line.total_cost}")
    peak_cost = peak.lines[0].total_cost
    if line.total_cost is None or peak_cost is None or line.total_cost * 2 != peak_cost:
        raise AssertionError(f"空闲该正好是高峰的一半:{line.total_cost} vs {peak_cost}")
    return f"空闲 {line.total_cost} 元 = 高峰 {peak.lines[0].total_cost} 元的一半"


@check("算钱 时段分行：同一模型跨两个时段时分成两行，时段合计能对上")
def _money_two_periods() -> str:
    report_ = _report(
        [
            _row(inputs="1000000", cached="0", outputs="0"),  # 高峰:1M 未命中
            _row(ts=OFF_TIME, inputs="1000000", cached="0", outputs="0"),  # 空闲:1M 未命中
        ]
    )
    if [line.period for line in report_.lines] != ["off_peak", "peak"]:
        raise AssertionError(f"该按(模型, 时段)分行:{[line.period for line in report_.lines]}")
    totals = {item.period: item for item in report_.periods}
    if (totals["peak"].calls, totals["off_peak"].calls) != (1, 1):
        raise AssertionError(f"时段调用数不对:{report_.periods}")
    if round(totals["peak"].total_cost, 6) != PEAK_MISS or round(
        totals["off_peak"].total_cost, 6
    ) != OFF_MISS:
        raise AssertionError(f"时段合计不对:{report_.periods}")
    return f"高峰 {PEAK_MISS} 元 + 空闲 {OFF_MISS} 元,各 1 次调用"


@check("算钱 不分时段的部署：只配 peak 时空闲也按 peak 计（回落，不是算不出来）")
def _money_single_period() -> str:
    report_ = _report(
        [_row(ts=OFF_TIME, inputs="1000000", cached="0", outputs="0")],
        {MODEL: _Price(miss=(2.0, None), hit=(0.04, None), out=(8.0, None))},
    )
    if report_.lines[0].period != "off_peak":
        raise AssertionError("时段仍该是空闲(只是它没有单独的价)")
    if report_.lines[0].total_cost != 2.0:
        raise AssertionError(
            f"空闲没配价时该回落到高峰价(2.0),实际 {report_.lines[0].total_cost}"
        )
    if not report_.all_priced:
        raise AssertionError("回落价算得出钱,不该报未配单价")
    return "只配 peak → 空闲按 peak 计(2.0 元),不算未配"


@check("算钱 先汇总再乘 + 只在最后四舍五入：单次调用的小额金额不被抹成 0.00")
def _money_rounding() -> str:
    report_ = _report([_row(inputs="3", cached="0", outputs="0")])
    line = report_.lines[0]
    if line.total_cost in (None, 0.0):
        raise AssertionError(f"3 个未命中 token 也是钱(0.000006),不该被抹成 {line.total_cost}")
    if line.total_cost != round(3 / 1_000_000 * PEAK_MISS, 6):
        raise AssertionError(f"金额该保留到 6 位,实际 {line.total_cost}")
    same = _report(
        [
            _row(inputs="3", cached="0", outputs="0"),
            _row(inputs="4", cached="0", outputs="0"),
        ]
    )
    if same.lines[0].total_cost != round(7 / 1_000_000 * PEAK_MISS, 6):
        raise AssertionError(f"两次调用该合成一次乘法:{same.lines[0].total_cost}")
    return f"3 token → {line.total_cost} 元;两行合并 = 7 token × 价"


# ══ 2. 六个"缺口"必须看得见(判不出来 / 报不出钱,都不许画成一个数)═══════════
@check("缺口 未配单价：金额是 None + 列进 unpriced_models，**不是 0 元**")
def _unpriced_is_not_zero() -> str:
    report_ = _report([_row(model="unknown-model", inputs="1000000", cached="0", outputs="0")])
    line = report_.lines[0]
    if line.total_cost is not None:
        raise AssertionError(f"没配单价的模型金额必须是 None(不是 0),实际 {line.total_cost}")
    if report_.all_priced or report_.unpriced_models != ("unknown-model",):
        raise AssertionError(f"该把没配单价的模型报出来:{report_}")
    if report_.total_cost != 0.0:
        raise AssertionError("合计只算得出单价的那部分,并由 all_priced=False 说明不完整")
    return "金额 None + unpriced_models 列出名字 + all_priced=False"


@check("缺口 单价 0 元 ≠ 未配：0 是『确实不要钱』，金额 0.0 且 all_priced=True")
def _zero_price_is_configured() -> str:
    report_ = _report(
        [_row(inputs="1000000", cached="0", outputs="1000000")],
        {MODEL: _Price(hit=(0.0, 0.0), miss=(0.0, 0.0), out=(0.0, 0.0))},
    )
    if report_.lines[0].total_cost != 0.0:
        raise AssertionError(f"配了 0 元该算出 0.0,实际 {report_.lines[0].total_cost}")
    if not report_.all_priced:
        raise AssertionError("0 元是配过的价,不该进 unpriced")
    return "0 元单价 → 金额 0.0、all_priced=True(与未配单价是两件事)"


@check("缺口 没量到 token：计进 unmetered_calls，**不算进金额**，也不是 0 token")
def _unmetered_counted_separately() -> str:
    report_ = _report(
        [
            _row(model="-", inputs="-", cached="-", outputs="-"),  # 调了模型,provider 没回报
            _row(inputs="1000000", cached="0", outputs="0"),
        ]
    )
    if (report_.metered_calls, report_.unmetered_calls) != (1, 1):
        raise AssertionError(
            f"该是『1 次量到 + 1 次没量到』,实际 {report_.metered_calls}/{report_.unmetered_calls}"
        )
    if report_.input_tokens != 1000000:
        raise AssertionError(f"没量到的那次不该被当成 0 个 token 混进汇总:{report_.input_tokens}")
    if report_.total_cost != round(1_000_000 / 1_000_000 * PEAK_MISS, 6):
        raise AssertionError(f"金额只该来自量到的那次,实际 {report_.total_cost}")
    return "1 次量到 + 1 次没量到(单列,不计金额)"


@check("缺口 缓存不明：没回报命中数时整段输入按**未命中价**计（上界）并计数")
def _cache_unknown_is_upper_bound() -> str:
    report_ = _report([_row(inputs="1000000", cached="-", outputs="0")])
    line = report_.lines[0]
    if report_.cache_unknown_calls != 1:
        raise AssertionError(f"该计一次『缓存不明』,实际 {report_.cache_unknown_calls}")
    if line.cached_input_tokens != 0:
        raise AssertionError(f"不知道就不许编命中数,实际 {line.cached_input_tokens}")
    if line.total_cost != PEAK_MISS:
        raise AssertionError(
            f"该按未命中价计(1M × {PEAK_MISS} = {PEAK_MISS},上界),实际 {line.total_cost}"
        )
    reported_zero = _report([_row(inputs="1000000", cached="0", outputs="0")])
    if reported_zero.cache_unknown_calls != 0:
        raise AssertionError("provider 明确报了 0 命中的,不算『缓存不明』")
    return f"没回报 → 按未命中价 {PEAK_MISS}(上界)+ 计数;报 0 → 不计数"


@check("缺口 时段不明：读不到账本时间戳时按**高峰价**计（偏保守）并计数")
def _period_unknown_is_peak() -> str:
    report_ = _report([_row(ts=None, inputs="1000000", cached="0", outputs="0")])
    if report_.period_unknown_calls != 1:
        raise AssertionError(f"该计一次『时段不明』,实际 {report_.period_unknown_calls}")
    if report_.lines[0].period != "peak" or report_.lines[0].total_cost != PEAK_MISS:
        raise AssertionError(f"该按高峰价计:{report_.lines[0]}")
    naive = _report([_row(ts=None, inputs="1000000", cached="0", outputs="0")], rule=_rule(windows=[]))
    if naive.period_unknown_calls != 0:
        raise AssertionError("部署不分时段时,时间戳读不到不该算『时段不明』")
    return f"无时间戳 → 高峰价 {PEAK_MISS} + 计数;不分时段时不计数"


@check("缺口 时区不明：时间戳没带时区时判不出来，按高峰价计 + 计数（不许当北京时间用）")
def _zone_unknown_is_peak() -> str:
    """**这条是拿真数据换来的**:账本表里的 ts 来自写入进程的本地钟,而容器是 UTC、宿主是 CST。

    实测:同一张表里两种写入者都有(容器 ``date`` → UTC,宿主 → CST),只存一个裸钟点的话,
    8 小时的差正好把一个上午的高峰判成空闲 —— 少报一半的钱,而报表上看不出来。
    """
    # 裸钟点 02:00:若**当北京时间用**会算成空闲(0.616 元);判不出来时按高峰计(1.232 元)。
    naive = _report([_row(ts="2026-10-13T02:00:00", inputs="1000000", cached="800000", outputs="100000")])
    if naive.zone_unknown_calls != 1 or naive.period_unknown_calls != 0:
        raise AssertionError(
            f"该计一次『时区不明』、0 次『时段不明』(有时间戳,只是没时区):"
            f"{naive.zone_unknown_calls}/{naive.period_unknown_calls}"
        )
    if naive.lines[0].period != "peak" or naive.lines[0].total_cost == 0.616:
        raise AssertionError(f"裸钟点该按**高峰价**计(保守),实际 {naive.lines[0]}")
    # 同一串数字带上 +00:00 = 北京 10:00(高峰),就不该再有缺口
    aware = _report([_row(ts="2026-10-13T02:00:00+00:00", inputs="1000000", cached="0", outputs="0")])
    if (aware.zone_unknown_calls, aware.lines[0].period) != (0, "peak"):
        raise AssertionError(f"带时区的时刻判得出高峰:{aware.zone_unknown_calls}/{aware.lines[0]}")
    # 带上 +00:00 落在北京午休(12:00)就该是空闲 —— 证明判的是**换算后的北京时间**
    lunch = _report([_row(ts="2026-10-13T04:00:00+00:00", inputs="1000000", cached="0", outputs="0")])
    if (lunch.zone_unknown_calls, lunch.lines[0].period) != (0, "off_peak"):
        raise AssertionError(f"UTC 04:00 = 北京 12:00 该算空闲:{lunch.lines[0]}")
    return "裸钟点 → 时区不明 + 高峰价(0.616 那个便宜价不许蒙);带 +00:00 则判得出高峰/午休"


@check("缺口 日历不明：那一年没填法定节假日时按高峰价计 + 计数（不假装那天不放假）")
def _calendar_unknown_is_peak() -> str:
    # 2027 年的节假日还没填进配置(国务院每年 11 月前后才发布):2027-03-02 是工作日,
    # 没法知道它是不是春节 —— 按高峰计(贵的那档)并如实计数。
    report_ = _report([_row(ts="2027-03-02T10:00:00+08:00", inputs="1000000", cached="0", outputs="0")])
    if report_.calendar_unknown_calls != 1:
        raise AssertionError(f"该计一次『日历不明』,实际 {report_.calendar_unknown_calls}")
    if report_.lines[0].total_cost != PEAK_MISS:
        raise AssertionError(f"该按高峰价计:{report_.lines[0]}")
    # 窗口外的那一年**不用**报:夜里/周末本来就已经是空闲,节假日表帮不上忙
    night = _report([_row(ts="2027-03-02T03:00:00+08:00", inputs="1000000", cached="0", outputs="0")])
    if night.calendar_unknown_calls != 0 or night.lines[0].period != "off_peak":
        raise AssertionError(f"窗口外的调用不受日历影响:{night.calendar_unknown_calls}")
    # 填了那一年就判得准:2027-02-15 是春节假期(这里给一份假的 2027 表来验证接线)
    known = _report(
        [_row(ts="2027-03-02T10:00:00+08:00", inputs="1000000", cached="0", outputs="0")],
        rule=_rule(holidays=[*HOLIDAYS, ["2027-01-01", "2027-12-31"]]),
    )
    if known.calendar_unknown_calls != 0 or known.lines[0].period != "off_peak":
        raise AssertionError(f"填了日历就该判得出空闲:{known.lines[0]}")
    return "缺 2027 日历 → 高峰价 + 计数;夜里不计;补上日历就判得准"


@check("缺口 老账本：调过模型但没有 token 字段的行只计数，**不被报成『未配单价的模型 -』**")
def _legacy_rows_not_unpriced() -> str:
    """加这套东西之前记的账就是这样:有 ``llm_ttft_ms``(真调了模型)但没有 token 字段。

    实测触发的形状:作者库里 199 行老账 → 13 行"调过模型"、token 全空。第一版的实现会把它
    报成 `unpriced_models = ("-",)`,等于让报表说"请把 `-` 填进 config.yaml"。
    """
    report_ = _report(
        [
            {"llm_ttft_ms": "900.0", "ts": PEAK_TIME},  # 老账:只有耗时
            {"llm_ttft_ms": "1200.0", "ts": PEAK_TIME},
        ]
    )
    if (report_.metered_calls, report_.unmetered_calls) != (0, 2):
        raise AssertionError(f"两行都该算『调了但没量到』:{report_}")
    if report_.lines or report_.unpriced_models:
        raise AssertionError(
            f"没有 token 数就不该有按模型的分行、更不该报未配单价:"
            f"{report_.lines} / {report_.unpriced_models}"
        )
    return "2 次调用没量到 token、0 条分行、不报未配单价"


@check("缺口 没调模型的行不进报表：FAQ 命中 / 检索为空拒答 / 寒暄都不花钱")
def _no_llm_rows_ignored() -> str:
    report_ = _report(
        [
            _row(called=False, channel="faq"),
            _row(called=False, channel="rag"),
            _row(called=False, channel="greeting"),
        ]
    )
    if report_.lines or report_.metered_calls or report_.unmetered_calls:
        raise AssertionError(f"没调模型的行不该出现在消耗报表里:{report_}")
    if report_.total_cost != 0.0 or not report_.all_priced:
        raise AssertionError("没有调用时合计 0、且不该报『未配单价』")
    return "三行都没调模型 → lines 空、0 次调用、0 元"


@check("缺口 不双计：真跑一次合流请求，两行账只算一次钱（用量只记在内层）")
def _no_double_count() -> str:
    """**这一条必须用真链路**:外层 qa 那行的 ``tokens``/``ttft_ms`` 都是数值
    (它转发了内层的 token 帧),光看字面量数据说不清"为什么它不该被计费"。

    token 用量只挂在内层 rag 那一行(见 ``application/answer_rag.py`` 的 ``timing.usage``),
    所以判据是 ``llm_ttft_ms`` / ``llm_model`` —— 那两个字段在外层是 ``-``。
    """
    import logging

    from _fakes import FakeEmbedder, FakeFaqStore, FakeMatcher, FakeVectorStore
    from project_rag.application import stream_question, stream_rag
    from project_rag.domain import (
        Chunk,
        LlmDelta,
        LlmUsage,
        RetrievalOptions,
        RetrievedChunk,
    )

    class _LLM:
        """吐一句答案,并在收尾帧上回报用量(含缓存命中,与真适配器同一个约定)。"""

        def stream(self, messages):
            yield LlmDelta(text="答案")
            yield LlmDelta(
                finish_reason="stop",
                last=True,
                usage=LlmUsage(
                    model=MODEL,
                    input_tokens=1000000,
                    output_tokens=0,
                    cached_input_tokens=0,
                ),
            )

    hit = RetrievedChunk(
        chunk=Chunk("c1", "子块", "bagu_ai", "d1", "p1", "父块正文"), score=0.9
    )

    def knowledge(history):
        return stream_rag(
            "装饰器有什么用",
            embedder=FakeEmbedder(),
            store=FakeVectorStore([hit]),  # type: ignore[arg-type]
            llm=_LLM(),
            keyword_index=None,
            options=RetrievalOptions(k=3, max_context=2),
        )

    rows: list[dict] = []

    class _Catcher(logging.Handler):
        def emit(self, record):
            row = getattr(record, "ledger", None)
            if isinstance(row, dict):
                rows.append(row)

    logger = logging.getLogger("project_rag.observability.latency")
    handler = _Catcher()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for _event in stream_question(
            "装饰器有什么用",
            faq_store=FakeFaqStore([]),
            matcher=FakeMatcher(),
            knowledge=knowledge,
        ):
            pass
    finally:
        logger.removeHandler(handler)

    if len(rows) != 2:
        raise AssertionError(f"一次合流请求该写两条账(rag + qa),实际 {len(rows)}")
    by_label = {row["label"]: row for row in rows}
    inner, outer = by_label["rag"], by_label["qa"]
    if (inner["llm_input_tokens"], inner["llm_model"]) != ("1000000", MODEL):
        raise AssertionError(f"内层该带着用量:{inner}")
    if (outer["llm_model"], outer["llm_ttft_ms"]) != ("-", "-"):
        raise AssertionError(f"外层不该有模型名或 llm_ttft(它没有自己的 provider 调用):{outer}")
    if outer["ttft_ms"] == "-":
        raise AssertionError("前置条件不成立:外层本该量到 ttft(用户等的是它)")

    # 账本行没有 ts(不是从库里读的),所以这里不分时段:按高峰价验"只算一次"
    report_ = _report(rows, rule=_rule(windows=[]))
    if report_.metered_calls != 1:
        raise AssertionError(f"该只算 1 次模型调用,实际 {report_.metered_calls}")
    if report_.unmetered_calls != 0:
        raise AssertionError(f"外层不该被算成『调了但没量到』,实际 {report_.unmetered_calls}")
    if report_.total_cost != PEAK_MISS:
        raise AssertionError(f"金额该是 1 百万未命中输入 = {PEAK_MISS} 元,实际 {report_.total_cost}")
    return f"真链路两条账 → 1 次调用 / {PEAK_MISS} 元(外层 ttft={outer['ttft_ms']}ms 但不计费)"


@check("缺口 空窗口 / 脏数据：报表要能打开，坏值当『没量到』而不是抛异常")
def _empty_and_dirty() -> str:
    empty = _report([])
    if (empty.metered_calls, empty.unmetered_calls, empty.total_cost) != (0, 0, 0.0):
        raise AssertionError(f"空窗口该全 0:{empty}")
    if empty.lines or empty.unpriced_models or not empty.all_priced:
        raise AssertionError(f"空窗口不该报未配单价:{empty}")
    if empty.currency != "CNY":
        raise AssertionError("币种该原样带出来")

    dirty = _report(
        [
            _row(inputs="-", cached="", outputs="-"),  # 三个字段都读不出
            _row(inputs="abc", cached="1", outputs="5"),  # 输入非法、输出有效
        ]
    )
    if (dirty.metered_calls, dirty.unmetered_calls) != (1, 1):
        raise AssertionError(
            f"一行全空 + 一行部分可读 → 1 量到 + 1 没量到,实际 "
            f"{dirty.metered_calls}/{dirty.unmetered_calls}"
        )
    if dirty.output_tokens != 5:
        raise AssertionError(f"能读出来的那 5 个输出 token 必须被算上:{dirty.output_tokens}")
    return "空窗口全 0;'-'/空/非数字都按没量到处理(不抛异常、不编数)"


# ══ 3. 时段判定(纯函数):工作日 ∩ 钟点 ∩ 非节假日,而且得是"北京时间的几点" ══
@check("时段 三条件：工作日 ∩ 09:00/14:00 两窗口 ∩ 非节假日 —— 任一不满足就空闲")
def _period_three_conditions() -> str:
    from datetime import datetime

    rule = _rule()
    # 2026-10-13 周二 / 10-10 周六 / 10-06 国庆假期内(周二)
    cases = {
        "2026-10-13T09:00:00": "peak",  # 窗口第一秒(左闭)
        "2026-10-13T11:59:00": "peak",
        "2026-10-13T12:00:00": "off_peak",  # 午休从 12:00 整开始(右开)
        "2026-10-13T13:59:00": "off_peak",
        "2026-10-13T14:00:00": "peak",
        "2026-10-13T17:59:00": "peak",
        "2026-10-13T18:00:00": "off_peak",  # 下班
        "2026-10-13T08:59:00": "off_peak",  # 上班前
        "2026-10-13T23:30:00": "off_peak",
        "2026-10-13T03:00:00": "off_peak",
        # ① 星期:周六周日不管几点都是空闲
        "2026-10-10T10:00:00": "off_peak",
        "2026-10-11T15:00:00": "off_peak",
        # ② 日历:国庆假期里的工作日时段是空闲(这是最容易被漏掉的一条)
        "2026-10-06T10:00:00": "off_peak",
        # ③ 调休上班的周末**不算工作日**(价目表说的是"周末",不是调休日历)
        "2026-01-04T10:00:00": "off_peak",  # 元旦调休,周日上班
    }
    for clock, want in cases.items():
        moment = datetime.fromisoformat(f"{clock}+08:00")
        got, gap = rule.classify(moment)
        if got != want or gap is not None:
            raise AssertionError(f"{clock} 该是 {want},实际 {got}(缺口 {gap!r})")
    return f"{len(cases)} 个钟点全判对(含午休边界、周末、国庆、调休上班日)"


@check("时段 北京时间：给 UTC 的时刻要换算过去判（差 8 小时 = 一个高峰窗口的距离）")
def _period_beijing_clock() -> str:
    from datetime import datetime

    rule = _rule()
    cases = {
        "2026-10-13T02:00:00+00:00": "peak",  # UTC 02:00 = 北京 10:00
        "2026-10-13T04:00:00+00:00": "off_peak",  # = 北京 12:00(午休)
        "2026-10-13T06:00:00+00:00": "peak",  # = 北京 14:00
        "2026-10-12T22:00:00-04:00": "peak",  # 纽约 10-12 22:00 = 北京 10-13 10:00
        "2026-10-13T18:00:00+00:00": "off_peak",  # = 北京 10-14 02:00(深夜)
    }
    for stamp, want in cases.items():
        got, gap = rule.classify(datetime.fromisoformat(stamp))
        if got != want or gap is not None:
            raise AssertionError(f"{stamp} 该是 {want},实际 {got}(缺口 {gap!r})")
    # 同一串钟点、两种时区 → 两个时段:这正是"必须带时区"的理由
    naive = _rule().classify(datetime.fromisoformat("2026-10-13T10:00:00"))
    aware = _rule().classify(datetime.fromisoformat("2026-10-13T10:00:00+08:00"))
    if naive != ("peak", "zone") or aware != ("peak", None):
        raise AssertionError(f"裸钟点该判不出来(缺口 zone),实际 {naive};带时区该能判:{aware}")
    return "UTC/纽约的钟点都换算成北京时间判;裸钟点一律判不出来"


@check("时段 窗口判定：左闭右开、支持跨午夜、窗口为空时全算高峰")
def _window_rules() -> str:
    from datetime import datetime, time

    from project_rag.application.cost import in_windows, parse_windows

    windows = parse_windows([["09:00", "12:00"], ["14:00", "18:00"]])
    cases = {
        "08:59": False,
        "09:00": True,
        "09:01": True,
        "11:59": True,
        "12:00": False,
        "14:00": True,
        "17:59": True,
        "18:00": False,
    }
    for clock, want in cases.items():
        if in_windows(time.fromisoformat(clock), windows) != want:
            raise AssertionError(f"窗口边界判错:{clock} 该是 {want}")

    midnight = parse_windows([["23:00", "01:00"]])
    for clock, want in {"22:59": False, "23:30": True, "00:30": True, "01:00": False}.items():
        if in_windows(time.fromisoformat(clock), midnight) != want:
            raise AssertionError(f"跨午夜窗口判错:{clock}")

    if in_windows(time.fromisoformat("10:00"), ()):
        raise AssertionError("窗口为空时 in_windows 该给 False(不分时段由规则自己短路)")
    # 不分时段:全按高峰价,而且**不报"时段不明"**(连时间戳都不用问)
    flat = _rule(windows=[])
    for moment in (None, datetime.fromisoformat("2026-10-13T10:00:00")):
        if flat.classify(moment) != ("peak", None):
            raise AssertionError(f"窗口为空该全算高峰且不报缺口:{flat.classify(moment)}")
    if flat.split:
        raise AssertionError("窗口为空时 split 该是 False")
    return "边界 09:00 进 / 12:00 出;跨午夜;空窗口全高峰且不报缺口"


# ══ 4. 采集:适配器把 provider 的用量(含缓存)交出来 ════════════════════════
class _Chunk:
    """一个假 SDK chunk:只需要适配器会读的那几个属性。"""

    def __init__(
        self,
        *,
        content: str = "",
        usage: dict[str, Any] | None = None,
        model: str = MODEL,
        reason: str | None = None,
        thought: str = "",
    ) -> None:
        self.content = content
        self.additional_kwargs = {"reasoning_content": thought} if thought else {}
        self.usage_metadata = usage
        # 类型写 Any:它的值既有字符串(model_name)也有嵌套字典(token_usage)
        self.response_metadata: dict[str, Any] = {"model_name": model}
        if reason is not None:
            self.response_metadata["finish_reason"] = reason


class _StubClient:
    """假 ChatDeepSeek:``stream`` 吐给定的 chunk。**不联网、不花钱。**"""

    def __init__(self, chunks, *, stream_usage: bool = True) -> None:
        self.chunks = chunks
        self.stream_usage = stream_usage
        self.seen = None

    def stream(self, messages):
        self.seen = messages
        return iter(self.chunks)


def _deepseek(chunks, *, stream_usage: bool = True):
    """输入:假 chunk + 是否开启用量回报。行为:造一个接了桩客户端的 DeepSeekLLM。输出:``(llm, stub)``。"""
    from pydantic import SecretStr

    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.config import LLMConfig

    config = LLMConfig(
        deepseek_api_key=SecretStr("sk-test"), model="deepseek-flash", stream_usage=stream_usage
    )
    llm = DeepSeekLLM(config)
    stub = _StubClient(chunks, stream_usage=stream_usage)
    llm._client = stub  # type: ignore[assignment] - 只为了不联网:被测的是 stream() 的转换逻辑
    return llm, stub


@check("采集 标准形状：usage_metadata 的 token 三段（含缓存命中）挂在收尾帧上")
def _usage_from_metadata() -> str:
    from project_rag.domain import ChatMessage

    llm, _ = _deepseek(
        [
            _Chunk(content="答"),
            _Chunk(
                content="",
                usage={
                    "input_tokens": 1200,
                    "output_tokens": 34,
                    "total_tokens": 1234,
                    "input_token_details": {"cache_read": 1000},
                },
                reason="stop",
            ),
        ]
    )
    frames = list(llm.stream([ChatMessage(role="user", content="问")]))
    last = frames[-1]
    if not last.last or last.usage is None:
        raise AssertionError(f"收尾帧该带 usage,实际 {last}")
    usage = last.usage
    if (usage.model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) != (
        MODEL,
        1200,
        34,
        1000,
    ):
        raise AssertionError(f"用量没照实交出来:{usage}")
    if (usage.uncached_input_tokens, usage.total_tokens) != (200, 1234):
        raise AssertionError(f"未命中数/总数算错:{usage}")
    if any(frame.usage is not None for frame in frames[:-1]):
        raise AssertionError("中间帧不该带用量(provider 只在末尾给)")
    return "收尾帧带 1200 输入(命中 1000)/ 34 输出;中间帧不带"


@check("采集 DeepSeek 原始形状：token_usage 里的 prompt_cache_hit_tokens 也认得")
def _usage_from_deepseek_raw() -> str:
    from project_rag.domain import ChatMessage

    chunk = _Chunk(content="答", reason="stop")
    chunk.usage_metadata = None
    chunk.response_metadata["token_usage"] = {
        "prompt_tokens": 500,
        "completion_tokens": 20,
        "prompt_cache_hit_tokens": 0,
    }
    llm, _ = _deepseek([chunk])
    usage = list(llm.stream([ChatMessage(role="user", content="问")]))[-1].usage
    if usage is None or (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) != (
        500,
        20,
        0,
    ):
        raise AssertionError(f"原始形状该被认出来(含 cache_hit=0):{usage}")
    if usage.uncached_input_tokens != 500:
        raise AssertionError("命中 0 时未命中该是全部 500")
    return "DeepSeek 原始 token_usage(prompt_cache_hit_tokens=0)也认;未命中 = 全部"


@check("采集 没有用量就不编：没回报时 usage 是 None；没有缓存明细时命中级是 None")
def _usage_missing_is_none() -> str:
    from project_rag.domain import ChatMessage

    llm, _ = _deepseek([_Chunk(content="答", reason="stop")])
    last = list(llm.stream([ChatMessage(role="user", content="问")]))[-1]
    if last.usage is not None:
        raise AssertionError(f"没回报用量时该给 None,实际 {last.usage}")

    only_totals, _ = _deepseek(
        [_Chunk(content="答", reason="stop", usage={"input_tokens": 100, "output_tokens": 5})]
    )
    usage = list(only_totals.stream([ChatMessage(role="user", content="问")]))[-1].usage
    if usage is None or usage.cached_input_tokens is not None:
        raise AssertionError(f"没有缓存明细时该是 None(0 会被按 {OFF_HIT} 元计价):{usage}")
    if usage.uncached_input_tokens is not None:
        raise AssertionError("缺命中数时算不出未命中数,该给 None 而不是当成全部")
    return "没用量 → None;有总量无缓存明细 → cached=None(不是 0)"


@check("采集 模型名取 provider 说的那个：它才是单价表要用的键")
def _usage_model_prefers_provider() -> str:
    from project_rag.domain import ChatMessage

    llm, _ = _deepseek(
        [
            _Chunk(
                content="答",
                reason="stop",
                model="DeepSeek-V4.1-Flash",
                usage={"input_tokens": 5, "output_tokens": 1},
            )
        ]
    )
    usage = list(llm.stream([ChatMessage(role="user", content="问")]))[-1].usage
    if usage is None or usage.model != "DeepSeek-V4.1-Flash":
        raise AssertionError(f"模型名该取 provider 回报的,实际 {usage}")
    return "provider 报 DeepSeek-V4.1-Flash → 账本里就是它(单价按它查)"


@check("采集 开关接线：config.llm.stream_usage 真的传给了真客户端（关掉要能关掉）")
def _stream_usage_wired() -> str:
    """**必须看真客户端**,不能看桩:桩上的那个属性是我们自己设的,验不出接线。

    构造 ``ChatDeepSeek`` 不发请求(它只存参数),所以这一条既不联网也不花钱。
    """
    from pydantic import SecretStr

    from project_rag.adapters.llm_deepseek import DeepSeekLLM
    from project_rag.config import LLMConfig

    def client_for(flag: bool):
        return DeepSeekLLM(
            LLMConfig(
                deepseek_api_key=SecretStr("sk-test"),
                model="deepseek-flash",
                stream_usage=flag,
            )
        )._client

    if getattr(client_for(True), "stream_usage") is not True:
        raise AssertionError("stream_usage=true 该传给 ChatDeepSeek")
    if getattr(client_for(False), "stream_usage") is not False:
        raise AssertionError("stream_usage=false 该真的关掉它(有些代理不认这个参数)")
    return "config.llm.stream_usage 逐字传给真客户端(true/false 都验过)"


# ══ 5. 配置与两个入口 ════════════════════════════════════════════════════════
@check("配置 cost 段：三档 × 两时段、高峰三条件、负价与格式错都被拒、prices() 形状固定")
def _cost_config() -> str:
    from project_rag.config import CostConfig, ModelPriceConfig, PeakConfig, RateConfig

    default = CostConfig()
    if default.currency != "CNY" or default.models or default.peak.windows:
        raise AssertionError(f"默认该是『没配任何单价、不分时段』:{default}")
    if default.holidays:
        raise AssertionError("默认不该自带任何节假日(它是每年要更新的数据,不是代码默认值)")
    if default.prices() != {}:
        raise AssertionError("空表该转成空字典")

    filled = CostConfig(
        peak=PeakConfig(weekdays=WEEKDAYS, windows=WINDOWS),
        holidays=HOLIDAYS,
        models={
            # 显式写成 RateConfig:pydantic 收 dict 也认,但类型检查器要看见这个形状
            "m": ModelPriceConfig(
                input_cache_hit=RateConfig(peak=PEAK_HIT, off_peak=OFF_HIT),
                input_cache_miss=RateConfig(peak=PEAK_MISS, off_peak=OFF_MISS),
                output=RateConfig(peak=PEAK_OUT, off_peak=OFF_OUT),
            )
        },
    )
    price = filled.prices()["m"]
    if price.input_cache_hit.for_period(off_peak=True) != OFF_HIT:
        raise AssertionError("prices() 该给三档 × 两时段")
    if price.output.for_period(off_peak=False) != PEAK_OUT:
        raise AssertionError("高峰价取错")
    if not price.configured:
        raise AssertionError("配过价的模型 configured 该为真")
    if (filled.peak.weekdays, filled.peak.windows, filled.holidays) != (
        WEEKDAYS,
        WINDOWS,
        HOLIDAYS,
    ):
        raise AssertionError("config 该原样保留星期 / 窗口 / 节假日")

    for bad in (-1.0, -0.5):
        try:
            RateConfig(peak=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"负单价该被拒:{bad}")
    rejected = [
        ({"peak": {"windows": [["25:00", "08:30"]]}}, "非法窗口格式"),
        ({"peak": {"weekdays": [0, 1, 2]}}, "星期 0(C 风格)"),
        ({"peak": {"weekdays": [1, 8]}}, "星期 8"),
        ({"holidays": ["2026-01-01", "2026-01-03"]}, "节假日写成扁平的一串日期"),
        ({"holidays": [["2026-01-03", "2026-01-01"]]}, "节假日区间起止反了"),
        ({"holidays": [["2026-13-01", "2026-13-02"]]}, "节假日不是日期"),
    ]
    for kwargs, why in rejected:
        try:
            CostConfig(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"{why} 该被拒 —— 写错的后果是『一直按另一档算钱』,而报表上看不出异常:{kwargs}"
            )
    return "默认空;三档两时段可取;负价 / 非法窗口 / 星期越界 / 节假日形状错全被拒"


@check("配置 接线：Deps 的单价表 / 币种 / 时段规则都来自 config，而且可注入")
def _deps_cost_wiring() -> str:
    from project_rag.cli.deps import Deps
    from project_rag.config import get_settings

    settings = get_settings()
    deps = Deps()
    if deps.cost_prices.keys() != settings.cost.prices().keys():
        raise AssertionError("Deps.cost_prices 该默认取配置(cost.models)")
    if deps.cost_currency != settings.cost.currency:
        raise AssertionError("Deps.cost_currency 该默认取配置")
    expected = _rule()
    if (deps.cost_rule.weekdays, deps.cost_rule.windows) != (
        expected.weekdays,
        expected.windows,
    ):
        raise AssertionError(f"Deps.cost_rule 的星期 / 窗口该取配置的 peak:{deps.cost_rule}")
    if len(deps.cost_rule.holidays) != len(settings.cost.holidays):
        raise AssertionError(
            f"Deps.cost_rule 该带上配置里的 {len(settings.cost.holidays)} 段节假日,"
            f"实际 {len(deps.cost_rule.holidays)}"
        )
    injected = Deps(
        cost_prices={},
        cost_currency="USD",
        cost_rule=_rule(windows=[["01:00", "02:00"]], weekdays=[7]),
    )
    if injected.cost_currency != "USD" or injected.cost_rule.weekdays != frozenset({7}):
        raise AssertionError("注入该覆盖默认值")
    if not deps.cost_rule.split:
        raise AssertionError("config.yaml 里配了高峰窗口,Deps 的规则该是分时段的(split=True)")
    if not deps.cost_rule.holidays:
        raise AssertionError("config.yaml 里的节假日表没走到 Deps.cost_rule")
    return "默认来自 config(星期 + 窗口 + 节假日)/ 注入可覆盖"


@check("配置 config.yaml：价目表 + 时段口径 + 节假日表都在（改价格只需改这一处）")
def _price_table_present() -> str:
    """**这一条盯的是"配置文件里真有那份价目表与时段口径"** —— 作者按图填的数字都在,
    否则报表会一直说"未配置单价"或"全按高峰价",而使用者只会以为是代码坏了。

    最后一条查"空闲价 = 高峰价的一半":那是价目表自己写的一句话,抄错一位数(0.02 写成 0.2)
    会让空闲时段的账差 10 倍,而报表上依旧合理。
    """
    from project_rag.config import get_settings

    settings = get_settings()
    if not settings.cost.models:
        raise AssertionError(
            "config.yaml 的 cost.models 是空的 —— 报表只能报 token,报不出钱。"
            "按 provider 的价目表填上(键是它回报的模型名)"
        )
    if not settings.cost.peak.windows:
        raise AssertionError("cost.peak.windows 是空的(= 不分时段);要分时段就把高峰窗口填上")
    if sorted(settings.cost.peak.weekdays) != WEEKDAYS:
        raise AssertionError(
            f"价目表说的是『周一至周五』 → weekdays 该是 {WEEKDAYS},"
            f"实际 {settings.cost.peak.weekdays}"
        )
    if not settings.cost.holidays:
        raise AssertionError(
            "cost.holidays 是空的 —— 春节/国庆里的工作日会被按高峰价算(贵一倍)。"
            "把国务院放假通知里的日期区间填上(见 config.yaml 的注释)"
        )
    checked: list[str] = []
    for name, price in settings.cost.prices().items():
        for tier, label in (
            (price.input_cache_hit, "输入(命中缓存)"),
            (price.input_cache_miss, "输入(未命中)"),
            (price.output, "输出"),
        ):
            if tier.peak is None:
                raise AssertionError(f"{name} 缺{label}的高峰价")
            if tier.off_peak is None:
                raise AssertionError(f"{name} 缺{label}的空闲价(价目表两档都给全了)")
            if abs(tier.off_peak * 2 - tier.peak) > 1e-9:
                raise AssertionError(
                    f"{name} 的{label}:价目表说『空闲时段价格为高峰时段价格的一半』,"
                    f"配置里是 peak={tier.peak} / off_peak={tier.off_peak} —— 十有八九抄错了一位数"
                )
        checked.append(name)
    # 节假日表的形状:区间别写太长(最长的是春节 9 天),而且别互相重叠(重叠 = 抄重复了)
    from datetime import date as _date

    spans = [(_date.fromisoformat(start), _date.fromisoformat(end)) for start, end in settings.cost.holidays]
    for start, end in spans:
        if (end - start).days > 9:
            raise AssertionError(f"这段节假日有 {(end - start).days + 1} 天({start}~{end}),最长的是春节 9 天")
    ordered = sorted(spans)
    for (_, prev_end), (next_start, _) in zip(ordered, ordered[1:]):
        if next_start <= prev_end:
            raise AssertionError(f"节假日区间重叠了:{prev_end} 与 {next_start}")
    years = sorted({start.year for start, _ in spans})
    return (
        f"{len(checked)} 个模型名配齐了三档两时段(空闲恰好是一半);"
        f"高峰={settings.cost.peak.weekdays}×{settings.cost.peak.windows};"
        f"节假日覆盖 {years[0]}–{years[-1]}"
    )


@check("入口 HTTP：/api/v1/reports/cost 形状、时段行、权限（运维 200 / 员工 403 / 无令牌 401）")
def _cost_endpoint() -> str:
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.application.security import hash_password
    from project_rag.cli.deps import Deps
    from project_rag.domain.auth import User

    class _Ledger:
        def __init__(self, rows): self.rows = rows
        def init_schema(self): ...
        def record(self, row): self.rows.append(dict(row))
        def load_since(self, since): return list(self.rows)

    class _Auth:
        """运维 + 员工两个账号(密码都是 pw-123456)。"""

        def __init__(self):
            self.users = {
                "ops": User(user_id="u-ops", username="ops", role="ops"),
                "emp": User(user_id="u-emp", username="emp", role="employee"),
            }
            self.passwords = {name: hash_password("pw-123456") for name in self.users}
            self.tokens: dict[str, User] = {}

        def init_schema(self): ...
        def create_user(self, username, password_hash, role): raise NotImplementedError
        def find_by_username(self, username):
            for user in self.users.values():
                if user.username == username:
                    return user, self.passwords[user.username]
            return None
        def set_password(self, user_id, password_hash): ...
        def list_users(self): return list(self.users.values())
        def delete_user(self, user_id): return 0
        def issue_token(self, user, token_hash, ttl_seconds): self.tokens[token_hash] = user
        def user_for_token(self, token_hash): return self.tokens.get(token_hash)
        def revoke_token(self, token_hash): self.tokens.pop(token_hash, None)
        def bind_session(self, session_id, user_id): ...
        def owner_of(self, session_id): return None

    rows = [_row(inputs="1000000", cached="800000", outputs="100000")]
    auth = _Auth()
    client = TestClient(
        create_app(
            Deps(
                ledger_store_factory=lambda: _Ledger(rows),
                auth_store_factory=lambda: auth,
                cost_prices=PRICES,
                cost_currency="CNY",
                cost_rule=_rule(),
            )
        )
    )
    ops = _headers_of(client, "ops")
    response = client.get("/api/v1/reports/cost?window=24h", headers=ops)
    if response.status_code != 200:
        raise AssertionError(f"运维该能读消耗报表,实际 {response.status_code}:{response.text[:200]}")
    body = response.json()
    expected_keys = {
        "window", "currency", "metered_calls", "unmetered_calls", "period_unknown_calls",
        "zone_unknown_calls", "calendar_unknown_calls", "cache_unknown_calls",
        "input_tokens", "cached_input_tokens", "output_tokens",
        "total_tokens", "total_cost", "all_priced", "unpriced_models", "periods", "lines",
    }
    if set(body) != expected_keys:
        raise AssertionError(
            f"出参字段与契约不符:多了 {set(body) - expected_keys}、少了 {expected_keys - set(body)}"
        )
    if body["periods"][0]["period"] != "peak" or body["lines"][0]["period"] != "peak":
        raise AssertionError(f"周二 10:00 的调用该落在 peak 时段:{body['periods']}")
    if (body["cached_input_tokens"], body["total_cost"], body["all_priced"]) != (
        800000,
        1.232,
        True,
    ):
        raise AssertionError(f"1M 输入(0.8M 命中)+ 0.1M 输出 该是 1.232 元:{body}")
    if client.get("/api/v1/reports/cost?window=42h", headers=ops).status_code != 400:
        raise AssertionError("窗口白名单外的值该 400")
    if client.get("/api/v1/reports/cost").status_code != 401:
        raise AssertionError("不带令牌该 401(先认证再授权)")
    if client.get("/api/v1/reports/cost", headers=_headers_of(client, "emp")).status_code != 403:
        raise AssertionError("员工看消耗报表该 403")
    return "运维 200 + 字段齐全 + 1.232 元(高峰/命中 0.8M);非法窗口 400、无令牌 401、员工 403"


@check("入口 HTTP：没配单价 / 缺口计数都如实报（不许把不知道画成 0）")
def _cost_endpoint_gaps() -> str:
    from fastapi.testclient import TestClient

    from project_rag.api.app import create_app
    from project_rag.cli.deps import Deps

    class _Ledger:
        def __init__(self, rows): self.rows = rows
        def init_schema(self): ...
        def record(self, row): self.rows.append(dict(row))
        def load_since(self, since): return list(self.rows)

    rows = [
        _row(model="还没填单价的模型", inputs="1000000", cached="0", outputs="0"),
        _row(inputs="1000000", cached="-", outputs="0"),  # 缓存不明
        _row(ts="2026-10-13T10:00:00", inputs="1000000", cached="0", outputs="0"),  # 没带时区
        _row(ts="2027-03-02T10:00:00+08:00", inputs="1000000", cached="0", outputs="0"),  # 没日历
        {"llm_ttft_ms": "900.0", "ts": PEAK_TIME, "llm_model": "-"},  # 没量到 token
    ]
    client = TestClient(
        create_app(
            Deps(
                ledger_store_factory=lambda: _Ledger(rows),
                cost_prices=PRICES,
                cost_rule=_rule(),
            )
        )
    )
    body = client.get("/api/v1/reports/cost?window=24h").json()
    if body["unpriced_models"] != ["还没填单价的模型"] or body["all_priced"]:
        raise AssertionError(f"没配单价该如实报出来:{body}")
    unpriced_line = next(line for line in body["lines"] if line["model"] == "还没填单价的模型")
    if unpriced_line["total_cost"] is not None:
        raise AssertionError(f"那一行的金额该是 null(不是 0):{unpriced_line}")
    got = (
        body["cache_unknown_calls"],
        body["unmetered_calls"],
        body["zone_unknown_calls"],
        body["calendar_unknown_calls"],
        body["period_unknown_calls"],
    )
    if got != (1, 1, 1, 1, 0):
        raise AssertionError(
            f"四个缺口计数不对(缓存 / 没量到 / 时区 / 日历 / 无时间戳):{got}"
        )
    return "unpriced_models 非空 + 行金额 null + 缓存/没量到/时区/日历 四个缺口各 1"


@check("入口 CLI：`project-rag metrics` 的消耗段有模型 × 时段 × 命中缓存三列，与 HTTP 同口径")
def _cost_in_cli_report() -> str:
    import argparse
    import contextlib
    import io

    from project_rag.cli.commands import metrics as metrics_command
    from project_rag.cli.deps import Deps

    class _Ledger:
        def init_schema(self): ...
        def record(self, row): ...
        def load_since(self, since):
            # 一笔高峰 + 一笔空闲:**只传高峰那笔的话,"规则没传下去"这个错看不出来**
            # (实测踩过:CLI 漏传规则 → 所有调用都按高峰价算,报表上依旧合理)。
            return [
                _row(inputs="1000000", cached="800000", outputs="100000", total_ms="1200.0"),
                _row(
                    ts=OFF_TIME,
                    inputs="1000000",
                    cached="800000",
                    outputs="100000",
                    total_ms="900.0",
                ),
            ]

    args = argparse.Namespace(
        deps=Deps(
            ledger_store_factory=lambda: _Ledger(),
            cost_prices=PRICES,
            cost_rule=_rule(),
        ),
        window="24h",
        top=5,
        segment=None,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = metrics_command.run(args)
    text = buffer.getvalue()
    if code != 0:
        raise AssertionError(f"metrics 命令该 0,实际 {code}")
    for needle, why in (
        ("消耗", "消耗段"),
        ("1.232000", "高峰金额(0.8M 命中 + 0.2M 未命中 + 0.1M 输出)"),
        ("0.616000", "空闲金额 —— 高峰的一半(规则真的传下去了才会出现这一行)"),
        ("高峰", "时段列"),
        ("空闲", "时段列"),
        ("命中缓存", "命中缓存那一列"),
        ("800000", "命中数"),
        ("时段口径", "时段口径那一行(哪几天、哪几个钟点算高峰)"),
        ("09:00", "高峰窗口"),
    ):
        if needle not in text:
            raise AssertionError(f"CLI 报表里缺{why}({needle!r}):\n{text}")
    return "命令行与接口同一口径:高峰 1.232000 + 空闲 0.616000,含时段口径与命中缓存列"


def main() -> int:
    return report(
        "P0-7 契约：token 采集 / 三档×两时段折算 / 报表口径",
        notes=(
            "说明：全部内存假件与桩客户端（不需要容器 / 模型 / 网络 / API key）。",
            "口径：命中/未命中/输出三档 × 高峰/空闲两段；时段 = 北京时间工作日 ∩ 09–12/14–18 ∩ 非节假日；",
            "      金额只来自 provider 回报的 token；没量到 / 无时间戳 / 时间戳没时区 / 那一年没日历 /",
            "      缓存不明 / 没配单价 六个缺口都必须能在出参里看见。",
        ),
        hint="提示：单价与时段口径填 config.yaml 的 cost 段（高峰窗口 + 节假日表）；"
        "报表 `GET /api/v1/reports/cost` 或 `project-rag metrics --window 24h`。",
    )


if __name__ == "__main__":
    sys.exit(main())

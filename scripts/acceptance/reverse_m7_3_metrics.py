#!/usr/bin/env python
"""M7.3 的反向测试：把可观测看板与 `capabilities.metrics` 改回坏样子，确认检查真的会红。

用法::

    uv run python scripts/acceptance/reverse_m7_3_metrics.py

**为什么专门有一份**：`check_m7_3_metrics.py` 里有一半是**静态契约**（读前端源码做正则
断言），而静态契约的老毛病是它可能永远为真。这里每条都真的把代码改坏一次，再看那一行变没变红
（"没红过的检查 = 没有检查"，`STATUS.md` §5 第 16 条）。

九条各自的坏法，共同点是**都不会报错**：

* RT-1 `capabilities` 把 p95 的门槛报成 5（前端会解释成"n≥5 就该有 p95"）;
* RT-2 窗口白名单只报 `24h`（后端有五个窗口,界面只给一个）;
* RT-3 `top_max` 报大 10（前端把滑条拖到 51 → 后端 422）;
* RT-4 路由去掉 `top` 的上界（前端以为能要 51 条）;
* RT-5 接口层把 `None` 折算成 `0`（"没量到"变成"很快" —— 最会骗人的那种）;
* RT-6 前端把 `null` 画成 `0.0 ms`（同上,只是搬到了界面上）;
* RT-7 前端写死窗口列表（后端加窗口界面不知道）;
* RT-8 最慢榜不标冷启动（"用户真的等了很久"那条被抹平）;
* RT-9 空窗口不给话术（空白表格被读成"坏了"）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
API_APP = ROOT / "src" / "project_rag" / "api" / "app.py"
VIEW = ROOT / "web" / "src" / "views" / "MetricsView.vue"
SCRIPT = "check_m7_3_metrics.py"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  p95 的门槛报成 5（前端会解释成「n≥5 就该有 p95」）",
        target=API_APP,
        old="        min_n=MetricsMinNOut(p50=MIN_N_P50, p95=MIN_N_P95, p99=MIN_N_P99),",
        new="        min_n=MetricsMinNOut(p50=MIN_N_P50, p95=5, p99=MIN_N_P99),",
        needle="capabilities.metrics.min_n",
    ),
    Case(
        title="RT-2  窗口白名单只报 24h（后端有五个窗口,界面只给一个）",
        target=API_APP,
        old="        windows=list(_WINDOWS),",
        new='        windows=["24h"],',
        needle="capabilities.metrics：窗口白名单",
    ),
    Case(
        title="RT-3  top_max 报大 10（前端拖到 51 → 后端 422）",
        target=API_APP,
        old="        top_max=TOP_MAX,",
        new="        top_max=TOP_MAX + 10,",
        needle="capabilities.metrics.top_max",
    ),
    Case(
        title="RT-4  路由去掉 top 的上界（前端以为能要 51 条）",
        target=API_APP,
        old="        top: int = Query(default=5, ge=1, le=TOP_MAX),",
        new="        top: int = Query(default=5, ge=1),",
        needle="capabilities.metrics.top_max",
    ),
    Case(
        title="RT-5  接口层把「没量到」折算成 0（变成「很快」）",
        target=API_APP,
        old="        segments=[SegmentOut(**asdict(seg)) for seg in summary.segments],",
        new=(
            "        segments=[\n"
            "            SegmentOut(**{**asdict(seg), 'p95': asdict(seg)['p95'] or 0.0})\n"
            "            for seg in summary.segments\n"
            "        ],"
        ),
        needle="接口层不把「样本不够」折算成 0",
    ),
    Case(
        title="RT-6  前端把 null 画成 0.0 ms（同上,只是搬到了界面上）",
        target=VIEW,
        old="  if (value === null || value === undefined) return '—'",
        new="  if (value === null || value === undefined) return '0.0 ms'",
        needle="报表口径在前端被如实画出来",
    ),
    Case(
        title="RT-7  前端写死窗口列表（后端加窗口界面不知道）",
        target=VIEW,
        old='            v-for="item in windows"',
        new="            v-for=\"item in ['1h', '24h']\"",
        needle="窗口选项来自 capabilities",
    ),
    Case(
        title="RT-8  最慢榜不标冷启动（「用户真的等了很久」被抹平）",
        target=VIEW,
        old="                  {{ row.cold ? '冷启动' : '热' }}",
        new="                  {{ '热' }}",
        needle="报表口径在前端被如实画出来",
    ),
    Case(
        title="RT-9  空窗口不给话术（空白表格被读成「坏了」）",
        target=VIEW,
        old="      这个窗口里还没有请求 —— 去「对话」页问几个问题,回来刷新就有了。",
        new="      无数据。",
        needle="窗口选项来自 capabilities",
    ),
)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    for case in CASES:
        original = case.target.read_text(encoding="utf-8")
        hits = original.count(case.old)
        if hits != 1:
            print(
                f"⚠️  {case.title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(case.target, case.old, case.new):
            red, line = run(case.needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""反向测试：把聚合口径与落表逐个改回「坏的样子」，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m5_9.py          # 全部
    uv run python scripts/acceptance/reverse_m5_9.py RT-3     # 只跑某几条

改的是 ``application/metrics.py`` 与 ``observability/ledger_sink.py``（**原地改、`finally` 还原**，
同 `reverse_m5_8.py`）。六个用例都是**真实会有人那么写**的坏版本，不是编出来像坏的：

* RT-1 分位数**插值**（numpy 默认行为，样本少时给出比任何真实观测都好看的数）；
* RT-2 去掉**最小样本量**门槛（n=3 也报 p95）；
* RT-3 把 ``-``（没量到）**折算成 0**（p50 被拉低，而且从数字上看不出来）；
* RT-4 冷启动行**也进分段统计**（p95 于是变成"冷启动有多慢"）；
* RT-5 handler **不过滤**记录（普通日志也往表里写，表里全是垃圾行）；
* RT-6 挂载**不幂等**（重复启动会把同一行账写两遍）;
* RT-7 冷启动**也算进拒答率与通道计数**（"这一版答得怎么样"于是随"这周重启过几次"漂）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m5_9_metrics.py"

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  分位数改成线性插值（样本少时报出没人见过的数）",
        "src/project_rag/application/metrics.py",
        """    rank = max(1, math.ceil(q * n))
    return ordered[min(rank, n) - 1]""",
        """    position = q * (n - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)""",
        "口径 分位数：最近秩取值",
    ),
    (
        "RT-2  去掉最小样本量门槛（n=3 也报 p95）",
        "src/project_rag/application/metrics.py",
        """    if n < min_n or n == 0:""",
        """    if n == 0:""",
        "口径 样本量门槛",
    ),
    (
        "RT-3  把 '-'（没量到）折算成 0",
        "src/project_rag/application/metrics.py",
        """    if value is None or value in ("", "-"):
        return None""",
        """    if value is None or value == "":
        return None
    if value == "-":
        return 0.0""",
        "口径 缺失：'-'",
    ),
    (
        "RT-4  冷启动行也进分段统计（p95 变成冷启动有多慢）",
        "src/project_rag/application/metrics.py",
        """    warm = [row for row in rows if not _flag(row, "cold")]""",
        """    warm = list(rows)""",
        "口径 冷热分层",
    ),
    (
        "RT-5  handler 不过滤（普通日志也往表里写）",
        "src/project_rag/observability/ledger_sink.py",
        """        row = getattr(record, LEDGER_KEY, None)
        if not isinstance(row, dict):
            return""",
        """        row = getattr(record, LEDGER_KEY, None) or {}""",
        "落表 只转交带 ledger 的记录",
    ),
    (
        "RT-7  冷启动也算进拒答率与通道计数（热行那一套拿全部行来数）",
        "src/project_rag/application/metrics.py",
        """        warm=_behavior(warm),
        cold=_behavior(cold),""",
        """        warm=BehaviorCounts(
            rows=len(warm),
            refused=sum(1 for row in rows if _flag(row, "refused")),
            truncated=sum(1 for row in rows if _flag(row, "truncated")),
            aborted=sum(1 for row in rows if _flag(row, "aborted")),
            faq_hits=sum(1 for row in rows if row.get("channel") == "faq"),
            rag_answers=sum(1 for row in rows if row.get("channel") == "rag"),
        ),
        cold=_behavior(cold),""",
        "口径 冷热分层",
    ),
    (
        "RT-6  挂载不幂等（重复启动把同一行账写两遍）",
        "src/project_rag/observability/ledger_sink.py",
        """    for existing in list(logger.handlers):
        if isinstance(existing, LedgerHandler):
            logger.removeHandler(existing)
""",
        """""",
        "落表 只转交带 ledger 的记录",
    ),
]


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    originals: dict[str, str] = {}
    for _, rel, _, _, _ in CASES:
        originals.setdefault(rel, (ROOT / rel).read_text(encoding="utf-8"))
    print(f"改动目标：{len(originals)} 个文件（跑完在 finally 里还原）\n")
    bad = 0
    try:
        for title, rel, old, new, needle in CASES:
            if only and not any(title.startswith(tag) for tag in only):
                continue
            path = ROOT / rel
            original = originals[rel]
            hits = original.count(old)
            if hits != 1:
                print(
                    f"⚠️  {title}\n      锚点在 {rel} 里出现 {hits} 次（必须恰好 1 次）"
                    f"—— 锚点过时了，这条反向测试等于没跑"
                )
                bad += 1
                continue
            with patched_source(path, old, new):
                red, line = run(needle)
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    finally:
        for rel, original in originals.items():
            (ROOT / rel).write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

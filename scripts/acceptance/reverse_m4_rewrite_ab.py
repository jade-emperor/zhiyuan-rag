#!/usr/bin/env python
"""查询改写 A/B 的反向测试:把两臂产物改回「坏的样子」,确认那几条检查真的会红。

用法：

    .venv/bin/python scripts/acceptance/reverse_m4_rewrite_ab.py

**跳过不算通过**:``data/`` 不随仓库发布,公开检出上这几份产物都不存在 ——
那时这份 runner 会明确打印「数据不在本地,跳过」并以 0 退出,
**这不是"反向测试全红"**,别把它的 0 当成证据(与 ``reverse_m4_eval.py`` 同一口径)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import patched_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/acceptance/check_m4_rewrite_ab.py"
EVAL_DIR = ROOT / "data" / "eval"
EVAL_SET = EVAL_DIR / "eval_set_mix_v1.json"
BASELINE_RAW = EVAL_DIR / "datasets" / "mix_v1_norw_raw.json"
REWRITE_RAW = EVAL_DIR / "datasets" / "mix_v1_rw_raw.json"
REPORT = EVAL_DIR / "rewrite_ab_report.md"

REQUIRED = (EVAL_SET, BASELINE_RAW, REWRITE_RAW, REPORT)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑检查、只看带裁决标记的行。输出:(是否真红了, 那一行)。"""
    import subprocess

    proc = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=900)
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    if proc.returncode == 0:
        return False, f"⚠️ 那条红了但整份检查仍然退出 0（报告口径坏了）：{hits[0]}"
    return "[FAIL]" in hits[0], hits[0]


def _first_row_anchor(path: Path) -> str:
    """输入:raw.json。行为:取第一行的 id 当锚点(它在整份文件里唯一)。输出:``"id": "xxxx",``。"""
    row = json.loads(path.read_text(encoding="utf-8"))["rows"][0]
    return f'"id": "{row["id"]}",'


def main() -> int:
    missing = [path for path in REQUIRED if not path.exists()]
    if missing:
        print(
            "数据不在本地(`data/` 不随仓库发布),跳过:\n  "
            + "\n  ".join(str(path.relative_to(ROOT)) for path in missing)
            + "\nowner：改写 A/B —— 造这些产物的命令见 scripts/tools/eval_rewrite_ab.sh 的头部注释。"
        )
        return 0

    first_question = str(json.loads(EVAL_SET.read_text(encoding="utf-8"))["rows"][0]["question"])
    report_text = REPORT.read_text(encoding="utf-8")
    report_line = next((ln for ln in report_text.splitlines() if ln.startswith("| faithfulness |")), "")
    if not report_line:
        print("⚠️ 报告里找不到 faithfulness 那一行,RT-4 无法判定")
        return 1
    cells = [cell.strip() for cell in report_line.strip("|").split("|")]
    # 表格列是:指标 | n | 基线 | 改写 | 差值 | 区间 | 胜负平 —— 改**基线**那一格
    tampered = f"| faithfulness | {cells[1]} | 0.1234 | {cells[3]} | {cells[4]} | {cells[5]} | {cells[6]} |"

    cases: list[tuple[str, Path, str, str, str]] = [
        (
            "RT-1 把基线那一臂的 meta.rewrite 改成 true(两臂就不再只有一个变量了)",
            BASELINE_RAW,
            '"rewrite": false',
            '"rewrite": true',
            "rewrite 标志一真一假",
        ),
        (
            "RT-2 往基线第一行里塞一个 strategy 字段(基线必须是 rewriter=None 那条路)",
            BASELINE_RAW,
            _first_row_anchor(BASELINE_RAW),
            _first_row_anchor(BASELINE_RAW) + '\n      "strategy": "direct",',
            "基线里不许出现改写字段",
        ),
        (
            "RT-3 把评估集里一条问题改一个字(评估集与生成规则就对不上了)",
            EVAL_SET,
            f'"question": "{first_question}"',
            f'"question": "{first_question[:-1]}啊"',
            "重建两次逐字相同",
        ),
        (
            "RT-4 把报告里 faithfulness 的基线值手工改成 0.1234(报告不再由产物算出来)",
            REPORT,
            report_line,
            tampered,
            "四指标表里的每个数都能从两份 rows.csv 重算出来",
        ),
        (
            "RT-5 把改写臂 meta 里的适配器目录清空(读数说不清是哪份权重跑的)",
            REWRITE_RAW,
            '"rewrite_adapter_dir": "logs/lora/run1/lora_adapter"',
            '"rewrite_adapter_dir": ""',
            "改写臂没记下适配器目录",
        ),
    ]

    bad = 0
    for title, path, old, new, needle in cases:
        text = path.read_text(encoding="utf-8")
        hits = text.count(old)
        if hits != 1:
            print(
                f"⚠️  {title}\n      锚点在 {path.relative_to(ROOT)} 里出现 {hits} 次(必须恰好 1 次)"
                f" —— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(path, old, new):
            red, line = run(needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")

    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏数据")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

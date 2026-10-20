#!/usr/bin/env python
"""延迟账本的反向测试:把「一次请求一条账」改回坏样子,确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m5_5_latency.py          # 全部
    uv run python scripts/acceptance/reverse_m5_5_latency.py RT-1     # 只跑某几条

**为什么专门有一份**:账本口径那一半(`check_m5_5_latency.py` 30 条)覆盖的是
"字段对不对、量得准不准",而**"记了几条"**这件事此前是**刻意设计成两条**的
(合流入口一条 + 知识库通道一条,当时认为"合成一条要给日志改协议,不值当")。
那两条账的后果不是多一行日志:报表的请求量、拒答率、通道计数**全部翻倍**,
而翻倍在报表上只表现为"量比想象的大" —— 没人会因此报错。所以这条契约必须自己
有反向用例:改回两条账,看报表那一侧的检查是不是真的会红。

改的是 ``application/answer_rag.py``、``application/answer_question.py`` 与
``observability/latency.py``(**原地改、`finally` 还原**,同 ``reverse_m5_9.py``)。
四个用例都是**真实会有人那么写**的坏版本:

* RT-1 知识库通道**自己再开一条账**(修复前就是这么多年的写法:内层认为自己才是
  "一次调用",于是给每一次请求都多记一行);
* RT-2 把已经交下去的那个账本**再包一层 `timed`**(一种"顺手也观察一下"的写法:
  `emit` 是幂等的所以看不出来,但每个 token 会被数两遍);
* RT-3 那两路召回**先 `join` 再跑下一路**(想"先等它回来再干别的,顺序更清楚")——
  线程还在,但两路又变回串行,墙钟等于两段之和;
* RT-4 分段**退回"相邻打点之差"**(并行之后这两格量不出各自的时长:先结束的那一路
  会被算成负数/零,而"等另一路"的时间会被算进融合段)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_m5_5_latency.py"
# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  知识库通道自己再开一条账（一次请求两行 → 请求量翻倍）",
        "src/project_rag/application/answer_rag.py",
        """    if timing is not None:
        yield from _rag_frames(
            query,
            embedder=embedder,
            store=store,
            llm=llm,
            keyword_index=keyword_index,
            reranker=reranker,
            options=options,
            history=history,
            timing=timing,
            rewriter=rewriter,
            image_signer=image_signer,
        )
        return
    own = StreamTiming(label="rag", channel="rag", thinking=thinking)""",
        """    own = StreamTiming(label="rag", channel="rag", thinking=thinking)""",
        "链路 合流走知识库：**一条**账",
    ),
    (
        "RT-2  把交下去的账本再包一层 timed（同一行的 tokens 数两遍）",
        "src/project_rag/application/answer_question.py",
        """    for event in knowledge(_history(session), timing):""",
        """    for event in timed(knowledge(_history(session), timing), timing):""",
        "链路 帧数只数一遍",
    ),
    (
        "RT-3  两路召回先 join 再跑下一路（线程还在，但两路又变回串行）",
        "src/project_rag/application/answer_rag.py",
        """        worker.start()
        started = time.perf_counter()""",
        """        worker.start()
        worker.join()  # 坏版本:先等关键字那一路跑完,再跑稠密那一路
        started = time.perf_counter()""",
        "两路召回并行",
    ),
    (
        "RT-4  分段退回「相邻打点之差」（并行时量不出各自时长，融合段吸走等待）",
        "src/project_rag/observability/latency.py",
        """        reported = self.spans.get(name)
        if reported is not None:
            return reported
        return self._gap(start, end)""",
        """        return self._gap(start, end)""",
        "只开关键字那一路",
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

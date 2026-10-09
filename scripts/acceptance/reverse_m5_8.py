"""M5.8 反向测试:逐个把产品改回「坏的样子」,确认对应的新检查真的会红。

**为什么要有这个文件**:验收检查最容易骗人的失败方式是「无论产品怎样都绿」。
每加一条口径/契约,就得把产品改坏一次、看它红一次 —— 红不了就说明检查没在测它。
(`docs/STATUS.md` §5 第 16 / 23 条;反向测试不是可选项。)

用法(会临时改 src/ 下的文件,**跑完自动还原**)::

    uv run python scripts/acceptance/reverse_m5_8.py

每个用例 = (说明, 文件, 原文, 改后, 期望红的检查名片段, 跑哪个验收脚本)。
锚点必须**唯一**:出现 0 次或多次就跳过并计为失败(说明锚点过时了 —— 产品被重构过)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
RAG = ROOT / "src" / "project_rag" / "application" / "answer_rag.py"
LAT = ROOT / "src" / "project_rag" / "observability" / "latency.py"
ADAPTER = ROOT / "src" / "project_rag" / "adapters" / "llm_deepseek.py"

THINK_CALL = """        if delta.reasoning:
            # 思考只记账、**不外发**:这一轮想没想、想了多久要看得见,但草稿不该进答案
            # (也不进 token 帧 —— 前端此刻只该等正文)。阈值/口径见 observability/latency.py
            timing.think(delta.reasoning_text)"""

CASES: list[tuple[str, Path, str, str, str, str]] = [
    (
        "RT-1 思考混进答案(还当成 token 帧发出去)",
        RAG,
        THINK_CALL,
        """        if delta.reasoning:
            timing.think(delta.reasoning_text)
            parts.append(delta.reasoning_text)
            yield QaEvent(kind="token", text=delta.reasoning_text)""",
        "链路 思考入账",
        "check_m5_5_latency.py",
    ),
    (
        "RT-2 思考当成正文 token 数进去(口径串了)",
        RAG,
        "            timing.think(delta.reasoning_text)",
        "            timing.token(delta.reasoning_text)",
        "链路 思考入账",
        "check_m5_5_latency.py",
    ),
    (
        "RT-3 档位不往账本里传(账本该是唯一真值来源)",
        RAG,
        'StreamTiming(label="rag", channel="rag", thinking=thinking)',
        'StreamTiming(label="rag", channel="rag")',
        "档位 传递",
        "check_m5_5_latency.py",
    ),
    (
        "RT-4 思考窗口取第一帧而不是最后一帧(少算一半)",
        LAT,
        'return self._since("llm_start", self.last_thought)',
        'return self._since("llm_start", self.first_thought)',
        "口径 思考窗口",
        "check_m5_5_latency.py",
    ),
    (
        "RT-5 没思考时编一个 0.0(把「没想」装成「量到了」)",
        LAT,
        'return self._since("llm_start", self.last_thought)',
        'return 0.0 if self.last_thought is None else self._since("llm_start", self.last_thought)',
        "口径 思考 没想",
        "check_m5_5_latency.py",
    ),
    (
        "RT-6 账本给「没传档位」的调用编一个 auto",
        LAT,
        "    thinking: str | None = None",
        '    thinking: str | None = "auto"',
        "档位：",
        "check_m5_5_latency.py",
    ),
    (
        "RT-7 适配器又把思考吞掉(M5.8 之前的样子)—— 真 provider 检查也要红",
        ADAPTER,
        "            if thought:\n                yield LlmDelta(reasoning_text=thought)\n",
        "",
        "thinking=off 真的不思考",
        "check_m5_stream.py",
    ),
]


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:验收脚本名 + 期望红的检查名片段。行为:跑它、找那一行。输出:(是否 FAIL, 该行)。

    片段必须只命中**一行**:第一次跑 RT-7 时写的是「真实链路」,结果命中的是同名的另一条
    通过了的检查 —— 于是「检查没红」被误读成「检查是摆设」。假绿比假红更坏,所以这里
    命中多行直接算失败(与验收脚本自己那条「不许松散关键字断言」是同一个教训)。

    只在**带裁决标记**的行里找([PASS]/[FAIL]/[SKIP]):脚本末尾还会把失败的检查名再列一遍,
    不排除那些行的话每条都会「命中 2 行」。
    """
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), f"scripts/acceptance/{script}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
        env={
            "PATH": "/usr/bin:/bin",
            "UV_CACHE_DIR": "/tmp/rag-uvcache",
            "RAG_EMBEDDING__DEVICE": "cpu",
            "HOME": os.environ.get("HOME", "/tmp"),   # 不写死本机家目录
        },
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [ln.strip() for ln in proc.stdout.splitlines() if any(m in ln for m in marks)]
    hits = [ln for ln in body if needle in ln]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行,反向测试无法判定:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]  # 可选:只跑某几条(如 RT-1)
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    for title, path, old, new, needle, script in CASES:
        if only and not any(title.startswith(tag) for tag in only):
            continue
        original = path.read_text(encoding="utf-8")
        hits = original.count(old)
        if hits != 1:
            print(f"⚠️  {title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑")
            bad += 1
            continue
        with patched_source(path, old, new):
            red, line = run(script, needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

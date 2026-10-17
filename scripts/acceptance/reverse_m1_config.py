#!/usr/bin/env python
"""配置的反向测试：把新加的旋钮改回「坏的样子」，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m1_config.py        # 全部
    uv run python scripts/acceptance/reverse_m1_config.py RT-1   # 只跑某几条

**为什么需要它**：`check_m1_1_config` 与 `check_m3_rag` 里新加的三条（类默认值、
读一致性接线、重排半精度接线）在 之前**一条反向用例都没有** —— 也就是说
"改坏了会不会红"从来没被证明过。这份 runner 补上这一格：

* RT-1/RT-4/RT-5/RT-6 改 `config.py` 的**类默认值**（默认值一改语义就变，这正是要拦的）；
* RT-2 把 `search` 的一致性从"配置"改回"写死 strong"（那 200× 就白省了，而且没人知道）；
* RT-3 把重排的精度开关从 `rerank_use_f16` 改回 `use_f16`（两个模型又被绑在一起，
  而这条从代码上完全看不出来 —— 只有测试能拦）。

两条断言纪律（与 `reverse_p0_4.py` 同源）：锚点必须**恰好出现一次**；期望红的检查名片段
只能命中**一行**（且只在带 `[PASS]/[FAIL]/[SKIP]` 裁决标记的行里找）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]

# (标题, 相对路径, 锚点, 改成, 期望红的检查名片段, 跑哪份检查)
CASES: list[tuple[str, str, str, str, str, str]] = [
    (
        "RT-1 读一致性的类默认值改成 bounded（默认值一改就悄悄放松了读己之写）",
        "src/project_rag/config.py",
        'read_consistency: Literal["bounded", "eventually", "strong"] = "strong"',
        'read_consistency: Literal["bounded", "eventually", "strong"] = "bounded"',
        "读一致性 strong、重排半精度 false",
        "check_m1_1_config.py",
    ),
    (
        "RT-2 search 不再听配置、写死 strong（400ms/次 又回来了，而且是静默的）",
        "src/project_rag/adapters/milvus_store.py",
        "            filter=f'source == \"{source}\"' if source is not None else \"\",\n"
        "            consistency_level=self._search_consistency,\n"
        "        )\n"
        "        return [\n"
        "            RetrievedChunk(chunk=self._to_chunk(hit), score=float(hit[\"distance\"]))\n"
        "            for hit in hits[0]\n"
        "        ]\n\n"
        "    def search_sparse(",
        "            filter=f'source == \"{source}\"' if source is not None else \"\",\n"
        "            consistency_level=_WRITE_READ_CONSISTENCY,\n"
        "        )\n"
        "        return [\n"
        "            RetrievedChunk(chunk=self._to_chunk(hit), score=float(hit[\"distance\"]))\n"
        "            for hit in hits[0]\n"
        "        ]\n\n"
        "    def search_sparse(",
        "读一致性：search 走配置档",
        "check_m3_rag.py",
    ),
    (
        "RT-3 重排的精度开关改回共用 use_f16（嵌入改一下、重排跟着变，看不出来）",
        "src/project_rag/adapters/reranker_bge.py",
        'if config.rerank_use_f16 and config.device.startswith("cuda"):',
        'if config.use_f16 and config.device.startswith("cuda"):',
        "重排半精度：rerank_use_f16",
        "check_m3_rag.py",
    ),
    (
        "RT-4 重排半精度的类默认值改成 True（别人的机器会被默认开上 fp16）",
        "src/project_rag/config.py",
        "    rerank_use_f16: bool = False",
        "    rerank_use_f16: bool = True",
        "读一致性 strong、重排半精度 false",
        "check_m1_1_config.py",
    ),
    (
        "RT-5 索引类型的类默认值改成 IVF_FLAT（新集合一律变近似检索）",
        "src/project_rag/config.py",
        '    type: Literal["FLAT", "IVF_FLAT"] = "FLAT"',
        '    type: Literal["FLAT", "IVF_FLAT"] = "IVF_FLAT"',
        "索引 FLAT、关键字后端 memory",
        "check_m1_1_config.py",
    ),
    (
        "RT-6 关键字后端的类默认值改成 milvus（老集合写入/检索会当场报错）",
        "src/project_rag/config.py",
        '    backend: Literal["memory", "milvus"] = "memory"',
        '    backend: Literal["memory", "milvus"] = "milvus"',
        "索引 FLAT、关键字后端 memory",
        "check_m1_1_config.py",
    ),
]


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:检查脚本名 + 期望红的检查名片段。行为:跑它、只看带裁决标记的行。输出:(是否真红了, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "acceptance" / script)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(ROOT),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    if proc.returncode == 0:
        return False, f"⚠️ 那条红了但整份检查仍然退出 0（报告口径坏了）：{hits[0]}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码，已自动还原：{stale}\n")
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    bad = 0
    for title, rel, old, new, needle, script in CASES:
        if only and not any(title.startswith(tag) for tag in only):
            continue
        target = ROOT / rel
        original = target.read_text(encoding="utf-8")
        hits = original.count(old)
        if hits != 1:
            print(
                f"⚠️  {title}\n      锚点在 {rel} 里出现 {hits} 次（必须恰好 1 次）"
                f"—— 锚点过时了，这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(target, old, new):
            red, line = run(script, needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏配置")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

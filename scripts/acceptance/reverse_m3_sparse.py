#!/usr/bin/env python
"""`check_m3_sparse.py` 的反向测试：把索引类型与稀疏那一路改回坏样子，确认检查真的会红。

用法::

    uv run python scripts/acceptance/reverse_m3_sparse.py

十一条各自的坏法，共同点是**都不会报错**（只是配置不生效、权重算错、或者权重被静默丢掉）：

* RT-1 建集合时把索引类型写死 FLAT（配置里改成 IVF_FLAT 也不生效）;
* RT-2 检索时不传 ``search_params``（IVF_FLAT 的 nprobe 丢了，回退成默认探几个簇）;
* RT-3 词维度改用内置 ``hash()``（**按进程加盐**：入库与检索两个进程落到不同维度，
  表现是"检索结果永远是空"，而且不报错）;
* RT-4 保留维度 0 不再排他（真实词可能落到 0，和空块占位撞在一起）;
* RT-5 空文本编码成空向量（Milvus 拒收整行，灌到一半才炸）;
* RT-6 写入时把稀疏向量丢掉（集合有那一列却写空 —— 检索那边一条都命中不了）;
* RT-7 稀疏检索不滤 0 分（候选不足 k 时，一个词都没沾上的行会被当成命中）;
* RT-8 没 fit 也能编码（idf 表是空的，权重静默算错）；
* RT-11 ``bm25_sparse`` 自己 import jieba —— **第三个分词用户从「口径只有一处」里漏出去**：
  入库按一种切法算权重、检索按另一种切法编码查询，命中率静默掉一截（这条由 ``check_m3_rag`` 盯着）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "project_rag" / "adapters" / "milvus_store.py"
DEPS = ROOT / "src" / "project_rag" / "cli" / "deps.py"
INGEST = ROOT / "src" / "project_rag" / "application" / "ingest_documents.py"
SPARSE = ROOT / "src" / "project_rag" / "adapters" / "bm25_sparse.py"
SCRIPT = "check_m3_sparse.py"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)
    script: str = SCRIPT  # 少数坏法由**别的**契约盯着(例如分词口径那条在 check_m3_rag)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  建集合时索引类型写死 FLAT（配置不生效）",
        target=STORE,
        old='            index_type=self._index.type,  # FLAT(精确)/ IVF_FLAT(近似,探 nprobe 个簇)',
        new='            index_type="FLAT",',
        needle="建集合时的索引类型与参数来自配置",
    ),
    Case(
        title="RT-2  检索时不传 search_params（IVF_FLAT 的 nprobe 丢了）",
        target=STORE,
        old="            search_params=self._dense_search_params(),",
        new="",
        needle="检索时的 search_params 跟着配置走",
    ),
    Case(
        title="RT-3  词维度改用内置 hash()（跨进程不稳定，检索永远空）",
        target=SPARSE,
        old='    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()\n'
        '    return 1 + int.from_bytes(digest, "big") % (_HASH_SPACE - 1)',
        new="    return 1 + hash(term) % (_HASH_SPACE - 1)",
        needle="词维度跨**进程**稳定",
    ),
    Case(
        title="RT-4  保留维度 0 不再排他（真实词可能与空块撞车）",
        target=SPARSE,
        old='    return 1 + int.from_bytes(digest, "big") % (_HASH_SPACE - 1)',
        new='    return int.from_bytes(digest, "big") % (_HASH_SPACE - 1)',
        needle="保留维度 0 被排除在映射之外",
    ),
    Case(
        title="RT-5  空文本编码成空向量（Milvus 会拒收整行）",
        target=SPARSE,
        old="            vectors.append(vector or {_EMPTY_DIM: _EMPTY_WEIGHT})",
        new="            vectors.append(vector)",
        needle="空文本也能写出非空向量",
    ),
    Case(
        title="RT-6  写入时把稀疏向量丢掉（那一列写进去是空的）",
        target=STORE,
        old="        if sparse is not None:\n"
        "            # 空稀疏行会被 Milvus 拒收(`empty sparse float vector row`)—— 编码器已经保证",
        new="        if False:\n"
        "            # 空稀疏行会被 Milvus 拒收(`empty sparse float vector row`)—— 编码器已经保证",
        needle="集合有稀疏列时",
    ),
    Case(
        title="RT-7  稀疏检索不滤 0 分（没沾上的行被当成命中）",
        target=STORE,
        old="            if float(hit[\"distance\"]) > 0.0",
        new="            if True  # noqa: SIM103 - 坏版本:0 分也算命中",
        needle="稀疏检索：只认 IP",
    ),
    Case(
        title="RT-9  无条件装配稀疏编码器（memory 那档也会带稀疏向量去撞没有那一列的集合）",
        target=DEPS,
        old=(
            "        sparse_encoder_factory=(\n"
            '            _sparse_encoder if settings.keyword.backend == "milvus" else None\n'
            "        ),"
        ),
        new="        sparse_encoder_factory=_sparse_encoder,",
        needle="deps 只在 milvus 那档装配稀疏编码器",
    ),
    Case(
        title="RT-10  去掉用例层的预检（要等嵌入跑完、第一批 upsert 才报错）",
        target=INGEST,
        old="        if not store.has_sparse_column():\n            raise RuntimeError(",
        new="        if False:\n            raise RuntimeError(",
        needle="嵌入之前",
    ),
    Case(
        title="RT-8  没 fit 也能编码（idf 表空着，权重静默算错）",
        target=SPARSE,
        old="        if self._n_docs == 0:\n"
        "            raise RuntimeError(",
        new="        if False:\n"
        "            raise RuntimeError(",
        needle="未 fit 就编码要报错",
    ),
    Case(
        title="RT-11 稀疏那一路自己 import jieba（第三个用户从「分词口径只有一处」里漏出去）",
        target=SPARSE,
        old="from project_rag.adapters.chinese_tokens import tokens",
        new=(
            "import jieba as _jieba  # 反向测试:自己分词,不走共享口径\n"
            "\n"
            "\n"
            "def tokens(text: str) -> list[str]:\n"
            "    return [piece for piece in _jieba.lcut(text) if piece.strip()]"
        ),
        needle="分词口径只有一处",
        script="check_m3_rag.py",
    ),
)


def run(needle: str, script: str = SCRIPT) -> tuple[bool, str]:
    """输入:期望红的检查名片段 + 跑哪份契约。行为:跑它、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{script}"],
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
            red, line = run(case.needle, case.script)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

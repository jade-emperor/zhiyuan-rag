"""多格式入库 反向测试:逐个把产品改回「坏的样子」,确认对应的检查真的会红。

**为什么要有这个文件**:验收检查最容易骗人的失败方式是「无论产品怎样都绿」。
每加一条口径,就得把产品改坏一次、看它红一次 —— 红不了就说明检查没在测它。
(反向测试不是可选项。)

用法(会临时改 src/ 下的文件,**跑完自动还原**)：

    uv run python scripts/acceptance/reverse_doc_formats.py
    uv run python scripts/acceptance/reverse_doc_formats.py RT-4   # 只跑某几条

每个用例 = (说明, 文件, 原文, 改后, 期望红的检查名片段, 跑哪个验收脚本)。
锚点必须**唯一**:出现 0 次或多次就跳过并计为失败(说明锚点过时了 —— 产品被重构过)。

覆盖的口径(对应 ``check_doc_formats.py`` 里的检查):
BOM 吃不吃 / 排序 / ``source`` 去掉 ``_data`` / docx 表格 / PDF 书签 /
空文本与不认识的文件两份清单 / 折行接法 / pptx 无标题页占位。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "project_rag"
TEXT = SRC / "adapters" / "text_files.py"
LOADER = SRC / "adapters" / "document_loader.py"
OFFICE = SRC / "adapters" / "office_files.py"
PDF = SRC / "adapters" / "pdf_files.py"
BLOCKS = SRC / "adapters" / "_text_blocks.py"

SCRIPT = "check_doc_formats.py"

# 三处锚点原文(与产品里逐字一致;产品一改,反向测试会自己报「锚点过时」)
_EMPTY_GUARD = """        if not text.strip():
            empty.append(relative)
            continue
"""
_SKIP_GUARD = """        if source is None:
            skipped.append(relative)
            continue
"""
_TABLE_BRANCH = """        elif tag == "tbl":
            table = Table(element, document)
            markdown = _table_to_markdown(
                [cell.text for cell in row.cells] for row in table.rows
            )
            if markdown:
                yield markdown
"""

CASES: list[tuple[str, Path, str, str, str, str]] = [
    (
        "RT-1 纯文本不吃 BOM(第一个标题会变成 \\ufeff#",
        TEXT,
        'return path.read_text(encoding="utf-8-sig")',
        'return path.read_text(encoding="utf-8")',
        "纯文本：读原文",
        SCRIPT,
    ),
    (
        "RT-2 装载不排序(同一份语料两次顺序可能不同 → chunk_id 幂等没了)",
        LOADER,
        # 锚点跟着"登记根"那轮改动走:排序键从 `root` 换成了每个文件自己的登记根
        # (`anchor_of`),否则从语料根灌与从子目录灌的顺序会不一样。
        "for file in sorted(candidates, key=lambda found: doc_id_of(anchor_of(found), found)):",
        "for file in candidates:",
        "装载：混合目录",
        SCRIPT,
    ),
    (
        "RT-3 source 不去掉 _data(过滤字段与库里对不上)",
        LOADER,
        "    return root.name.removesuffix(\"_data\") or root.name",
        "    name = root.name",
        "装载：混合目录",
        SCRIPT,
    ),
    (
        "RT-4 docx 不收表格(Document.paragraphs 里没有表格,表会静默丢)",
        OFFICE,
        _TABLE_BRANCH,
        '        elif tag == "tbl":\n            continue\n',
        "docx：Title",
        SCRIPT,
    ),
    (
        "RT-5 PDF 不用书签(标题层级没了,父块在任意位置断开)",
        PDF,
        "            blocks = list(headings.get(number, []))",
        "            blocks = []",
        "pdf：书签目录",
        SCRIPT,
    ),
    (
        "RT-6 空文档也塞进 documents(切不出块,块数对不上还查不出为什么)",
        LOADER,
        _EMPTY_GUARD,
        "",
        "装载：混合目录",
        SCRIPT,
    ),
    (
        "RT-7 不认识的文件一声不吭(少入库一份文档没人知道)",
        LOADER,
        _SKIP_GUARD,
        "        if source is None:\n            continue\n",
        "装载：混合目录",
        SCRIPT,
    ),
    (
        "RT-8 中文折行补空格(把一个词切成两半)",
        BLOCKS,
        'separator = "" if is_cjk(merged[-1]) and is_cjk(line[0]) else " "',
        'separator = " "',
        "文本整理",
        SCRIPT,
    ),
    (
        "RT-9 pptx 没标题的页不给占位(整页文字跟前一页粘成一块)",
        OFFICE,
        '        yield f"## {title or f\'第 {number} 页\'}"',
        '        yield f"## {title}"',
        "pptx：每页一个",
        SCRIPT,
    ),
]


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:验收脚本名 + 期望红的检查名片段。行为:跑它、在裁决行里找那一行。输出:(是否 FAIL, 该行)。

    片段必须只命中**一行**:命中的是另一条同名的通过检查时,「检查没红」会被误读成
    「检查是摆设」。假绿比假红更坏,所以命中多行直接算失败。
    只在**带裁决标记**的行里找([PASS]/[FAIL]/[SKIP]):脚本末尾还会把失败的名字再列一遍。
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
            "HOME": str(Path.home()),
        },
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行,反向测试无法判定:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
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
            print(
                f"⚠️  {title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑"
            )
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

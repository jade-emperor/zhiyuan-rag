#!/usr/bin/env python
"""P0-5 反向测试:把页码那条链路改回坏样子,确认契约真的会红。

用法::

    uv run python scripts/acceptance/reverse_p0_5.py

改的是 ``adapters/pdf_files.py`` / ``document_loader.py`` / ``chunker_recursive.py`` / ``milvus_store.py``、
``application/ingest_documents.py``、``api/app.py``、``cli/commands/drop.py``
(原地改、`finally` 还原,并带崩溃备份)。十个坏版本**都不会报错**,只是让页号错位或丢掉:

* RT-1 算偏移时忘掉 ``\\n\\n`` 那两个字符(每翻一段少算 2,越往后错得越多);
* RT-2 分隔符长度算错一个字符(``cursor += 1``);
* RT-3 装载器不走 ``read_with_pages``(区间表进不了 ``Document``,页码在装载那一步就丢了);
* RT-4 切分器不带 ``add_start_index``(偏移没了,langchain 也不报错);
* RT-5 存储层的哨兵漏进领域层(没页码变成 ``-1``,而 ``-1`` 会被当成一个真实页号读);
* RT-6 用例层不拿偏移算页码(``Chunk.page`` 全空,一路静默到引用上);
* RT-7 出参漏抄页码(``Citation`` 上有,``CitationOut`` 里没有 —— domain 层全对,只有 HTTP 出参那一格空了);
* RT-8 跨页块取**结束页**而不是起始页(听起来更"完整",实际是让用户翻错页)。
* RT-9 `drop` **不给 `--yes` 也照删**(破坏性动作的「默认只说不做」被改掉);
* RT-10 `drop` 删的是**当前配置指向的集合**而不是点名的那个(迁移时正好会删错库)。

**锚点为什么这么写**:锚点是**逐字符照抄**源码里的那几行(含缩进),所以
``"\\n\\n"`` 这种转义序列必须写成 ``\\\\n\\\\n`` —— 它代表源码文件里的**两个字符**反斜杠+n。
RT-3 的锚点是从 ``document_loader.py`` 里直接读出来的,而且 ``main`` 会先核对
「锚点在文件里恰好出现 1 次」:不核对的话,一个对不上的锚点会让**什么都没改**的源码
跑出一片绿,然后被读成「这个检查是摆设」—— 反向测试自己必须先能证伪。

    **为什么改源码前后都要清字节码**（真踩过):契约报「区间到 66,正文 68」
    (像少算了一个分隔符),而源码里明明是 ``cursor += 2``;删掉 ``__pycache__/pdf_files.*.pyc``
    后立刻全绿。**两个方向都会骗人**:假红(改坏的字节码留着),以及更危险的**假绿**
    (反向测试读到好版本的旧字节码 → 报「检查是摆设」,或者真回归被漏掉)。
    这件事现在统一由 ``_harness`` 负责:import 时整体清一遍 + ``patched_source`` 还原后再清一次。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_p0_5_pages.py"

ADAPTERS = ROOT / "src" / "project_rag" / "adapters"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)
    script: str = SCRIPT  # 这条坏版本该由哪个检查脚本抓住


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  算偏移时忘掉 \\n\\n（越往后错得越多）",
        target=ADAPTERS / "pdf_files.py",
        old='                text_parts.append("\\n\\n")\n                cursor += 2',
        new='                text_parts.append("\\n\\n")',
        needle="区间首尾相接且覆盖全文",
    ),
    Case(
        title="RT-2  分隔符长度算错（cursor += 1，后面每页都偏一位）",
        target=ADAPTERS / "pdf_files.py",
        old='                text_parts.append("\\n\\n")\n                cursor += 2',
        new='                text_parts.append("\\n\\n")\n                cursor += 1',
        needle="区间首尾相接且覆盖全文",
    ),
    Case(
        title="RT-3  装载器不走 read_with_pages（区间表在装载那一步丢掉）",
        target=ADAPTERS / "document_loader.py",
        old=(
            "        reader = getattr(source, \"read_with_pages\", None)\n"
            "        if reader is not None:\n"
            "            text, pages = reader(file)\n"
            "        else:\n"
            "            text = source.read(file)\n"
        ),
        new="        text = source.read(file)\n",
        needle="装载器把区间表带进 Document",
    ),
    Case(
        title="RT-4  切分器不带 add_start_index（偏移没了，langchain 也不报错）",
        target=ADAPTERS / "chunker_recursive.py",
        old="        add_start_index=True,\n",
        new="",
        needle="每块起始偏移都切得出它自己",
    ),
    Case(
        title="RT-5  存储层哨兵漏进领域层（没页码读成 -1，像个真实页号）",
        target=ADAPTERS / "milvus_store.py",
        old=(
            "        page = int(value)\n"
            "        return page if page > 0 else None\n"
        ),
        new=(
            "        page = int(value)\n"
            "        return page\n"
        ),
        needle="Milvus 往返",
    ),
    Case(
        title="RT-6  用例层不拿偏移算页码（Chunk.page 全空，一路静默到引用上）",
        target=ROOT / "src" / "project_rag" / "application" / "ingest_documents.py",
        old="                        page=_page_of_child(document, group, child_index),\n",
        new="",
        needle="chunk_documents 用偏移算页码",
    ),
    Case(
        title="RT-7  出参漏抄页码（库里/引用上都有，HTTP 出参里没有 → 前端只能显示「页码未知」）",
        target=ROOT / "src" / "project_rag" / "api" / "app.py",
        old="            page=citation.page,\n",
        new="",
        needle="引用该带子块的页码",
        script="check_m5_api.py",  # 这一坏法只有接口那层看得见(domain 层它是对的)
    ),
    Case(
        title="RT-9  drop 不给 --yes 也照删（「默认只说不做」变成「默认就删」）",
        target=ROOT / "src" / "project_rag" / "cli" / "commands" / "drop.py",
        old="    if not args.yes:\n",
        new="    if False:\n",
        needle="集合运维：drop",
    ),
    Case(
        title="RT-10  drop 删的是「当前配置指向的集合」而不是点名的那个（迁移时正好会删错库）",
        target=ROOT / "src" / "project_rag" / "cli" / "commands" / "drop.py",
        old="    removed = deps.drop_collection_factory(name)\n",
        new=(
            "    from project_rag.config import get_settings as _gs\n"
            "\n"
            "    removed = deps.drop_collection_factory(_gs().milvus.collection)\n"
        ),
        needle="集合运维：drop",
    ),
    Case(
        title="RT-8  跨页块取「结束页」而不是「起始页」（用户翻到那一页会翻到下一段去）",
        target=ROOT / "src" / "project_rag" / "application" / "ingest_documents.py",
        old="    return page_of(document.pages, group.child_starts[child_index])",
        new=(
            "    return page_of(\n"
            "        document.pages,\n"
            "        group.child_starts[child_index] + len(group.children[child_index]) - 1,\n"
            "    )"
        ),
        needle="chunk_documents 用偏移算页码",
    ),
)


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:检查脚本名、期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{script}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120, env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    """输入:无。行为:逐个改坏→跑契约→还原,统计哪几条没红。输出:退出码。

    **改源码一律走 `_harness.patched_source`**(不再自己写一份备份逻辑):
    它把备份落在**镜像源码路径**的 `.cache/reverse_backup/` 下,于是**任何** runner 启动时
 的 `restore_stale_patches` 都能扫到并还原。真踩过:这份 runner 原来用自己的
    `备份名 = <文件名>.py`(扁平),被 SIGTERM 打断后留下的坏源码**别的 runner 看不见** ——
    紧接着跑全量回归,检查读到的是被改坏的 `ingest_documents.py`,报出来的却是"页码契约失败"
    (`STATUS.md` §0.3 第 37 条)。
    """
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")

    bad = 0
    for case in CASES:
        original = case.target.read_text(encoding="utf-8")
        hits = original.count(case.old)
        if hits != 1:
            print(
                f"⚠️  {case.title}\n      锚点出现 {hits} 次(必须恰好 1 次) —— 源码变了,先更新锚点"
            )
            bad += 1
            continue
        with patched_source(case.target, case.old, case.new):
            red, line = run(case.script, case.needle)
        if not red:
            bad += 1
            if "[SKIP]" in line:
                line += "\n      ⚠️ 这一条是 SKIP(环境不满足,比如 Milvus 没起)—— 反向测试证明不了它"
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

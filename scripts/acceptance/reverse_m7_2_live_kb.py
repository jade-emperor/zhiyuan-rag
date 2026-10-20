#!/usr/bin/env python
"""补丁（实时清单）的反向测试：把"三处对账"改回坏样子，确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m7_2_live_kb.py

**这一份在堵什么**：实时清单的每一条断言都可能写成"永远为真"（比如断言一个字段存在、
断言一个计数等于它自己）。这里每条都真的把产品代码 / 前端源码改坏一次，再看那一行变没变红
（"没红过的检查 = 没有检查"）。

十条坏法的共同点是**都不会报错**，看起来一切正常：

* RT-1 只念登记表（磁盘与向量库都不看）—— 这一条就是那个 bug 本身;
* RT-2 ``searchable`` 按 ``status`` 算（登记说 processed 就算"搜得到"）;
* RT-3 状态优先级反过来（库内没块压过文件不在）—— 让人白跑一趟重灌;
* RT-4 在途/失败的行也去做一致性判定（pending 被报成"库里没块"）;
* RT-5 磁盘上有、登记表没有的文件不进清单（后端放进去的文件前端看不见）;
* RT-6 磁盘索引丢掉"多入库根"的候选 id（从语料根灌的登记行被误报成文件不在）;
* RT-7 CLI ingest 不登记（后端灌的语料在前端永远是"未登记"）;
* RT-8 删除时把语料源材料一起删了（不可逆）;
* RT-9 卡片回到按 ``status`` 显示（"已入库"再次由登记表说了算）;
* RT-10 上传不原子改名（中断留下的半截文件在清单上装成一份"未入库"的文档）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
DOCS_APP = ROOT / "src" / "project_rag" / "application" / "documents.py"
API_APP = ROOT / "src" / "project_rag" / "api" / "app.py"
LOADER = ROOT / "src" / "project_rag" / "adapters" / "document_loader.py"
INGEST = ROOT / "src" / "project_rag" / "cli" / "commands" / "ingest.py"
TABLE = ROOT / "web" / "src" / "components" / "DocumentTable.vue"
SCRIPT = "check_m7_2_live_kb.py"


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
        title="RT-1  清单只念登记表（磁盘与向量库都不看）—— 这个 bug 本身",
        target=API_APP,
        old="        files=_disk_files(deps),\n        indexed=_factory(deps, \"vector_store_factory\").chunk_counts(),",
        new="        files=(),\n        indexed={},",
        needle="清单：登记说已入库、库里一块没有",
    ),
    Case(
        title="RT-2  searchable 按 status 算（登记说 processed 就算「搜得到」）",
        target=DOCS_APP,
        old="        searchable=indexed_chunks > 0,\n        on_disk=file is not None,",
        new="        searchable=record.status == \"processed\",\n        on_disk=file is not None,",
        needle="清单：登记说已入库、库里一块没有",
    ),
    Case(
        title="RT-3  状态优先级反过来（库内没块压过文件不在）",
        target=DOCS_APP,
        old="""    if file is None:
        return "file_missing"
    if indexed_chunks == 0:
        return "not_indexed\"""",
        new="""    if indexed_chunks == 0:
        return "not_indexed"
    if file is None:
        return "file_missing\"""",
        needle="state 优先级：文件不在 压过 库里没块",
    ),
    Case(
        title="RT-4  在途/失败的行也做一致性判定（pending 被报成「库里没块」）",
        target=DOCS_APP,
        old="    if record.status != \"processed\":\n        return record.status",
        new="    if False:  # 反向测试:在途的行也去对账\n        return record.status",
        needle="清单：在途与失败的行原样回状态",
    ),
    Case(
        title="RT-5  磁盘上有、登记表没有的文件不进清单（后端放进去的看不见）",
        target=DOCS_APP,
        old="        if path in used:\n            continue",
        new="        if True:  # 反向测试:未登记的文件不列出来\n            continue",
        needle="清单：磁盘上有、登记表没有",
    ),
    Case(
        title="RT-6  磁盘索引只留首选 doc_id（从语料根灌的登记行被误报「文件不在」）",
        target=LOADER,
        old="            if previous is None:\n                merged[key] = file",
        new="            if previous is None:\n                merged[key] = file\n            elif True:  # 反向测试:丢掉别的入库根给的候选 id\n                pass",
        needle="清单：同一个文件从两个入库根看都认",
    ),
    Case(
        title="RT-7  CLI ingest 不登记（后端灌的语料在前端永远是「未登记」）",
        target=INGEST,
        old="    if deps.document_store_factory is None:",
        new="    if True:  # 反向测试:永远当作「没接登记表」,于是什么都不登记",
        needle="写路径：CLI ingest 之后登记表有行",
    ),
    Case(
        title="RT-8  删除时把语料源材料一起删了（不可逆：删库连源一起删）",
        target=DOCS_APP,
        old="    if not _inside(target, uploads_dir):\n        # 语料目录里的源材料:清库不等于删源(这是不可逆的那一步,宁可留着)\n        kept_source = target.is_file()",
        new="    if False:  # 反向测试:不区分上传目录与语料目录\n        kept_source = target.is_file()",
        needle="删除：只清上传目录里的原件",
    ),
    Case(
        title="RT-9  表格回到按 status 显示（「已入库」再次由登记表说了算）",
        target=TABLE,
        old="  return STATE_LABELS[doc.state]",
        new="  return STATE_LABELS[doc.status as DocState]",
        needle="前端：表格按 state 显示",
    ),
    Case(
        title="RT-10  上传直接往最终文件名里写（中断留下半截文件,清单上多一张「未入库」）",
        target=API_APP,
        old="        os.replace(part, target)\n    except BaseException:",
        new="        pass  # 反向测试:不原子改名(半截文件也叫这个名)\n    except BaseException:",
        needle="上传：落盘是原子的",
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

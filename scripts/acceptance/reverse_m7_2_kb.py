#!/usr/bin/env python
"""的反向测试：把知识库页与预览接口改回坏样子，确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m7_2_kb.py

**为什么专门有一份**：`check_m7_2_kb.py` 一半是**静态契约**（读前端源码做正则断言），
而静态契约的老毛病是它可能永远为真。这里每条都真的把代码改坏一次，再看那一行变没变红
（"没红过的检查 = 没有检查"）。

八条各自的坏法，共同点是**都不会报错**：

* RT-1 预览不折叠父块（每子块一段：块数是子块数，用户以为入库炸了）;
* RT-2 `chars` 用截断后的长度（显示"60 字"而后面还有 540 字）;
* RT-3 没登记的文档也给预览（孤儿块也能被当成"这份文档"看）;
* RT-4 `limit` 上界不校验（前端滑条能拖到一个后端会 422 的值）;
* RT-5 忘了按 limit 截断（预览变成"整份文档"）;
* RT-6 删除一步到位（点一下就删，连带删向量,没有确认）;
* RT-7 离开页面不停轮询（切到聊天页还在每 3 秒打一次清单）;
* RT-8 上传框的 accept 写死（后端加了格式前端不知道）。
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
KB_VIEW = ROOT / "web" / "src" / "views" / "KnowledgeView.vue"
CARD = ROOT / "web" / "src" / "components" / "DocumentCard.vue"
DIALOG = ROOT / "web" / "src" / "components" / "PreviewDialog.vue"
PANEL = ROOT / "web" / "src" / "components" / "UploadPanel.vue"
SCRIPT = "check_m7_2_kb.py"


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
        title="RT-1  预览不折叠父块（每子块一段：3 子块显示成 3 段）",
        target=DOCS_APP,
        old="        grouped.setdefault(chunk.parent_id or chunk.chunk_id, []).append(chunk)",
        new="        grouped.setdefault(chunk.chunk_id, []).append(chunk)",
        needle="预览：子块折叠成父块",
    ),
    Case(
        title="RT-2  chars 用截断后的长度（显示「60 字」而后面还有 540 字）",
        target=DOCS_APP,
        old="                chars=len(body),",
        new="                chars=min(len(body), chars),",
        needle="预览：chars 是**截断前**的完整字数",
    ),
    Case(
        title="RT-3  没登记的文档也给预览（孤儿块被当成「这份文档」）",
        target=API_APP,
        old="    if store.get(document_id) is None:",
        new="    if False:  # 反向测试:不查登记表",
        needle="预览：没登记的文档 → 404",
    ),
    Case(
        title="RT-4  limit 上界不校验（前端能拖到一个后端会 422 的值）",
        target=API_APP,
        old="        limit: int = Query(default=20, ge=1, le=100),",
        new="        limit: int = Query(default=20, ge=1),",
        needle="预览：limit 与 chars 越界 → 422",
    ),
    Case(
        title="RT-5  忘了按 limit 截断（预览变成「整份文档」）",
        target=DOCS_APP,
        old="        for position, block in enumerate(blocks[:limit], start=1)",
        new="        for position, block in enumerate(blocks, start=1)",
        needle="预览：子块折叠成父块",
    ),
    Case(
        title="RT-6  删除一步到位（点一下就删,连带删向量,没有确认）",
        target=CARD,
        old='        <button class="danger" type="button" :disabled="deleting" @click="confirming = true">',
        new='        <button class="danger" type="button" :disabled="deleting" @click="emit(\'remove\')">',
        needle="删除：两步确认",
    ),
    Case(
        title="RT-7  离开页面不停轮询（切到聊天页还在每 3 秒打一次清单）",
        target=KB_VIEW,
        old="""onUnmounted(() => {
  // 离开页面就停轮询:不然切到聊天页还在每 3 秒打一次清单
  stopPolling()""",
        new="""onUnmounted(() => {
  // 反向测试:不停轮询(后面那两行 removeEventListener 也一并去掉,让锚点保持唯一)
  void onVisible""",
        needle="轮询：还有入库中的就开",
    ),
    Case(
        title="RT-8  上传框的 accept 写死（后端加了格式前端不知道）",
        target=PANEL,
        old='        :accept="accept"',
        new='        accept=".pdf,.docx"',
        needle="上传：accept 与大小上限来自 capabilities",
    ),
    Case(
        title="RT-9  预览只显示一个数（用户把「入库 N 块 / 预览 M 段」读成丢数据）",
        target=DIALOG,
        old="            入库 {{ docs.preview.child_chunks }} 块(子块) · 预览 {{ docs.preview.parent_blocks }} 段(父块)",
        new="            入库 {{ docs.preview.child_chunks }} 块",
        needle="预览界面：同时显示子块与父块两个数",
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

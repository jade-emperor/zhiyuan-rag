#!/usr/bin/env python
"""反向测试：把两级目录的几条口径逐个改回坏样子，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m7_5_folders.py

**为什么专门有一份**：`check_m7_5_folders.py` 里一半是"不会报错的错" ——
删目录顺手删文档、回填改成"每次启动都塞回去"、计数按登记表而不是按看得见的算……
这些都不会抛异常、不会留日志，只会在某一天让某个人说"我的文档怎么没了"。
每条都真的把代码改坏一次，再看那一行变没变红（"没红过的检查 = 没有检查"）。

八条各自的坏法：

* RT-1 目录 ID 可能撞上保留 ID `unfiled`（路由会指错格子）;
* RT-2 删目录不拦非空（一次误点清空一整格，不可逆）;
* RT-3 改名顺带改 ID（目录里的文档全部脱钩）;
* RT-4 目录计数不过可见范围（"有 3 份"泄露了"还有 1 份你看不到"）;
* RT-5 回填改成"凡是空就塞回去"（用户移回未分类的文件，重启又被塞回原目录）;
* RT-6 目录名不唯一（命令行 `--folder 名字` 变成掷硬币）;
* RT-7 `ingest --folder` 不认那个参数（命令行说灌进去了，其实落在别处）;
* RT-8 一级页面自己拉文档清单（它只该列目录，不然"两级"就只剩一级）。

RT-5 / RT-6 要真 MySQL：连不上时那两条检查是 SKIP，这里也标"环境跳过"而不是判失败
（与 `reverse_m4_eval.py` 对"数据不在本地"的处理同一口径）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src" / "project_rag" / "domain" / "documents.py"
FOLDERS_APP = ROOT / "src" / "project_rag" / "application" / "folders.py"
DOCS_MYSQL = ROOT / "src" / "project_rag" / "adapters" / "documents_mysql.py"
API_APP = ROOT / "src" / "project_rag" / "api" / "app.py"
INGEST_CLI = ROOT / "src" / "project_rag" / "cli" / "commands" / "ingest.py"
LOADER = ROOT / "src" / "project_rag" / "adapters" / "document_loader.py"
KB_VIEW = ROOT / "web" / "src" / "views" / "KnowledgeView.vue"
ROUTER = ROOT / "web" / "src" / "router.ts"
SCRIPT = "check_m7_5_folders.py"


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
        title="RT-1  目录 ID 可能撞上保留 ID `unfiled`",
        target=DOMAIN,
        old='    return f"f_{uuid4().hex[:12]}"',
        new='    return "unfiled"',
        needle="目录 ID：形状恒为",
    ),
    Case(
        title="RT-2  删目录不拦非空（一次误点清空一整格）",
        target=FOLDERS_APP,
        old="""    if documents > 0:
        raise FolderNotEmpty(folder, documents)""",
        new="""    if False:  # 反向测试:非空也删
        raise FolderNotEmpty(folder, documents)""",
        needle="删目录：非空 409",
    ),
    Case(
        title="RT-3  改名顺带改 ID（目录里的文档全部脱钩）",
        target=FOLDERS_APP,
        old="""    updated = Folder(
        folder_id=folder.folder_id,""",
        new="""    updated = Folder(
        folder_id=new_folder_id(),""",
        needle="改名：ID 不变",
    ),
    Case(
        title="RT-4  目录计数不过可见范围（份数泄露了「还有你看不到的」）",
        target=API_APP,
        old="    return tuple(view for view in views if access.allows(view.document_id))",
        new="    return tuple(views)  # 反向测试:不过滤可见范围",
        needle="可见范围：看不见的文档不计入目录份数",
    ),
    Case(
        title="RT-5  回填改成「凡是空就塞回去」（移回未分类的文件重启又被塞回原目录）",
        target=DOCS_MYSQL,
        old="""                if added:
                    self._backfill_folders(cur)""",
        new="""                if True:  # 反向测试:每次启动都回填
                    self._backfill_folders(cur)""",
        needle="迁移：老表补 `folder` 列并按来源回填",
    ),
    Case(
        title="RT-6  目录名不唯一（命令行 `--folder 名字` 变成掷硬币）",
        target=DOCS_MYSQL,
        old="  UNIQUE KEY uk_folders_name (name)",
        new="  KEY idx_folders_name (name)",
        needle="迁移：目录名在库里唯一",
    ),
    Case(
        title="RT-7  默认落点又按来源建格（界面上冒出 uploads / corpus 这类不成分类的目录）",
        target=INGEST_CLI,
        old="            name = corpus_folder_of_source(corpus_dir, document.source)",
        new="            name = document.source  # 反向测试:又按来源标签建目录",
        needle="CLI：`--folder` 显式建目录",
    ),
    Case(
        title="RT-10 不按语料根归一化 doc_id（同一份文件从两个入口灌 → 库里两份）",
        target=LOADER,
        old="""        if relative is not None and len(relative.parts) >= 2:
            return corpus_root / relative.parts[0]""",
        new="""        if False:  # 反向测试:不按语料根归一化
            return corpus_root / relative.parts[0]""",
        needle="登记根：同一个文件从语料根或子目录灌",
    ),
    Case(
        title="RT-8  一级页面自己拉文档清单（那「两级」就只剩一级）",
        target=KB_VIEW,
        old="  void refreshFolders()",
        new="  void refreshDocuments()",
        needle="前端：路由有 /knowledge/:folder",
    ),
    Case(
        title="RT-9  二级页面的路由被删掉（点目录进不去）",
        target=ROUTER,
        old="      path: '/knowledge/:folder',",
        new="      path: '/knowledge-folder',",
        needle="前端：路由有 /knowledge/:folder",
    ),
)


def run(needle: str) -> tuple[bool, str, bool]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行, 是否 SKIP)。"""
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
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}", False
    return "[FAIL]" in hits[0], hits[0], "[SKIP]" in hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    skipped = 0
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
            red, line, was_skip = run(case.needle)
        if was_skip:
            skipped += 1
            print(f"… 环境跳过  {case.title}\n      {line}")
            continue
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    if bad:
        print(f"{bad} 条没红 —— 那些检查抓不到坏代码")
    else:
        print(f"全部反向测试都红了{'（另有 %d 条环境跳过）' % skipped if skipped else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

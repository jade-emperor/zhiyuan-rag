#!/usr/bin/env python
"""接口层的反向测试:把接口契约改回坏样子,确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m5_api.py

**为什么专门有一份**:``check_m5_api.py`` 有 26 条契约(路由表 / 命名空间 / 全是协程 /
出参形状 / 各端点语义 / WS 帧协议 / 静态挂载),却是**唯一一份没有反向测试的检查脚本** ——
而"没红过的检查 = 没有检查"是这个项目的老账。
这三条挑的是**最容易被顺手破坏**的地方,它们的共同点是**不会报错**:

* RT-1 新加一个端点忘了带 ``/api/v1`` 前缀(前端会生成两个前缀的 TS 客户端);
* RT-2 静态挂载漏掉 ``html=True``(目录请求 404,而 ``/static/app/index.html`` 照样 200 ——
  用户输的是前者,页面就是打不开);
* RT-3 删掉一条路由的装饰器(端点静默消失,调用方拿到 405)。

**原来的 RT-2 / RT-4 去哪了**:它们改的是内置调试页
``static/index.html``(把 WS 地址改回老路径 / 不再转 ``?token=``),而那份页面已经删掉了
(界面只留前端产物一份,见 ``check_m7_frontend`` 根路径那条)。它们守的两条性质没丢,
只是换了归属:「令牌必须进 WS 的 query」由 ``reverse_m7_frontend`` 的 RT-7 反向,
「页面连的是服务端真有的那条 WS」由 ``check_m7_frontend`` 的**前端清单↔后端路由表**那条
(反向见 ``reverse_m7_frontend`` 的 RT-12)。**没有一条性质是"随文件一起删掉"的。**

每条都通过 ``_harness.patched_source`` 改源码:它先落备份、退出时还原,
**被打断也能在下次启动时自愈**。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "project_rag" / "api" / "app.py"
SCRIPT = "check_m5_api.py"


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
        title="RT-1  新端点忘了带 /api/v1 前缀（前端会认两个前缀）",
        target=APP,
        old='    @app.get("/health", response_model=HealthOut)\n',
        new=(
            '    @app.get("/api/legacy_probe")\n'
            "    async def legacy_probe() -> dict[str, str]:\n"
            '        return {"ok": "1"}\n'
            "\n"
            '    @app.get("/health", response_model=HealthOut)\n'
        ),
        needle="命名空间：业务接口一律在",
    ),
    Case(
        title="RT-2  静态挂载漏掉 html=True（用户会输的 /static/app/ 变 404，而 /static/app/index.html 照样 200）",
        target=APP,
        old="StaticFiles(directory=str(_STATIC_DIR), html=True)",
        new="StaticFiles(directory=str(_STATIC_DIR))",
        needle="静态：GET / 307",
    ),
    Case(
        title="RT-3  删掉一条路由的装饰器（端点静默消失，调用方拿到 405）",
        target=APP,
        old='    @app.delete("/api/v1/history/{session_id}", response_model=DeletedOut)\n',
        new="",
        needle="路由清单：",
    ),
)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120, env=dict(os.environ),
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
            print(f"⚠️  {case.title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑")
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

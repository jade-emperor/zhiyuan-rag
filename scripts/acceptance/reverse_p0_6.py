#!/usr/bin/env python
"""反向测试:把"谁能看什么"改回坏样子,确认契约真的会红。

用法：

    uv run python scripts/acceptance/reverse_p0_6.py

这一批坏版本有个共同点:**都不会报错**,只会让权限悄悄放宽(或者悄悄锁死)——
正是最该被抓住的那种错。改的是:

* ``application/access.py`` / ``domain/access.py`` —— 口径本身(空规则 = 全禁、组取交集、管理员也被挡);
* ``application/accounts.py`` —— 删号的两条守卫;
* ``api/auth.py`` —— 运维/报表那两道门;
* ``api/app.py`` —— 检索/清单/预览/删文档四处落地,以及"读不到规则就放行"。

用例可以点名**别的检查脚本**(``script`` 字段):有几条坏版本的第一现场在
``check_p0_3_auth.py``(``/me`` 的能力字段)与 ``check_m7_frontend.py``(路由守卫),
让最贴近的那一份去抓它,比硬塞给本文件对应的检查更结实。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_p0_6_access.py"

SRC = ROOT / "src" / "project_rag"
DOMAIN = SRC / "domain"
APP = SRC / "application"
API = SRC / "api"


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
        title="RT-1  没配规则的文档变成『谁都看不到』(空 ≠ 全禁被写反)",
        target=DOMAIN / "access.py",
        old="        if sees_all or not self.restricted:\n            return True",
        new="        if sees_all:\n            return True",
        needle="口径 没规则=人人可见",
    ),
    Case(
        title="RT-2  组命中改成交集(必须属于名单里的每一个组 —— 加个组反而少了权限)",
        target=DOMAIN / "access.py",
        old="        return bool(set(self.group_ids) & set(group_ids))",
        new="        return bool(self.group_ids) and set(self.group_ids) <= set(group_ids)",
        needle="口径 组命中是并集",
    ),
    Case(
        title="RT-3  管理员也被可见范围挡住(改错一次授权就再也改不回来)",
        target=APP / "access.py",
        old="    if sees_all:\n        return frozenset()",
        new="    if False:\n        return frozenset()",
        needle="口径 有规则就收窄",
    ),
    Case(
        title="RT-4  检索不过滤被禁文档(受限内容照常出现在引用里)",
        target=APP / "answer_rag.py",
        old="    if denied:\n        rankings = {\n            route: _visible(hits, denied, options.k) for route, hits in rankings.items()\n        }",
        new="    if False:\n        rankings = {\n            route: _visible(hits, denied, options.k) for route, hits in rankings.items()\n        }",
        needle="落地 检索：被禁文档的块不许出现在引用里",
    ),
    Case(
        title="RT-5  过滤放在截断之后(被禁几条就少给几条 —— 多要的候选全白要)",
        target=APP / "answer_rag.py",
        old="    return tuple(hit for hit in hits if hit.chunk.doc_id not in denied)[:k]",
        new="    return tuple(hit for hit in hits[:k] if hit.chunk.doc_id not in denied)",
        needle="落地 检索：过滤在截断之前",
    ),
    Case(
        title="RT-6  清单不按可见范围过滤(点开预览却 404 —— 用户以为系统坏了)",
        target=API / "app.py",
        old="    visible = [view for view in views if access.allows(view.document_id)]",
        new="    visible = list(views)",
        needle="落地 清单：员工看不到受限文档",
    ),
    Case(
        title="RT-7  预览不看可见范围(受限文档的内容直接给出去)",
        target=API / "app.py",
        old="    if not access.allows(document_id):\n        raise _no_such_document(document_id)",
        new="    if False:\n        raise _no_such_document(document_id)",
        needle="落地 预览：看不见的文档回 404",
    ),
    Case(
        title="RT-8  读不到可见范围时放行(一次数据库抖动 = 一次全网可见)",
        target=API / "app.py",
        old="""    except Exception as exc:  # noqa: BLE001 - 翻成 503:读不到规则时**不静默放行**
        raise HTTPException(""",
        new="""    except Exception:  # noqa: BLE001 - 坏版本:读不到规则就放行
        return UNRESTRICTED
    if False:
        raise HTTPException(""",
        needle="落地 读不到规则时 503",
    ),
    Case(
        title="RT-9  删文档时不清可见范围规则(同名重传会继承旧授权)",
        target=API / "app.py",
        old="        removed = store.forget_document(document_id)",
        new="        removed = 0",
        needle="落地 删文档：连带清掉它的可见范围规则",
    ),
    Case(
        title="RT-10 运维从知识库能力表里被拿掉(他能问答)",
        target=ROOT / "src" / "project_rag" / "domain" / "auth.py",
        old='KNOWLEDGE_ROLES: tuple[UserRole, ...] = ("employee", "ops", "admin")',
        new='KNOWLEDGE_ROLES: tuple[UserRole, ...] = ("employee", "admin")',
        needle="落地 运维：**能问答能看报表**",
    ),
    Case(
        title="RT-11 报表退回『只有管理员』(运维这个角色就没有意义了)",
        target=API / "auth.py",
        old="        if user is not None and not user.can_view_reports:",
        new="        if user is not None and not user.is_admin:",
        needle="落地 运维：**能问答能看报表**",
    ),
    Case(
        title="RT-12 删号守卫去掉『不能删自己』",
        target=APP / "accounts.py",
        old='    if target.user_id == actor_id:\n        raise ValueError("不能删自己的账号:删掉之后这枚令牌立刻失效,你就被踢出去了")',
        new="    if False:\n        raise ValueError(\"不能删自己的账号\")",
        needle="守卫 删号",
    ),
    Case(
        title="RT-13 删号守卫去掉『不能删最后一个管理员』(系统会被锁死)",
        target=APP / "accounts.py",
        old="    if target.is_admin and sum(1 for user in existing if user.is_admin) <= 1:",
        new="    if False:",
        needle="守卫 删号",
    ),
    Case(
        title="RT-14 建号不校验重名与长度(把守卫整段拆掉)",
        target=APP / "accounts.py",
        old="    name = username.strip()",
        new="    return username",
        needle="守卫 建号",
    ),
    Case(
        title="RT-15 /me 不再报能力(前端只能回去比较角色字符串)",
        target=API / "app.py",
        old="            can_view_reports=user.can_view_reports,",
        new="            can_view_reports=True,",
        needle="角色：报表是运维与管理员的",
        script="check_p0_3_auth.py",
    ),
    Case(
        title="RT-17 组侧授权把「人人可见」的文档也当成一次授权（顺手改了别人的可见性）",
        target=APP / "access.py",
        old="""        if row.visibility == "open":
            if row.document_id in wanted:
                already_open.append(row.document_id)
            continue""",
        new="""        if row.visibility == "open":
            if row.document_id in wanted:
                grant.append(row.document_id)
            continue""",
        needle="组侧授权：三态列得对",
    ),
    Case(
        title="RT-18 名单空了不点名（取消一个组之后整份文档悄悄对所有人可见）",
        target=APP / "access.py",
        old="""            if row.other_subjects == 0:
                will_open.append(row.document_id)""",
        new="""            if row.other_subjects < 0:
                will_open.append(row.document_id)""",
        needle="组侧授权：三态列得对",
    ),
    Case(
        title="RT-16 路由守卫不按能力拦(页签藏了,输入 URL 仍然进得去)",
        target=ROOT / "web" / "src" / "router.ts",
        old="  if (need !== undefined && !NEEDS[need]()) return landingRoute()",
        new="  if (need !== undefined && false) return landingRoute()",
        needle="角色分流：页签按能力渲染",
        script="check_m7_frontend.py",
    ),
    Case(
        title="RT-19  403 文案说反了(运维能问答之后又写回『运维只能看报表』)",
        target=API / "auth.py",
        old='                detail="这个操作需要问答权限",',
        new='                detail="这个操作需要员工或管理员角色(运维只能看报表)",',
        needle="403 文案与能力表一致",
    ),
)


def run(script: str, needle: str) -> tuple[bool, str]:
    """输入:检查脚本名、期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{script}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    """输入:无。行为:逐个改坏→跑契约→还原,统计哪几条没红。输出:退出码。"""
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")

    originals = {case.target: case.target.read_text(encoding="utf-8") for case in CASES}
    bad = 0
    try:
        for case in CASES:
            if only and not any(case.title.startswith(tag) for tag in only):
                continue
            original = originals[case.target]
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
                    line += "\n      ⚠️ 这一条是 SKIP(环境不满足)—— 反向测试证明不了它"
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    finally:
        for target, original in originals.items():
            if target.read_text(encoding="utf-8") != original:
                target.write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

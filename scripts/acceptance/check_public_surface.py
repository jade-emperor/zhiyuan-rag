#!/usr/bin/env python
"""公开面契约：**只有内部看得懂的东西，不许留在仓库里**。

用法::

    uv run python scripts/acceptance/check_public_surface.py

这条契约守的是一条**边界**,不是某个功能：

* 仓库（``git ls-files`` 认识的那些文件）是**对外**的 —— 陌生人、第三方、未来的贡献者都会读；
* 维护者的决策台账（``docs/``）是**对内**的 —— 不发布，只给维护者与协作者复盘用。

于是仓库里任何一行都不该出现"只有我们俩看得懂"的东西：台账路径、台账里的章节编号
（``§0.3 第 N 条``）、决策者口吻（"作者要求 / 作者报障"）。那些内容的价值只在对内复盘,
它们住在台账里;仓库里该留的是**自包含的约束** —— "改这里会坏什么",不需要另开一份文档才读得懂。

为什么这条要单独一份契约、还要进 ``--set ci``：
**它防的坏法没有报错**。把一句 ``(见 docs/STATUS.md §0.3 第 39 条)`` 留在代码注释里,
测试照样全绿、程序照样跑,只有"外人读到这里一头雾水"这一个症状 —— 而没人会为此提 issue。
同一条纪律此前靠人记（"注释里别放台账"）,已经失效过一次（全仓 100+ 处指针）。

判据是机械的（``git ls-files`` + 正则）,所以它也能被反向用例打红。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from _harness import SkipCheck, check, report

ROOT = Path(__file__).resolve().parents[2]

# 「只有内部看得懂」的三种形状。**列的是判据,不是清单**:
# 台账路径（含裸文件名）/ 台账章节编号 / 决策者口吻。
INTERNAL_PATTERNS = (
    "docs/STATUS.md",
    "docs/deferred.md",
    "docs/frontend.md",
    "STATUS.md",
    "deferred.md",
    "§0.3 第",
    "作者要求",
    "作者报障",
    "作者原话",
    "作者定稿",
    "作者本机",
    # 「他们俩之间说过的话」也算内部信息：报障经过、口头定稿、当初踩的坑 —— 对读者全是噪音。
    "作者报",
    "作者定",
    "作者踩",
    "作者追",
    "法则 #",
)

# ``docs/`` 这几个字节**在下列文件里是合法的**,因为它们是**边界声明本身**
# （"维护者的台账不随仓库发布"）—— 读者需要知道有这么一份东西，才不会被"为什么没有设计文档"困住。
# 判据:同一行必须同时出现"不发布"的说法;只写路径不给说法,等于给外人留一个死链。
BOUNDARY_MARKERS = (
    "不随仓库发布",
    "不随本仓库发布",
    "不发布",
    "不公开",
    "not part of this repository",
    "not published",
)
# 「我们的台账」= 被**引号/斜杠之外**的 `docs/`（`/docs/oauth2-redirect` 是 FastAPI 自己的
# Swagger 路径，不是我们的目录 —— 这条曾经误报过，所以判据里把前面带 `/` 或 `-` 的排掉）。
# **里程碑编号**（`M4.2` / `P0-6` / `M7.4` 这类内部票号）也是台账味道：
# 读者不知道 M7.4 是什么，它只说明「这句话是写给实现者的」。
# 判据必须是**整词** —— ``BM25`` / ``M2_SDK_ROOTS`` / ``check_m2_faq.py``（小写）都不是票号。
# 前后各加一个「不是标识符字符」的断言，就能把它们排除掉。
MILESTONE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:M[0-9]+(?:\.[0-9]+)?[a-z]?|P0-[0-9]+[a-z]?(?:-[0-9]+)?)(?![A-Za-z0-9_])"
)

_DOCS = re.compile(r"(?<![-/\w])docs/")

# SVG 路径数据里那个 ``M`` 是**绘图指令**(moveto),不是票号:``d="M6.5 9.2c…"`` 与
# 图标表里的 ``'M12 5v14'`` 都算。判据 = 引号里以 M/m 开头、随后全是数字/空格/命令字母 ——
# 里程碑引用后面跟的是中文或空格+词("M7.4 之后"),不会长成这样。
_SVG_PATH = re.compile(r"""['"][Mm][\d.,\s]*[A-Za-z]""")
# 忽略清单里出现目录名是**技术需要**（不是文档引用）—— 不进仓的规则必须写得出来。
PATH_ONLY_FILES = (".gitignore", ".dockerignore")
# 判据本身要能写出"被禁的形状"（否则这份文件没法描述自己），所以**只豁免这两份**：
# 检查与它的反向用例。别的文件一律不许出现那些字样 —— 包括这个仓库的 README。
SELF_FILES = (
    "scripts/acceptance/check_public_surface.py",
    "scripts/acceptance/reverse_public_surface.py",
)


def _tracked() -> list[str]:
    """输入:无。行为:问 git 这份仓库**会公开出去**哪些文件。输出:相对路径列表。

    没有 git（源码是从 tar 包解出来的）时**跳过并点名 owner**,而不是假装通过 ——
    "查不了"与"查了没问题"是两件事。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境态
        raise SkipCheck(f"这里没有可用的 git（{exc}）—— owner: 版本库") from exc
    if out.returncode != 0:
        raise SkipCheck(f"git ls-files 失败（{out.stderr.strip()[:120]}）—— owner: 版本库")
    return [line for line in out.stdout.splitlines() if line.strip()]


def _scan(paths: list[str]) -> list[tuple[str, int, str, str]]:
    """输入:被跟踪的相对路径。行为:逐行找内部字样。输出:[(路径, 行号, 命中的字样, 那一行)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for rel in paths:
        if rel in SELF_FILES:
            continue
        target = ROOT / rel
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制 / 不可读 —— 不是文档面
        for lineno, line in enumerate(text.splitlines(), 1):
            # 边界声明本身是合法的:同一行说清了"不发布",读者就不会顺着路径找过去。
            boundary = any(marker in line for marker in BOUNDARY_MARKERS)
            for pattern in INTERNAL_PATTERNS:
                if pattern in line and not boundary:
                    hits.append((rel, lineno, pattern, line.strip()))
                    break
            else:
                found = None if _SVG_PATH.search(line) else MILESTONE.search(line)
                if found:
                    hits.append((rel, lineno, found.group(0), line.strip()))
    return hits


@check("公开面：仓库文件里不出现内部台账的路径、章节编号、决策者口吻与里程碑编号")
def _() -> str:
    """三种内部形状各判一次（见模块 docstring）。命中就点名 `文件:行` 与前 60 字,便于直接改。"""
    paths = _tracked()
    hits = _scan(paths)
    if hits:
        shown = "\n".join(
            f"  {rel}:{lineno}  [{pattern}]  {line[:60]}" for rel, lineno, pattern, line in hits[:8]
        )
        more = "" if len(hits) <= 8 else f"\n  …… 另有 {len(hits) - 8} 处"
        raise AssertionError(
            f"{len(hits)} 处内部字样留在了会公开的文件里 —— 台账不发布，读者读不到它：\n{shown}{more}\n"
            "改法：把约束**就地写成一句自包含的话**（\"改这里会坏什么\"），"
            "过程与决策留在维护者本地台账里。"
        )
    return f"{len(paths)} 个被跟踪文件里：无台账路径 / 无章节编号 / 无决策者口吻 / 无里程碑编号"


@check("公开面：提到 `docs/` 只许是「不发布」的边界声明（忽略清单除外）")
def _() -> str:
    """输入:无。行为:找 `docs/` 的出现。输出:合规或点名。

    允许两种：① 忽略清单里写目录名（技术需要）；② 同一行说清了"不发布"（边界声明本身）。
    只丢一个路径不给说法 = 给外人留一个死链，那正是这次清理掉的那种东西。
    """
    paths = _tracked()
    bad: list[tuple[str, int, str]] = []
    good = 0
    for rel in paths:
        if rel in PATH_ONLY_FILES or rel in SELF_FILES:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _DOCS.search(line):
                continue
            if any(marker in line for marker in BOUNDARY_MARKERS):
                good += 1
                continue
            bad.append((rel, lineno, line.strip()))
    if bad:
        shown = "\n".join(f"  {rel}:{lineno}  {line[:70]}" for rel, lineno, line in bad[:8])
        more = "" if len(bad) <= 8 else f"\n  …… 另有 {len(bad) - 8} 处"
        raise AssertionError(
            f"{len(bad)} 处提到 `docs/` 却没说清它不发布（外人只会看到一个死链）：\n{shown}{more}\n"
            "要么删掉这个引用（改成自包含的说明），要么在同一行写明「不随仓库发布」。"
        )
    return f"`docs/` 的出现全部是边界声明（{good} 行）或忽略清单；无裸引用"


def main() -> int:
    return report(
        "公开面契约：仓库里不留内部台账的路径 / 编号 / 口吻",
        hint="这条进 --set ci（只要 git，零容器零网络）；加注释时别把台账搬进来。",
    )


if __name__ == "__main__":
    sys.exit(main())

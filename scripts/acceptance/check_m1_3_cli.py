#!/usr/bin/env python
"""M1.3 验收：CLI + LLM 端口/适配器（行走骨架的最后一块）。

用法::

    uv run python scripts/acceptance/check_m1_3_cli.py

固化的公开契约：

* ``from project_rag.interfaces import cli``
  ``cli(argv: list[str] | None = None, *, llm_factory: Callable[[], LLMClient] | None = None) -> int``
  返回进程退出码，自己不要 ``sys.exit``；用法错误交给 argparse 抛 ``SystemExit(2)``。
* 子命令 ``chat``：``-p/--prompt`` 必填；用户提问必须以 **user** 角色送出。
* 分层（静态扫描，不靠自觉）：
  - ``domain/`` 只准出现标准库；
  - ``ports/`` 只准出现标准库 + ``project_rag.domain``；
  - ``langchain*`` 只准出现在 ``adapters/``。
* 安装了控制台脚本 ``project-rag``（``[project.scripts]``）。
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.metadata
import io
import shutil
import subprocess
import sys
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARK = {PASS: "✔", FAIL: "✘", SKIP: "○"}
_results: list[tuple[str, str, str]] = []
SRC = Path(__file__).resolve().parents[2] / "src" / "project_rag"


class SkipCheck(Exception):
    """该检查在当前环境下不适用。"""


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def check(name: str):
    def decorate(fn):
        try:
            detail = fn() or ""
            record(name, PASS, str(detail))
        except SkipCheck as exc:
            record(name, SKIP, str(exc))
        except Exception as exc:  # noqa: BLE001 - 验收脚本要显示任何失败
            record(name, FAIL, f"{type(exc).__name__}: {exc}")
        return fn

    return decorate


def cli_api():
    module = importlib.import_module("project_rag.interfaces")
    if not hasattr(module, "cli"):
        raise AssertionError(
            "project_rag.interfaces 没有导出 cli；请在 interfaces/__init__.py 里 re-export"
        )
    return module.cli


class FakeLLM:
    """记录收到的领域消息，然后按可预测的顺序吐字。"""

    def __init__(self, chunks=("你好", "，", "世界")) -> None:
        self.chunks = chunks
        self.seen: list[tuple[str, str]] = []

    def stream(self, messages):
        self.seen = [(m.role, m.content) for m in messages]
        yield from self.chunks


class BoomLLM:
    """一调用就炸，用来验证接口层把异常翻译成退出码而不是往上抛。"""

    def stream(self, messages):
        raise RuntimeError("模拟网络故障")


def run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "project_rag", *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


# ── 1. 接口与用法 ─────────────────────────────────────────────────────────────
@check("公开入口可导入（from project_rag.interfaces import cli）")
def _() -> str:
    cli_api()
    return "project_rag.interfaces.cli"


@check("python -m project_rag --help 退出码 0")
def _() -> str:
    proc = run_cli(["--help"])
    if proc.returncode != 0:
        raise AssertionError(f"退出码 {proc.returncode}：{proc.stderr[:200]}")
    if "chat" not in proc.stdout:
        raise AssertionError("--help 里没列出 chat 子命令")
    return "0，且列出了 chat"


@check("用法错误退出码 2（未知子命令 / chat 缺 -p）")
def _() -> str:
    unknown = run_cli(["不存在的子命令"])
    missing = run_cli(["chat"])
    if unknown.returncode != 2:
        raise AssertionError(f"未知子命令退出码应为 2，实际 {unknown.returncode}")
    if missing.returncode != 2:
        raise AssertionError(f"chat 缺 -p 退出码应为 2，实际 {missing.returncode}")
    return "两者均为 2"


# ── 2. 依赖注入：整条链路可测 ──────────────────────────────────────────────────
@check("注入 FakeLLM 后能跑通并返回 0（不联网、不读 .env）")
def _() -> str:
    cli = cli_api()
    fake = FakeLLM()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli(["chat", "-p", "介绍一下 Milvus"], llm_factory=lambda: fake)
    if code != 0:
        raise AssertionError(f"退出码应为 0，实际 {code}")
    if buf.getvalue().strip() != "".join(fake.chunks):
        raise AssertionError(f"流式输出没按顺序透传，实际 {buf.getvalue()!r}")
    return f"输出 {buf.getvalue().strip()!r}"


@check("用户提问必须以 user 角色送出（不是 system）")
def _() -> str:
    cli = cli_api()
    fake = FakeLLM()
    prompt = "介绍一下 Milvus"
    with contextlib.redirect_stdout(io.StringIO()):
        cli(["chat", "-p", prompt], llm_factory=lambda: fake)
    hit = [role for role, content in fake.seen if content == prompt]
    if not hit:
        raise AssertionError(f"FakeLLM 没收到提问内容，实际收到 {fake.seen}")
    if hit[0] != "user":
        raise AssertionError(
            f"用户提问的角色是 {hit[0]!r}，应为 'user'。"
            "system 是指令/人设的位置，用户问题放那儿会明显劣化回答"
        )
    return f"{len(fake.seen)} 条消息，提问角色 = user"


@check("适配器抛异常时返回 1，且不把异常抛给调用方")
def _() -> str:
    cli = cli_api()
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = cli(["chat", "-p", "hi"], llm_factory=lambda: BoomLLM())
    if code != 1:
        raise AssertionError(f"退出码应为 1，实际 {code}（异常被吞了或没翻译）")
    return "1 + 人话错误信息"


# ── 3. 分层纪律（静态扫描，不靠自觉） ─────────────────────────────────────────
def _imports_of(path: Path) -> list[tuple[str, int]]:
    """返回 [(模块名, 相对层级)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def _third_party(module: str) -> bool:
    root = module.split(".")[0]
    return bool(root) and root not in sys.stdlib_module_names and root != "project_rag"


@check("domain/ 只用标准库（连 project_rag 内部都不 import）")
def _() -> str:
    bad: list[str] = []
    for path in sorted(SRC.joinpath("domain").rglob("*.py")):
        for module, level in _imports_of(path):
            if level > 0:  # 同层相对导入，允许
                continue
            if module.startswith("project_rag") or _third_party(module):
                bad.append(f"{path.name} -> {module}")
    if bad:
        raise AssertionError(f"domain 不该依赖任何东西：{bad}")
    return f"{len(list(SRC.joinpath('domain').rglob('*.py')))} 个文件干净"


@check("ports/ 只用标准库 + project_rag.domain")
def _() -> str:
    bad: list[str] = []
    for path in sorted(SRC.joinpath("ports").rglob("*.py")):
        for module, level in _imports_of(path):
            if level > 0:
                continue
            if module.startswith("project_rag"):
                if not module.startswith("project_rag.domain"):
                    bad.append(f"{path.name} -> {module}（端口只能依赖 domain）")
            elif _third_party(module):
                bad.append(f"{path.name} -> {module}（端口不许依赖第三方）")
    if bad:
        raise AssertionError(f"{bad}")
    return "端口只依赖 domain + 标准库"


@check("langchain 只出现在 adapters/ 里")
def _() -> str:
    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for module, level in _imports_of(path):
            if module.split(".")[0].startswith("langchain") and "adapters" not in path.parts:
                bad.append(f"{path.relative_to(SRC)} -> {module}")
    if bad:
        raise AssertionError(f"第三方 SDK 泄漏到了适配器之外：{bad}")
    return "唯一入口是 adapters/"


def _protocol_names() -> set[str]:
    """找出 ports/ 里定义的所有 Protocol 名字。"""
    names: set[str] = set()
    for path in SRC.joinpath("ports").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == "Protocol":
                    names.add(node.name)
                elif isinstance(base, ast.Attribute) and base.attr == "Protocol":
                    names.add(node.name)
    return names


@check("每个端口都有适配器显式实现（端口不能是没人执行的合同）")
def _() -> str:
    ports = _protocol_names()
    if not ports:
        raise AssertionError("ports/ 里没找到任何 Protocol")
    implemented: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "ports" in path.parts:  # 端口自己不算实现
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                if name in ports:
                    implemented[name] = str(path.relative_to(SRC))
    missing = ports - set(implemented)
    if missing:
        raise AssertionError(
            f"这些端口没有任何显式实现：{sorted(missing)}。"
            "结构化子类型只在类型检查器眼里成立——要么让适配器显式继承端口"
            "（check 点更靠近错误），要么引入 pyright 之类的检查器；"
            "两者都没有，端口就只是一份没人执行的合同"
        )
    return ", ".join(f"{k} ← {v}" for k, v in sorted(implemented.items()))


@check("控制台脚本 project-rag 已注册（[project.scripts]）")
def _() -> str:
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    found = {ep.name: ep.value for ep in entry_points}
    if "project-rag" not in found:
        raise AssertionError(f"没有注册 project-rag 入口，已注册：{sorted(found)[:8]}")
    where = shutil.which("project-rag")
    return f"{found['project-rag']}" + (f"（可执行：{where}）" if where else "（当前 PATH 里找不到，非致命）")


def main() -> int:
    print("M1.3 CLI 验收\n" + "─" * 60)
    for name, status, detail in _results:
        suffix = f"  — {detail}" if detail else ""
        print(f"  {_MARK[status]} [{status}] {name}{suffix}")
    failed = [name for name, status, _ in _results if status == FAIL]
    skipped = [name for name, status, _ in _results if status == SKIP]
    print("─" * 60)
    print(
        f"结果：{len(_results) - len(failed) - len(skipped)} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过"
    )
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✘ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

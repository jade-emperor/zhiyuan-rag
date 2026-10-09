#!/usr/bin/env python
"""M1.3 验收：CLI + LLM 端口/适配器（行走骨架的最后一块）。

用法::

    uv run python scripts/acceptance/check_m1_3_cli.py

固化的公开契约：

* ``from project_rag.cli import cli, Deps``
  ``cli(argv: list[str] | None = None, *, deps: Deps | None = None) -> int``
  返回进程退出码，自己不要 ``sys.exit``；用法错误交给 argparse 抛 ``SystemExit(2)``。

  ``deps=None`` = 真实装配（读 Settings、装日志）；传入 ``Deps`` = 不读 .env、不装日志、不联网。
  ``Deps`` 的字段与默认值由 ``check_m2_faq.py``（M2 契约）固化；M1.3 只用它的 ``llm_factory``::

      Deps(llm_factory=lambda: FakeLLM())
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

from _harness import check, report
SRC = Path(__file__).resolve().parents[2] / "src" / "project_rag"


def cli_api():
    module = importlib.import_module("project_rag.cli")
    if not hasattr(module, "cli"):
        raise AssertionError(
            "project_rag.cli 没有导出 cli；请在 cli/__init__.py 里 re-export"
        )
    return module.cli


def deps_api():
    """M2 起组合根换成 Deps dataclass（替换 llm_factory 关键字）。"""
    module = importlib.import_module("project_rag.cli")
    if not hasattr(module, "Deps"):
        raise AssertionError(
            "project_rag.cli 没有导出 Deps；"
            "M2 契约（check_m2_faq.py）要求 cli(argv, *, deps: Deps | None = None)，"
            "并在 cli/__init__.py 里 re-export"
        )
    return module.Deps


def inject(deps_cls, fake) -> object:
    """把假 LLM 包成注入用的 Deps；只给 llm_factory，其余依赖不该被 chat 碰到。"""
    return deps_cls(llm_factory=lambda: fake)


class FakeLLM:
    """记录收到的领域消息，然后按可预测的顺序吐字。"""

    def __init__(self, chunks=("你好", "，", "世界")) -> None:
        self.chunks = chunks
        self.seen: list[tuple[str, str]] = []

    def stream(self, messages):
        from project_rag.domain import LlmDelta

        self.seen = [(m.role, m.content) for m in messages]
        for piece in self.chunks:
            yield LlmDelta(text=piece)
        yield LlmDelta(finish_reason="stop", last=True)


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
@check("公开入口可导入（from project_rag.cli import cli）")
def _() -> str:
    cli_api()
    return "project_rag.cli.cli"


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
@check("注入 Deps(FakeLLM) 后能跑通并返回 0（不联网、不读 .env）")
def _() -> str:
    cli = cli_api()
    fake = FakeLLM()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli(["chat", "-p", "介绍一下 Milvus"], deps=inject(deps_api(), fake))
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
        cli(["chat", "-p", prompt], deps=inject(deps_api(), fake))
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
        code = cli(["chat", "-p", "hi"], deps=inject(deps_api(), BoomLLM()))
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


@check("控制台脚本 project-rag 已注册，且登记的目标能真的解析到（改名/移动包后必查）")
def _() -> str:
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    found = {ep.name: ep.value for ep in entry_points}
    if "project-rag" not in found:
        raise AssertionError(f"没有注册 project-rag 入口，已注册：{sorted(found)[:8]}")

    # 注册表里写着目标 ≠ 目标还存在。.venv 里的 entry_points.txt 是安装时生成的快照，
    # 改了 [project.scripts] 或移动了模块之后必须重装（uv sync）才会刷新，
    # 否则出现「python -m 正常、控制台命令 ModuleNotFoundError」这种最难查的组合。
    target = found["project-rag"]  # 形如 pkg.mod:func
    module_path, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"登记的目标 {target!r} 已失效：{type(exc).__name__}: {exc}。"
            "执行 `uv sync` 重新生成控制台脚本即可（python -m project_rag 不受影响，"
            "所以这个坑不会自己暴露）"
        ) from exc
    if not callable(getattr(module, attr, None)):
        raise AssertionError(f"登记的目标 {target!r} 里 {attr!r} 不是可调用对象")

    where = shutil.which("project-rag")
    return f"{target}" + (f"（可执行：{where}）" if where else "（当前 PATH 里找不到，非致命）")


def main() -> int:
    return report("M1.3 CLI 验收")


if __name__ == "__main__":
    sys.exit(main())
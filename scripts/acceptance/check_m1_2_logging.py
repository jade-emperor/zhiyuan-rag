#!/usr/bin/env python
"""M1.2 验收：日志地基。

用法::

    uv run python scripts/acceptance/check_m1_2_logging.py

本脚本固化的「公开契约」——这两样必须能从 ``project_rag.observability`` 直接 import::

    configure_logging(settings: Settings | None = None) -> None
        **每次调用都重建配置**（dictConfig 的 force=True 语义），而不是「首次生效后短路」。
        这样既保证重复调用不叠加 handler，又保证换一份 settings 能立刻生效。
        root logger 上恰好 1 个控制台 handler + 1 个文件 handler。

    get_logger(name: str) -> logging.Logger
        返回 project_rag.<name> 形式的 logger，便于按模块单独调级别。

以及这些行为约定：

* 日志文件 = ``<PROJECT_ROOT>/<settings.log_dir>/app.log``；
  ``log_dir`` 是相对路径时相对**项目根**，不是当前工作目录（CWD）。
* 目录不存在时自动创建。
* 级别取自 ``settings.root_log_level``（root 是第一道阀门），
  ``console_log_level`` / ``file_log_level`` 再各自过滤一层。
* 第三方库（httpx / urllib3 / pymilvus 等）的日志级别被抬到 WARNING 以上，
  避免把整个日志文件淹掉。
"""

from __future__ import annotations

import importlib
import io
import logging

import subprocess
import sys
import tempfile
import uuid
from contextlib import redirect_stderr
from pathlib import Path

from _harness import check, report
NOISY_LIBS = ("httpx", "httpcore", "urllib3", "pymilvus", "openai", "grpc")


def api():
    module = importlib.import_module("project_rag.observability")
    missing = [n for n in ("configure_logging", "get_logger") if not hasattr(module, n)]
    if missing:
        raise AssertionError(
            f"project_rag.observability 缺少 {missing}；"
            "请在 src/project_rag/observability/__init__.py 里显式 re-export"
        )
    return module


def root_handlers() -> tuple[list[logging.Handler], list[logging.Handler]]:
    """返回 (文件 handler 列表, 控制台 handler 列表)。

    ``files`` 显式标成 ``list[logging.Handler]``:推导出来的 ``list[FileHandler]``
    因为 list 不变型,赋不回声明里的 ``list[Handler]``。
    """
    root = logging.getLogger()
    files: list[logging.Handler] = [
        h for h in root.handlers if isinstance(h, logging.FileHandler)
    ]
    streams = [h for h in root.handlers if not isinstance(h, logging.FileHandler)]
    return files, streams


def configure_at(module, **overrides):
    """用一份临时 Settings 装配日志，overrides 覆盖 root_log_level / log_dir 等字段。"""
    from project_rag.config import Settings

    return module.configure_logging(Settings(**overrides))


# ── 1. 结构与幂等 ─────────────────────────────────────────────────────────────
@check("公开入口可导入（configure_logging / get_logger）")
def _() -> str:
    module = api()
    return f"{module.__name__} @ {Path(module.__file__).parent if module.__file__ else '?'}"


@check("get_logger 返回包命名空间下的 logger（project_rag.<name>）")
def _() -> str:
    logger = api().get_logger("ingest")
    if not logger.name.startswith("project_rag"):
        raise AssertionError(f"期望 project_rag.* 命名空间，实际 {logger.name!r}")
    return logger.name


@check("handler 恰好 2 个（1 控制台 + 1 文件），且重复装配不叠加")
def _() -> str:
    module = api()
    module.configure_logging()
    module.configure_logging()  # 幂等：第二次不得再挂一套
    module.configure_logging()
    files, streams = root_handlers()
    if len(files) != 1 or len(streams) != 1:
        raise AssertionError(
            f"期望 1 文件 + 1 控制台，实际 {len(files)} 文件 + {len(streams)} 控制台；"
            "重复调用叠加了 handler（日志会重复打印），用 dictConfig(force=True) 或加守卫"
        )
    # 再收窄一次拿 baseFilename:files 声明成了 list[Handler],
    # 上面那句 isinstance 的收窄已经随列表推导丢掉了
    file_handler = next(h for h in files if isinstance(h, logging.FileHandler))
    return f"文件={Path(file_handler.baseFilename).name} 控制台={type(streams[0]).__name__}"


# ── 2. 落盘与目录创建 ─────────────────────────────────────────────────────────
@check("目录不存在时自动创建，日志能落盘且包含消息与来源")
def _() -> str:
    module = api()
    marker = f"m1.2-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "logs"
        configure_at(module, log_dir=str(target))
        try:
            logger = module.get_logger("acceptance")
            logger.info("落盘检查 %s", marker)
            for handler in logging.getLogger().handlers:
                handler.flush()
            log_file = target / "app.log"
            if not log_file.exists():
                raise AssertionError(
                    f"没有生成日志文件：{log_file}。"
                    "若你的 configure_logging 在首次生效后就短路，这里也会失败——"
                    "契约要求每次调用都按传入的 settings 重建"
                )
            text = log_file.read_text(encoding="utf-8")
            if marker not in text:
                raise AssertionError("日志文件里没有刚写入的消息")
            line = next(ln for ln in text.splitlines() if marker in ln)
            missing = [p for p in ("acceptance", "INFO") if p not in line]
            if missing:
                raise AssertionError(f"格式里缺少 {missing}，实际行：{line!r}")
        finally:
            module.configure_logging()  # 换回默认配置，释放临时文件 handler
    return "目录自动创建 + 消息/级别/来源齐全"


@check("级别过滤生效：DEBUG 落盘，WARNING 级别下 DEBUG 不落盘")
def _() -> str:
    module = api()
    low, high = f"dbg-{uuid.uuid4().hex}", f"warn-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            # 三个级别全部显式钉死：本检查只测 root 这道阀门，
            # 绝不能依赖 config.yaml 里恰好写了什么，否则脚本会「碰巧通过」
            configure_at(
                module, root_log_level="DEBUG", file_log_level="DEBUG", log_dir=tmp
            )
            module.get_logger("acceptance").debug("debug 检查 %s", low)
            configure_at(
                module, root_log_level="WARNING", file_log_level="DEBUG", log_dir=tmp
            )
            module.get_logger("acceptance").debug("debug 检查 %s", high)
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = (Path(tmp) / "app.log").read_text(encoding="utf-8")
            if low not in text:
                raise AssertionError("DEBUG 级别下 debug 日志没有落盘")
            if high in text:
                raise AssertionError("WARNING 级别下 debug 日志仍然落盘：级别没生效")
        finally:
            module.configure_logging()
    return "DEBUG 进 / WARNING 挡"


@check("控制台与文件级别可各自独立设置（root 只做第一道阀门）")
def _() -> str:
    module = api()
    dbg, wrn = f"onlyfile-{uuid.uuid4().hex}", f"both-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.StringIO()
        try:
            # console handler 绑的是配置那一刻的 sys.stderr，所以重定向要包住 configure 调用
            with redirect_stderr(buf):
                configure_at(
                    module,
                    root_log_level="NOTSET",
                    console_log_level="WARNING",
                    file_log_level="DEBUG",
                    log_dir=tmp,
                )
                log = module.get_logger("acceptance")
                log.debug("只该进文件 %s", dbg)
                log.warning("两边都该有 %s", wrn)
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = (Path(tmp) / "app.log").read_text(encoding="utf-8")
            console = buf.getvalue()
            if dbg not in text:
                raise AssertionError("file_log_level=DEBUG，但 debug 没进文件")
            if dbg in console:
                raise AssertionError("console_log_level=WARNING，但 debug 仍打到控制台")
            if wrn not in text or wrn not in console:
                raise AssertionError("warning 应该同时进文件和屏幕")
        finally:
            module.configure_logging()
    return "file=DEBUG / console=WARNING 各自生效"


# ── 3. log_dir 的基准目录 ─────────────────────────────────────────────────────
@check("log_dir 是相对路径时，相对项目根而不是 CWD")
def _() -> str:
    from project_rag.config import PROJECT_ROOT

    marker = f"cwd-check-{uuid.uuid4().hex}"
    code = (
        "from project_rag.observability import configure_logging, get_logger;"
        "import logging;"
        "configure_logging();"
        f"get_logger('acceptance').warning({marker!r});"
        "[h.flush() for h in logging.getLogger().handlers]"
    )
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"在 {tmp} 下执行失败：{(proc.stderr or proc.stdout).strip()[-300:]}"
        )
    log_file = PROJECT_ROOT / "logs" / "app.log"
    if marker not in log_file.read_text(encoding="utf-8", errors="ignore"):
        raise AssertionError(
            f"在其它工作目录下运行时日志没落到 {log_file}；"
            "log_dir 大概被当成了相对 CWD 的路径（要和 .env 用同样的绝对路径处理）"
        )
    return f"落盘于 {log_file.relative_to(PROJECT_ROOT)}"


# ── 4. 第三方库降噪 ───────────────────────────────────────────────────────────
@check("第三方库日志被抬到 WARNING 以上（否则日志会被淹没）")
def _() -> str:
    module = api()
    module.configure_logging()
    noisy = {
        name: logging.getLogger(name).getEffectiveLevel()
        for name in NOISY_LIBS
        if logging.getLogger(name).getEffectiveLevel() < logging.WARNING
    }
    if noisy:
        pretty = ", ".join(f"{k}={logging.getLevelName(v)}" for k, v in noisy.items())
        raise AssertionError(f"以下库仍在刷低级别日志：{pretty}")
    return f"{len(NOISY_LIBS)} 个库已降噪"


def main() -> int:
    return report("M1.2 日志验收")


if __name__ == "__main__":
    sys.exit(main())
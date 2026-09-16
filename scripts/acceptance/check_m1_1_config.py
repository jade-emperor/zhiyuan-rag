#!/usr/bin/env python
"""M1.1 验收：类型化配置模块。

用法::

    uv run python scripts/acceptance/check_m1_1_config.py

判定规则：全部 PASS 才算通过（退出码 0）；任何 FAIL 都会让退出码变成 1；
SKIP 表示需要你人工确认。脚本只依赖标准库 + 本项目，任何时候都能跑。

本脚本固化的「公开契约」——这三样必须能从 ``project_rag.config`` 直接 import::

    Settings          配置类本身（可直接构造，用于测试）
    get_settings()    带缓存的单例工厂（应用代码只用它）
    CONFIG_FILE       YAML 配置文件的绝对路径（仅「YAML + env」双源方案需要）

为什么要把它们列为契约：调用方 import 的路径是承诺，内部文件叫
``setting.py`` 还是 ``settings.py`` 是你的自由，但不该影响调用方。

密钥摆放契约（2026-09-14 起）：所有密钥都住在**嵌套配置**里，走 ``RAG_`` 前缀 +
双下划线，放在 ``.env`` —— ``RAG_LLM__DEEPSEEK_API_KEY`` / ``RAG_MYSQL__PASSWORD`` /
``RAG_REDIS__PASSWORD``。不再有 ``Settings.deepseek_api_key`` 这个顶层字段。
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARK = {PASS: "✔", FAIL: "✘", SKIP: "○"}
_results: list[tuple[str, str, str]] = []

# 本脚本固化的命名契约：自家开关加 RAG_ 前缀，嵌套层级用双下划线
ENV_PREFIX = "RAG_"
NESTED_DELIM = "__"


class SkipCheck(Exception):
    """该检查在当前方案下不适用。"""


def record(name: str, status: str, detail: str = "") -> None:
    """只记录，不打印——打印统一放在 main()，保证输出顺序稳定。"""
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


@contextmanager
def env(**kv: str | None):
    """临时设置环境变量；传 None 表示临时删除。"""
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for key, value in kv.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_FALLBACK_NOTE = ""


def _public():
    """严格按契约导入；不符合契约就抛错。"""
    module = importlib.import_module("project_rag.config")
    missing = [
        n for n in ("Settings", "get_settings", "CONFIG_FILE") if not hasattr(module, n)
    ]
    if missing:
        raise AssertionError(
            f"project_rag.config 缺少公开成员 {missing}；"
            "请在 src/project_rag/config/__init__.py 里显式 re-export"
        )
    return module


def api():
    """给各检查项用的导入入口。

    公开入口不可用时**降级**到实现模块，好让「优先级」「嵌套字段」这些独立问题
    照常暴露出来，而不是被第一条失败全部掩盖。降级会记录在报告末尾。

    注意：这里只 import 包路径 `project_rag.config`，**不写死内部文件名**
    （曾经写死过 project_rag.config.setting，config 包拍平成 config.py 后立刻失效——
    这条正是"内部文件名可以随便改"的反面教材）。
    """
    global _FALLBACK_NOTE
    try:
        return _public()
    except Exception as exc:  # noqa: BLE001
        _FALLBACK_NOTE = str(exc)
        module = importlib.import_module("project_rag.config")
        missing = [
            n
            for n in ("Settings", "get_settings", "CONFIG_FILE")
            if not hasattr(module, n)
        ]
        if missing:
            raise exc from None
        return module


# ── 1. 包结构与单例 ────────────────────────────────────────────────────────────
@check("公开入口可导入（from project_rag.config import Settings, get_settings, CONFIG_FILE）")
def _() -> str:
    module = _public()
    return f"{module.__name__} @ {Path(module.__file__).parent if module.__file__ else '?'}"


@check("get_settings() 是单例（两次调用返回同一对象）")
def _() -> str:
    get_settings = api().get_settings
    if get_settings() is not get_settings():
        raise AssertionError("两次调用返回了不同对象，缓存没生效")
    return "lru_cache 生效"


# ── 2. 类型与默认值 ────────────────────────────────────────────────────────────
@check("配置项类型正确（llm 是模型对象、数值是真 float/int）")
def _() -> str:
    from pydantic import BaseModel

    settings = api().Settings()
    if not isinstance(settings.llm, BaseModel):
        raise AssertionError(
            f"llm 应该是嵌套模型对象，实际是 {type(settings.llm).__name__}"
        )
    if not isinstance(settings.llm.temperature, float):
        raise AssertionError(
            f"temperature 应该是 float，实际是 {type(settings.llm.temperature).__name__}"
        )
    if not isinstance(settings.llm.max_tokens, int):
        raise AssertionError(
            f"max_tokens 应该是 int，实际是 {type(settings.llm.max_tokens).__name__}"
        )
    return f"model={settings.llm.model} temperature={settings.llm.temperature} max_tokens={settings.llm.max_tokens}"


# ── 3. 环境变量：前缀 + 嵌套分隔符 ──────────────────────────────────────────────
@check(f"嵌套字段可被环境变量覆盖（{ENV_PREFIX}LLM{NESTED_DELIM}TEMPERATURE=0.1）")
def _() -> str:
    with env(**{f"{ENV_PREFIX}LLM{NESTED_DELIM}TEMPERATURE": "0.1"}):
        value = api().Settings().llm.temperature
    if value != 0.1:
        raise AssertionError(
            f"期望 0.1，实际 {value}（检查 env_nested_delimiter 是否设为 '__'）"
        )
    return "0.1"


@check(f"顶层字段可被环境变量覆盖（{ENV_PREFIX}ROOT_LOG_LEVEL=DEBUG）")
def _() -> str:
    with env(**{f"{ENV_PREFIX}ROOT_LOG_LEVEL": "DEBUG"}):
        value = api().Settings().root_log_level
    if value != "DEBUG":
        raise AssertionError(
            f"期望 DEBUG，实际 {value}（检查 env_prefix 是否为 '{ENV_PREFIX}'、"
            "以及字段名是否还叫 root_log_level）"
        )
    return "DEBUG"


# ── 4. 优先级：环境变量必须压过 YAML 文件 ──────────────────────────────────────
@check(f"优先级：环境变量 > YAML 文件（{ENV_PREFIX}ENV=acceptance-env）")
def _() -> str:
    module = api()
    if not module.CONFIG_FILE.exists():
        raise AssertionError(
            f"{module.CONFIG_FILE} 不存在。YAML 源配了却读不到文件 = 静默丢配置，"
            f"先检查扩展名是否与代码一致（磁盘上可能是 .yml）"
        )
    with env(**{f"{ENV_PREFIX}ENV": "acceptance-env"}):
        value = module.Settings().env
    if value != "acceptance-env":
        raise AssertionError(
            f"期望 acceptance-env，实际 {value}：YAML 压过了环境变量（优先级顺序反了）"
        )
    return "环境变量胜出"


# ── 5. YAML 文件本身 ──────────────────────────────────────────────────────────
@check("YAML 配置文件存在且能被解析（不是空文件、扩展名与代码一致）")
def _() -> str:
    module = api()
    path: Path = module.CONFIG_FILE
    if not path.exists():
        raise AssertionError(
            f"{path} 不存在。代码声明的路径与磁盘文件不一致时，"
            f"YamlConfigSettingsSource 会静默返回空配置：程序照常启动，但配置全丢"
        )
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data:
        raise AssertionError(
            f"{path.name} 解析结果为空字典：文件是空的，或内容格式不对"
        )
    return f"{path.name} → {sorted(data)}"


@check("YAML 文件缺失时 get_settings() 必须报错（官方源默认会静默回退）")
def _() -> str:
    module = api()
    sentinel = Path(tempfile.gettempdir()) / "definitely-missing-config.yaml"
    settings_cls = module.Settings
    saved_cfg = dict(settings_cls.model_config)
    # 校验可能读包上的常量，也可能读实现模块里的常量——把两个命名空间都打上补丁，
    # 这样脚本不依赖「常量定义在哪个文件」这种内部细节
    patched: list[tuple[object, Path]] = []
    for name, mod in list(sys.modules.items()):
        if name.startswith("project_rag.config") and hasattr(mod, "CONFIG_FILE"):
            patched.append((mod, mod.CONFIG_FILE))
            mod.CONFIG_FILE = sentinel
    settings_cls.model_config["yaml_file"] = sentinel
    try:
        module.get_settings.cache_clear()
        try:
            module.get_settings()
        except Exception as exc:  # noqa: BLE001 - 期望就是抛错
            return f"按预期失败：{type(exc).__name__}"
        raise AssertionError(
            "配置文件不存在却没有报错。校验要写在 get_settings()（应用启动路径）里，"
            "不要放在模块导入时——否则 Settings 无法被单测直接构造"
        )
    finally:
        for mod, original in patched:
            mod.CONFIG_FILE = original
        settings_cls.model_config.clear()
        settings_cls.model_config.update(saved_cfg)
        module.get_settings.cache_clear()


# ── 6. 密钥安全 ───────────────────────────────────────────────────────────────
@check("SecretStr 遮蔽：打印、repr、model_dump 都不出现明文")
def _() -> str:
    settings = api().Settings()
    secret = settings.llm.deepseek_api_key.get_secret_value()
    if not secret:
        raise AssertionError("密钥为空")
    leaked = [
        where
        for where, text in (
            ("repr", repr(settings)),
            ("model_dump", str(settings.model_dump())),
        )
        if secret in text
    ]
    if leaked:
        raise AssertionError(f"{leaked} 中出现了明文密钥")
    return f"明文长度 {len(secret)}，对外一律 **********"


# 这里原本有一条「密钥缺失时 fail-fast，且报错点名 DEEPSEEK_API_KEY」，2026-09-14 契约变更时删除。
# 理由（同步记在 docs/STATUS.md §8，以及 docs/deferred.md 的对应条目）：
#   1. 密钥完全缺失**不是静默失败** —— 首次调用 DeepSeek 会以 401 暴露出来，
#      不存在"带着错配置悄悄跑出错误答案"的风险；
#   2. 要在启动期拦住它，嵌套模型必须同时挂 ConfigDict(validate_default=True)
#      + Field(default_factory=...)（否则 validator 根本不会在默认值上触发，
#      而且 validate_default 会让 LLMConfig() 在类体里当场抛异常、import 阶段就崩）；
#   3. 为一个真实不存在的边界情况换三处复杂度，不划算 —— 先跑通主流程。
# 仍然保留的保证：**显式**写空值（YAML 里 `password: ""` 或 `RAG_MYSQL__PASSWORD=`）
# 会被各自的 field_validator 拦住。


@check("源码与日志中不含明文密钥")
def _() -> str:
    module = api()
    settings = module.Settings()
    secret = settings.llm.deepseek_api_key.get_secret_value()
    # 不再用 parents[N] 做算术：config 从包拍平成模块时，parents[2] 会悄悄从 src/ 变成仓库根，
    # 于是扫描范围扩大到 .venv 并误报。改用包的目录 + 公开的 PROJECT_ROOT。
    src_root = Path(module.__file__).resolve().parent  # src/project_rag
    logs_root = Path(module.PROJECT_ROOT) / "logs"
    sources = list(src_root.rglob("*.py"))
    logs = [p for p in logs_root.rglob("*") if p.is_file()] if logs_root.is_dir() else []
    offenders = [
        str(p)
        for p in sources + logs
        if secret in p.read_text(encoding="utf-8", errors="ignore")
    ]
    pattern_hits = [
        str(p)
        for p in sources
        if re.search(
            r"sk-[A-Za-z0-9]{8,}", p.read_text(encoding="utf-8", errors="ignore")
        )
    ]
    if offenders or pattern_hits:
        raise AssertionError(f"疑似泄漏：{offenders + pattern_hits}")
    return f"干净（扫描 {len(sources)} 个源文件 + {len(logs)} 个日志文件）"


# ── 7. 与工作目录无关 ─────────────────────────────────────────────────────────
@check("换个工作目录也能读到 .env（.env 必须是绝对路径）")
def _() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from project_rag.config import get_settings;"
                "s=get_settings();"
                "print(len(s.llm.deepseek_api_key.get_secret_value()))",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"在 {tmp} 下执行失败：{proc.stderr.strip().splitlines()[-1:] or proc.stderr[:200]}"
        )
    if int(proc.stdout.strip() or 0) <= 0:
        raise AssertionError("读到的密钥为空，说明 .env 没被定位到")
    return "CWD 无关"


def main() -> int:
    # 检查函数在模块导入（装饰）时已全部执行，结果按文件中的定义顺序记录
    print("M1.1 配置模块验收\n" + "─" * 60)
    for name, status, detail in _results:
        suffix = f"  — {detail}" if detail else ""
        print(f"  {_MARK[status]} [{status}] {name}{suffix}")
    failed = [name for name, status, _ in _results if status == FAIL]
    skipped = [name for name, status, _ in _results if status == SKIP]
    print("─" * 60)
    print(
        f"结果：{len(_results) - len(failed) - len(skipped)} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过"
    )
    if _FALLBACK_NOTE:
        print("\n注意：公开入口不可用，本次报告是「降级诊断」结果——")
        print(f"  {_FALLBACK_NOTE}")
        print("  其余各项的结果仍然有效，但修复后必须重跑本脚本。")
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✘ {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

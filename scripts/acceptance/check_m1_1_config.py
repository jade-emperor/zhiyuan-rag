#!/usr/bin/env python
"""验收：类型化配置模块。

用法：

    uv run python scripts/acceptance/check_m1_1_config.py

判定规则：全部 PASS 才算通过（退出码 0）；任何 FAIL 都会让退出码变成 1；
SKIP 表示需要你人工确认。脚本只依赖标准库 + 本项目，任何时候都能跑。

本脚本固化的「公开契约」——这三样必须能从 ``project_rag.config`` 直接 import：

    Settings          配置类本身（可直接构造，用于测试）
    get_settings()    带缓存的单例工厂（应用代码只用它）
    CONFIG_FILE       YAML 配置文件的绝对路径（仅「YAML + env」双源方案需要）

为什么要把它们列为契约：调用方 import 的路径是承诺，内部文件叫
``setting.py`` 还是 ``settings.py`` 是你的自由，但不该影响调用方。

密钥摆放契约：所有密钥都住在**嵌套配置**里，走 ``RAG_`` 前缀 +
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
import typing
from pathlib import Path

from _harness import check, report, env

# 本脚本固化的命名契约：自家开关加 RAG_ 前缀，嵌套层级用双下划线
ENV_PREFIX = "RAG_"
NESTED_DELIM = "__"


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
            "请在 src/project_rag/config.py 里显式 re-export"
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
    # **先把值取出来再 isinstance**:settings 是 untyped 的,`settings.llm` 被
    # isinstance 判定收窄成 BaseModel 之后,后面就再也看不到 LLMConfig 的字段了
    llm = settings.llm
    temperature = llm.temperature
    max_tokens = llm.max_tokens
    model_name = llm.model
    if not isinstance(llm, BaseModel):
        raise AssertionError(
            f"llm 应该是嵌套模型对象，实际是 {type(llm).__name__}"
        )
    if not isinstance(temperature, float):
        raise AssertionError(
            f"temperature 应该是 float，实际是 {type(temperature).__name__}"
        )
    if not isinstance(max_tokens, int):
        raise AssertionError(
            f"max_tokens 应该是 int，实际是 {type(max_tokens).__name__}"
        )
    return f"model={model_name} temperature={temperature} max_tokens={max_tokens}"


@check("api 段存在且预热默认开着（把冷启动从第一个用户挪到启动）")
def _() -> str:
    settings = api().Settings()
    warmup = settings.api.warmup
    if warmup is not True:
        raise AssertionError(f"配置里的 api.warmup 应为 True（把冷启动留在启动阶段），实际 {warmup!r}")
    # 也钉住**类默认值**：config.yaml 里写了 true，会把类默认值的改动盖住 ——
    # 只断言「有效值」的话，把默认值改成 False 这条检查照样绿（反向测试抓到的）
    ApiConfig = getattr(importlib.import_module("project_rag.config"), "ApiConfig")
    if ApiConfig().warmup is not True:
        raise AssertionError("ApiConfig 的类默认值也该是 True（新建实例不该悄悄改变语义）")
    exported = importlib.import_module("project_rag.config").__all__
    if "ApiConfig" not in exported:
        raise AssertionError("ApiConfig 没出现在 config 的 __all__ 里 —— 公开面不完整")
    return f"api.warmup={warmup}，ApiConfig 已导出"


@check("cache 段存在且 TTL 可配置（TTL 从 answer_faq 的字面量变成配置项）")
def _() -> str:
    """TTL 得是配置项，不能再是 ``answer_faq.DEFAULT_CACHE_TTL_S`` 那个 300 的字面量。

    为什么同时钉**类默认值**与**有效值**：config.yaml 里写了值，
    会把类默认值的改动整个盖住 —— 只断言有效值的话，把类默认值改成 60 这条照样绿。

    为什么**不**断言 YAML 里的有效值等于某个具体数字：TTL 是部署方的
    部署旋钮，不是机制。只要求它落在「正整数」这个合法集合里。
    """
    settings = api().Settings()
    ttl = settings.cache.ttl_s
    # bool 是 int 的子类，True 会蒙混过关 —— 所以先把它排掉
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise AssertionError(f"cache.ttl_s 应为 int（秒），实际 {type(ttl).__name__}")
    if ttl <= 0:
        raise AssertionError(
            f"cache.ttl_s 应为正整数（0 或负数 = 写进去就过期，等于每次必穿透），实际 {ttl}"
        )

    CacheConfig = getattr(importlib.import_module("project_rag.config"), "CacheConfig")
    if CacheConfig().ttl_s != 300:
        raise AssertionError(
            "CacheConfig 的类默认值应为 300 —— 加这个字段不许悄悄改变语义："
            f"落配置之前 answer_faq 的行为就是 300 秒。实际 {CacheConfig().ttl_s}"
        )
    exported = importlib.import_module("project_rag.config").__all__
    if "CacheConfig" not in exported:
        raise AssertionError("CacheConfig 没出现在 config 的 __all__ 里 —— 公开面不完整")
    return f"cache.ttl_s={ttl}，CacheConfig 类默认 300 且已导出"


@check("api 段：host / port / cors_origins / warmup_query 可配置（部署旋钮从代码里搬出来）")
def _() -> str:
    """绑定地址、CORS、预热查询原先全是 ``api/app.py`` 里的字面量。

    **为什么类默认值必须原样保留**:默认值一改,语义就变了 —— 绑回环时 WSL 里
    Windows 浏览器连不上(实测),CORS 收紧成空列表前端直接跨域失败。
    **为什么不钉有效值**:绑哪个地址、开哪些源是部署方的选择。
    """
    from project_rag.config import ApiConfig

    settings = api().Settings()
    bare = ApiConfig()
    if bare.host != "0.0.0.0":
        raise AssertionError(
            f"ApiConfig 类默认 host 应为 0.0.0.0(绑回环时 WSL 里 Windows 浏览器连不上),实际 {bare.host!r}"
        )
    if bare.port != 8080:
        raise AssertionError(f"ApiConfig 类默认 port 应为 8080,实际 {bare.port!r}")
    if bare.warmup_query != "装饰器有什么用":
        raise AssertionError(
            "ApiConfig 类默认 warmup_query 应为「装饰器有什么用」—— 预热要真的检索到东西才有意义,"
            f"实际 {bare.warmup_query!r}"
        )
    if bare.cors_origins != ["*"]:
        raise AssertionError(f"ApiConfig 类默认 cors_origins 应为 ['*'],实际 {bare.cors_origins!r}")

    host = settings.api.host
    port = settings.api.port
    origins = settings.api.cors_origins
    query = settings.api.warmup_query
    if not isinstance(host, str) or not host.strip():
        raise AssertionError(f"api.host 应为非空字符串,实际 {host!r}")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise AssertionError(f"api.port 应为 1..65535 的 int,实际 {port!r}")
    if not isinstance(origins, list) or not origins or not all(isinstance(o, str) for o in origins):
        raise AssertionError(f"api.cors_origins 应为非空的字符串列表,实际 {origins!r}")
    if not isinstance(query, str) or not query.strip():
        raise AssertionError(f"api.warmup_query 应为非空字符串,实际 {query!r}")
    exported = importlib.import_module("project_rag.config").__all__
    for name in ("ApiConfig", "CacheConfig"):
        if name not in exported:
            raise AssertionError(f"{name} 没出现在 config 的 __all__ 里")
    return f"api {host}:{port}，origins={len(origins)} 条，warmup_query={query!r}"


@check("连接超时可配置：redis.socket_timeout_s / mysql.connect_timeout_s")
def _() -> str:
    """超时原先是一处一个的字面量(``_SOCKET_TIMEOUT = 1.0``、``_CONNECT_TIMEOUT_S = 5``)。

    **为什么该可配**:本机 Docker 与跨网部署的两个合理值不一样,而它又直接决定
    「Redis 挂了之后要等多久才降级」—— 那正是降级放大的时长。
    """
    from project_rag.config import MySQLConfig, RedisConfig

    settings = api().Settings()
    if RedisConfig().socket_timeout_s != 1.0:
        raise AssertionError(f"RedisConfig 类默认 socket_timeout_s 应为 1.0,实际 {RedisConfig().socket_timeout_s!r}")
    if MySQLConfig().connect_timeout_s != 5:
        raise AssertionError(f"MySQLConfig 类默认 connect_timeout_s 应为 5,实际 {MySQLConfig().connect_timeout_s!r}")

    redis_timeout = settings.redis.socket_timeout_s
    mysql_timeout = settings.mysql.connect_timeout_s
    if not isinstance(redis_timeout, int | float) or isinstance(redis_timeout, bool):
        raise AssertionError(f"redis.socket_timeout_s 应为数值,实际 {type(redis_timeout).__name__}")
    if redis_timeout <= 0:
        raise AssertionError(f"redis.socket_timeout_s 应为正数(0 = 立即超时,永远读不到缓存),实际 {redis_timeout}")
    if not isinstance(mysql_timeout, int) or isinstance(mysql_timeout, bool) or mysql_timeout <= 0:
        raise AssertionError(f"mysql.connect_timeout_s 应为正整数,实际 {mysql_timeout!r}")
    return f"redis={redis_timeout}s mysql={mysql_timeout}s"


@check("rag.ingest_batch_size 可配置（消灭 ingest_documents 里第二个 64）")
def _() -> str:
    """批大小原先在 ``ingest_documents.DEFAULT_BATCH_SIZE`` 和 ``ingest --batch-size`` 各写一个 64。

    它随设备变(CPU 上批量越大越划算,显存小的机器反而要调小),是旋钮不是机制。
    """
    from project_rag.config import RagConfig

    settings = api().Settings()
    if RagConfig().ingest_batch_size != 64:
        raise AssertionError(f"RagConfig 类默认 ingest_batch_size 应为 64,实际 {RagConfig().ingest_batch_size!r}")
    value = settings.rag.ingest_batch_size
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AssertionError(f"rag.ingest_batch_size 应为正整数(0 会让 ingest_documents 抛 ValueError),实际 {value!r}")
    return f"rag.ingest_batch_size={value}"


@check("日志轮转可配置：log_max_bytes / log_backup_count")
def _() -> str:
    """``logging_setup`` 里原先写死 10MB × 5 份。``log_dir`` 早就是配置了,轮转参数却不是。"""
    settings = api().Settings()
    if settings.log_max_bytes != 10 * 1024 * 1024:
        raise AssertionError(f"log_max_bytes 类默认应为 10MB(10485760),实际 {settings.log_max_bytes!r}")
    if settings.log_backup_count != 5:
        raise AssertionError(f"log_backup_count 类默认应为 5,实际 {settings.log_backup_count!r}")
    if not isinstance(settings.log_max_bytes, int) or settings.log_max_bytes <= 0:
        raise AssertionError(f"log_max_bytes 应为正整数,实际 {settings.log_max_bytes!r}")
    if not isinstance(settings.log_backup_count, int) or settings.log_backup_count < 0:
        raise AssertionError(f"log_backup_count 应为非负整数(0 = 不保留旧文件),实际 {settings.log_backup_count!r}")
    return f"{settings.log_max_bytes} bytes × {settings.log_backup_count} 份"


@check("llm.thinking：类默认 auto（= 不动服务端默认），取值受限，可被环境变量覆盖")
def _() -> str:
    """一个变量同时管「思考开关」与「强度」。

    为什么**类默认值**必须是 auto:加这个字段不许悄悄改变语义 —— auto = 一个参数都不发,
    沿用服务端默认(开启、high)。想更快得显式写 off。
    """
    from project_rag.config import LLMConfig

    allowed = set(typing.get_args(LLMConfig.model_fields["thinking"].annotation))
    if allowed != {"auto", "off", "low", "high", "max"}:
        raise AssertionError(
            f"可选值应为 auto / off / low / high / max，实际 {sorted(allowed)}；"
            "改取值集合就得同步 check_m5_stream 里的映射检查"
        )
    # **类默认值**:config.yaml 里写了什么会把类默认值的改动盖住 ——
    # 只断言「有效值」的话,把类默认值改成 off 这条检查照样绿(反向测试亲自抓到过两次,
    # 上一次是 ApiConfig.warmup)
    if LLMConfig().thinking != "auto":
        raise AssertionError(
            f"LLMConfig 的类默认值该是 auto,实际 {LLMConfig().thinking!r} —— "
            "新建配置对象不该悄悄改变语义"
        )
    # **有效值(来自 config.yaml)只钉「是个合法档位」,不钉具体是哪一档**:
    # 这一档正是部署时要调的东西(off = 最快;auto = 沿用服务端默认),把它钉成字面量
    # 等于让人一改配置验收就红 —— 那是检查在越权,不是配置错了。
    # 真正该拦的是**打错的档位**(例如 "highh"),那会被 pydantic 的 Literal 拦下。
    effective = api().Settings().llm.thinking
    if effective not in allowed:
        raise AssertionError(f"config.yaml 里的档位应是 {sorted(allowed)} 之一,实际 {effective!r}")
    with env(**{f"{ENV_PREFIX}LLM{NESTED_DELIM}THINKING": "off"}):
        overridden = api().Settings().llm.thinking
    if overridden != "off":
        raise AssertionError(f"{ENV_PREFIX}LLM{NESTED_DELIM}THINKING=off 应生效，实际 {overridden!r}")
    return f"类默认 auto；可选 {sorted(allowed)}；config.yaml={effective}；env 可覆盖成 off"


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
            # setattr 而不是直接赋值:mod 是 ModuleType(以及这里的 object),
            # pyright 不允许给「未知属性」赋值,而打补丁本来就该走 setattr
            setattr(mod, "CONFIG_FILE", sentinel)
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
            setattr(mod, "CONFIG_FILE", original)
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


# 这里原本有一条「密钥缺失时 fail-fast，且报错点名 DEEPSEEK_API_KEY」，契约变更时删除。
# 理由：
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
    # 不再用 parents[N] 做算术,也不用 module.__file__(类型是 str | None):
    # config 从包拍平成模块时,parents[2] 会悄悄从 src/ 变成仓库根,
    # 于是扫描范围扩大到 .venv 并误报。用公开的 PROJECT_ROOT 拼,既确定又无歧义。
    src_root = Path(module.PROJECT_ROOT) / "src" / "project_rag"
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



@check("新增旋钮的类默认值：读一致性 strong、重排半精度 false（默认不改变既有语义）")
def _() -> str:
    """两个新加的旋钮，类默认值都取"这段代码一直以来的行为"：

    * ``MilvusConfig.read_consistency`` 默认 ``strong`` —— 代码里原先硬编码就是 Strong。
      改成 bounded 是**部署选择**（实测 399ms → 2ms，200×），写进 config.yaml，不写进类默认：
      默认值一改，语义就跟着变（这正是本条与上面几条存在的原因）。
    * ``EmbeddingConfig.rerank_use_f16`` 默认 False —— 与嵌入的 ``use_f16`` 分开，
      因为两者的收益/精度风险不同（重排实测 3.2×、嵌入 2.4×）。
    """
    from project_rag.config import EmbeddingConfig, MilvusConfig

    bare_milvus = MilvusConfig()
    if bare_milvus.read_consistency != "strong":
        raise AssertionError(
            f"MilvusConfig 类默认 read_consistency 应为 'strong'（= 代码原先硬编码的行为），"
            f"实际 {bare_milvus.read_consistency!r} —— 默认值一改语义就变"
        )
    bare_embedding = EmbeddingConfig()
    if bare_embedding.rerank_use_f16 is not False:
        raise AssertionError(
            f"EmbeddingConfig 类默认 rerank_use_f16 应为 False，实际 {bare_embedding.rerank_use_f16!r}"
        )
    allowed = {"bounded", "eventually", "strong"}
    args = set(typing.get_args(MilvusConfig.model_fields["read_consistency"].annotation))
    if args != allowed:
        raise AssertionError(f"read_consistency 的取值域变了：{sorted(args)}，契约是 {sorted(allowed)}")
    return "read_consistency 默认 strong（域 bounded/eventually/strong）；rerank_use_f16 默认 False"


@check("新增旋钮的类默认值：索引 FLAT、关键字后端 memory（两档都不改既有语义）")
def _() -> str:
    """索引类型与关键字后端这两个新旋钮，类默认值同样取"一直以来的行为"：

    * ``MilvusConfig.index.type`` 默认 ``FLAT`` —— 代码里原先硬编码就是 FLAT（精确检索）。
      改成 IVF_FLAT 是**部署选择**（近似检索换延迟），而且**建集合那一刻定格**，
      所以默认值不动：新机器第一次 ingest 得到的东西必须与老部署一致。
    * ``KeywordConfig.backend`` 默认 ``memory`` —— 进程内 BM25。换成 ``milvus``
      （稀疏向量进库）要求集合带稀疏列，也就是**要重灌**；默认值一改，
      老集合会在写入/检索时当场报错（那条报错是对的，但不该由一个默认值触发）。
    """
    from project_rag.config import KeywordConfig, MilvusConfig

    index = MilvusConfig().index
    if index.type != "FLAT":
        raise AssertionError(
            f"MilvusConfig.index.type 类默认应为 'FLAT'（= 原先硬编码的索引），实际 {index.type!r}"
            " —— 默认值一改，新集合建出来的东西就和老部署不是一回事了"
        )
    if index.nlist < 1 or index.nprobe < 1 or index.nprobe > index.nlist:
        raise AssertionError(f"索引参数默认值不合法：{index}")
    keyword = KeywordConfig()
    if keyword.backend != "memory":
        raise AssertionError(
            f"KeywordConfig.backend 类默认应为 'memory'（进程内 BM25），实际 {keyword.backend!r}"
            " —— 换成 milvus 要求集合带稀疏列（要重灌），不能靠默认值静默切换"
        )
    if (keyword.bm25_k1, keyword.bm25_b) != (1.5, 0.75):
        raise AssertionError(
            f"BM25 常数默认值该与 rank_bm25 的默认一致（1.5 / 0.75），实际 "
            f"{(keyword.bm25_k1, keyword.bm25_b)} —— 两档的形状要能对上"
        )
    allowed_index = {"FLAT", "IVF_FLAT"}
    args = set(typing.get_args(MilvusConfig.model_fields["index"].annotation))
    _ = args  # index 是嵌套模型，取值域在 MilvusIndexConfig 上，见下
    from project_rag.config import MilvusIndexConfig

    got = set(typing.get_args(MilvusIndexConfig.model_fields["type"].annotation))
    if got != allowed_index:
        raise AssertionError(f"index.type 的取值域变了：{sorted(got)}，契约是 {sorted(allowed_index)}")
    backends = set(typing.get_args(KeywordConfig.model_fields["backend"].annotation))
    if backends != {"memory", "milvus"}:
        raise AssertionError(f"keyword.backend 的取值域变了：{sorted(backends)}")
    return "索引默认 FLAT（域 FLAT/IVF_FLAT）、关键字后端默认 memory（域 memory/milvus）、BM25 1.5/0.75"


def main() -> int:
    # 检查函数在模块导入（装饰）时已全部执行，结果按文件中的定义顺序记录
    notes = (
        [
            "注意：公开入口不可用，本次报告是「降级诊断」结果——",
            f"  {_FALLBACK_NOTE}",
            "  其余各项的结果仍然有效，但修复后必须重跑本脚本。",
        ]
        if _FALLBACK_NOTE
        else []
    )
    return report("配置模块验收", notes=notes)


if __name__ == "__main__":
    sys.exit(main())

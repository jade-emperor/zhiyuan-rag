"""日志地基:统一装配 root logger,日志同时落到控制台与文件。
关键函数:
    (1)configure_logging()进行全局日志配置。(2)get_logger(日志名字)获得logger日志对象。

三个关键设计:

1. ``configure_logging()`` 每次调用都**重建**配置(dictConfig 的 force=True),
   而不是「首次生效后短路」——既保证重复调用不叠加 handler,又保证换一份
   settings 能立刻生效(单测/验收就是靠这个注入临时 log_dir 的)。
2. ``log_dir`` 是相对路径时相对**项目根**(PROJECT_ROOT),不是当前工作目录(CWD)。
3. ``disable_existing_loggers=False``:不去 disable 已经存在的 logger,
   否则第三方库和业务模块的日志会被静默吞掉(和 YAML 前缀那个坑同类的静默失败)。
"""

from __future__ import annotations

import logging
import logging.config
import logging.handlers  # dictConfig 要靠它把字符串 class 解析成类
from pathlib import Path

# 导入Settings是纯标注依赖,给静态类型检查和 get_type_hints 用;运行时不会被求值
from project_rag.config import PROJECT_ROOT, Settings, get_settings

# 这些库默认爱刷 DEBUG/INFO,将这些库的日志等级抬到 WARNING 以上,只有error和critical会输出。
# 日志输出级别:DEBUG < INFO < WARNING < ERROR < CRITICAL,设置的等级表示最低放行标准
NOISY_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "openai",
    "grpc",
    "pymilvus",
    "chromadb",
    "transformers",
    "sentence_transformers",
    "FlagEmbedding",
    "PIL",
    "matplotlib",
    "numexpr",
    "asyncio",
)
# 设置命名空间,统一前缀
LOGGER_NAMESPACE = "project_rag"
# 日志文件名
LOG_FILE_NAME = "app.log"
# 日志文件单个最大10M,写满了将当前文件命名为app.log.1,再写入新的日志文件app.log.再写满就app.log.1变app.log.2依此类推.
# 最多5个旧日志,超过了旧删除最老的,只留5个.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

# 定义日志文件的格式长什么样子
_FILE_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "%(process)d:%(threadName)s | %(message)s"
)
# 定义控制台的格式长什么样子
_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


# 获得日志文件的绝对路径,如果是相对路径,会拼上项目根路径,这里就是只传入了logs(如果不手动传入settings的话)
def resolve_log_file(settings: Settings) -> Path:
    """把 settings.log_dir 解析成日志文件的绝对路径(相对路径按项目根算)。

    输入:配置对象
    行为:展开 ~,相对路径拼项目根,再拼日志文件名
    输出:日志文件绝对路径
    """

    # 如果路径想放在项目外使用~,会将~替换为用户目录(如果没传~的话,则这行代码无操作,直接是settings.log_dir)
    log_dir = Path(settings.log_dir).expanduser()
    # 如果是相对路径,拼上项目根路径
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    # 拼上具体的文件名字
    return log_dir / LOG_FILE_NAME


# 将settings中配置翻译成dictConfig函数需要传入的字典
# {
#     "version": 1,                      ← 必须，固定为 1
#     "disable_existing_loggers": False, ← 是否禁用已有 logger
#     "formatters": {...},               ← 每行长什么样包括控制台和文件里
#     "handlers": {...},                 ← 输出到哪
#     "loggers": {...},                  ← 按名字配置的 logger
#     "root": {...},                     ← 根 logger
#     "force": True,                     ← 是否清掉旧配置重建
# }
def build_logging_config(settings: Settings, log_file: Path | None = None) -> dict:
    """把 settings 翻译成 dictConfig 要的字典;单独抽出来便于单测。

    输入:配置对象、可选的日志文件路径
    行为:组装 formatters/handlers/root,并把噪声库抬到 WARNING
    输出:dictConfig 用的配置字典
    """
    # 这里获得日志文件的绝对路径,如果前面代码不出错,这里应该传入了
    log_file = log_file or resolve_log_file(settings)
    return {
        "version": 1,
        # 关键:别把别人(第三方库)的 logger 静默关掉
        "disable_existing_loggers": False,
        "formatters": {  # 定义格式
            "file": {"format": _FILE_FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"},
            "console": {"format": _CONSOLE_FORMAT, "datefmt": "%H:%M:%S"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",  # 使用哪个handler类
                "formatter": "console",  # 使用formatters中哪个格式
                "stream": "ext://sys.stderr",  # stream参数绑定sys.stderr对象,有等价写法
                "level": settings.console_log_level.upper(),  # 单独设置控制台日志级别
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "file",
                "filename": str(log_file),  # log_file是Path对象,这里要求输入的是字符串
                # 必须追加:切成 WARNING 重建配置时,不能把前面的日志清掉,且不存在时构建
                "mode": "a",
                "maxBytes": _MAX_BYTES,
                "backupCount": _BACKUP_COUNT,
                "encoding": "utf-8",
                "delay": True,  # 真正写第一条日志时才建文件
                "level": settings.file_log_level.upper(),
            },
        },
        "root": {
            # 根 logger 的日志级别,upper()转大写,先经过root过滤,再经过handlers过滤,也可以通过loggers单独设计级别绕过root
            "level": settings.root_log_level.upper(),
            "handlers": ["console", "file"],  # 调用顺序：日志记录先给 console再给file。
        },
        # 这里可以单独设置日志级别
        "loggers": {  # 提升这些库的日志等级为warning,别让这些库刷debug和info,字典推导式写法
            name: {"level": "WARNING", "propagate": True} for name in NOISY_LOGGERS
        },
        "force": True,  # 每次调用都清掉旧 handler 重建
    }


# 整个日志模块的入口函数,输入settings(传入的优先级高但是一般不用).
def configure_logging(settings: Settings | None = None) -> None:
    """按 settings 重建 root logger 配置;不传就用 get_settings()。

    输入:可选的配置对象
    行为:建日志目录,用 dictConfig(force=True) 重建,重复调用不叠加 handler
    输出:无
    """
    # 获取日志的配置,配置和绝对路径传入build_logging_config函数,获得配置字典
    settings = settings or get_settings()
    # 获得存放的日志文件的绝对路径
    log_file = resolve_log_file(settings)
    # 如果日志文件的父目录不存在,则创建父目录
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # 核心代码:将转化的配置字典传入dictConfig函数,进行日志的全局配置
    logging.config.dictConfig(build_logging_config(settings, log_file))


# 业务代码调用的函数,传入该日志的名字,返回logger日志对象.注:子节点如果没设置会找最近设置了的祖先节点的日志级别
def get_logger(name: str | None = None) -> logging.Logger:
    """取 project_rag 命名空间下的 logger:get_logger("ingest") -> project_rag.ingest

    输入:可选的日志名
    行为:空了用根名,已带前缀不重复拼,否则自动加 project_rag. 前缀
    输出:logger 对象
    """
    # logging模块维护的是一颗树,logger对象是树中的节点,用名字和点划分父子关系以及分支
    if not name:
        return logging.getLogger(LOGGER_NAMESPACE)
    # 如果传入的名字是根名,或者是以根名开头的,则不要反复拼前缀(也就是根名)
    if name == LOGGER_NAMESPACE or name.startswith(f"{LOGGER_NAMESPACE}."):
        return logging.getLogger(name)
    # 自动拼接前缀,这也是日常应该使用的方式
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")


if __name__ == "__main__":
    configure_logging()
    logger = get_logger()  # 没传入参数用根名,传入了用传入名(或者自动拼接根名前缀)
    logger.info("hello world")
    logger.debug("这是一段debug信息")

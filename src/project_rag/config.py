from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

# 项目根目录的路径和配置文件的路径,我这里的配置优先级：传入参数(如果传入参数) > 环境变量 > .env文件 > 配置文件 > 默认值
# 本文件位于 src/project_rag/config.py,所以 parents[0]=project_rag, [1]=src, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


# 嵌套配置类,传入yaml的子配置字典,将字典中的值转化为对应的字段类型,如果没传入的字段,则使用默认值
class LLMConfig(BaseModel):
    deepseek_api_key: SecretStr = SecretStr("")
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_s: float = 60.0

    # 验证器,这个方法会自动被调用,验证DEEPSEEK_API_KEY字段的值,如果未设置或者为空,会抛出异常
    @field_validator("deepseek_api_key")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:LLM 的密钥字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密钥。"""
        if not v.get_secret_value():
            raise ValueError("RAG_LLM__DEEPSEEK_API_KEY 未设置或为空")
        return v


class MySQLConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 10093
    user: str = "rag"
    password: SecretStr = SecretStr("")
    database: str = "rag"

    @field_validator("password")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:MySQL 密码字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密码。"""
        if not v.get_secret_value():
            raise ValueError("RAG_MYSQL__PASSWORD 未设置或为空")
        return v


class RedisConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 10087
    password: SecretStr = SecretStr("")
    db: int = 0

    @field_validator("password")
    @classmethod
    def _require_key(cls, v: SecretStr) -> SecretStr:
        """输入:Redis 密码字段。行为:校验非空,空则抛 ValueError 让启动 fail-fast。输出:原样密码。"""
        if not v.get_secret_value():
            raise ValueError("RAG_REDIS__PASSWORD 未设置或为空")
        return v


class FaqConfig(BaseModel):
    # 表名和阈值。阈值口径见 adapters/matcher_bm25.py:
    # score = top1 原始分 / 该条问自己的分,一字不差 ≈ 1.000,完全无关 ≤ 0.274,
    # 所以 0.85 表示「只答近乎一字不差的问题」,改写问法交给 RAG 通道。
    threshold: float = 0.85
    table: str = "faq"


# pydantic的写法,也是配置类的标准写法,比__init__(self.env="dev"....)强大的多,可以设置内部去读取.env和yaml文件中的配置
# 优先级是根据SettingsConfigDict写的关键字顺序定的,越靠前的优先级越高
# 关于字段名的匹配,显式传入的参数字段名必须一一对应,.env中的字段大小写不敏感,但是要对应上.__表示层级
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,  # .env文件路径 ,这是第三优先级,第一优先级是传入的参数,第二优先级是环境变量
        env_prefix="RAG_",  # 环境变量写前缀防止全局命名冲突,加前缀声明去匹配,不匹配.yaml,用于环境变量和.env的匹配
        env_nested_delimiter="__",  # 嵌套配置的分隔符,默认是.是读取环境变量的嵌套结构的时候用的
        extra="ignore",  # 忽略多传入的字段
    )

    # 这些属性当成类属性的时候统一都是默认值,当成实例的时候会使用显示传入的参数,没有传入的才使用默认值
    # 以下数据必须符合类型,否则会报错,但是可以不传入,默认值会自动生效

    # 普通配置,优先传入的,若没有传入则使用下面定义的默认配置
    env: str = "dev"  # 声明当前环境是开发环境
    root_log_level: str = "INFO"  # 日志级别默认设置为正常信息
    console_log_level: str = "INFO"
    file_log_level: str = "INFO"
    log_dir: str = "logs"  # 日志保存目录,这里指项目根目录下的logs目录

    # 嵌套配置,通过类型注解声明要把yaml中的层级,转化为LLMConfig类型的对象,自动将字典解包传入这个类中,若没有传入则使用默认值
    llm: LLMConfig = LLMConfig()
    mysql: MySQLConfig = MySQLConfig()
    redis: RedisConfig = RedisConfig()
    faq: FaqConfig = FaqConfig()

    # 这个类方法,是用来定义配置的来源和优先级顺序的
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,  # 其他都是内置的,这里是自定义的
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """输入:pydantic-settings 传入的各配置源。行为:按优先级排序,并把 yaml 排在最后。输出:配置源元组。"""
        return (  # 这里是一个元组,元组中的元素顺序就是优先级,越靠前的优先级越高
            init_settings,  # 1. 显式传入
            env_settings,  # 2. 环境变量
            dotenv_settings,  # 3. .env
            file_secret_settings,
            # 4. 优先级第四,并且给他配置文件的路径
            YamlConfigSettingsSource(settings_cls, yaml_file=CONFIG_FILE),
        )


# 这个装饰器缓存了函数的输入输出，下次调用时如果传入的输入不变,直接返回缓存的结果，而不需要再次执行函数
@lru_cache
def get_settings() -> Settings:
    """输入:无。行为:检查配置文件存在后构建 Settings,结果被 lru_cache 缓存复用。输出:全局唯一配置对象。"""
    # Settings类返回最终的配置类
    yaml_file = CONFIG_FILE
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在：{yaml_file}")
    return Settings()


__all__ = [
    "CONFIG_FILE",
    "ENV_FILE",
    "PROJECT_ROOT",
    "Settings",
    "get_settings",
]


if __name__ == "__main__":
    settings = get_settings()
    print(settings)
    print(settings.llm.base_url)
    print(settings.mysql.password)

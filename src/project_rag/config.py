from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
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
    # 思考模式(DeepSeek V4 系列支持,**一个变量同时管开关与强度**):
    #   auto         —— 不发任何参数,沿用服务端默认(开启、high)= 加这个字段之前的行为
    #   off          —— 关掉思考:实测首个内容字 **~0.5s**(思考时 2~15s)
    #   low/high/max —— 开启并指定强度(模型支持档位见 GET /models 的 effort.supported_levels)
    # 两个要知道的副作用:① 思考模式下 temperature 会被 provider **忽略**(不报错也不生效),
    # 只有 off 时才用得上;② 思考计入输出 token 计费,而且会吃掉 max_tokens 预算
    # (思考吃满时 content 为 0 帧、finish_reason=length → 产品会按「空回答」降级成拒答话术)。
    thinking: Literal["auto", "off", "low", "high", "max"] = "auto"

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
    # score = top1 原始分 / 该条问自己的分,一字不差 ≈ 1.000,
    # 完全无关 ≤ 0.303(463 条真实语料),所以 0.85 表示「只答近乎一字不差的问题」,
    # 改写问法交给知识库通道。
    threshold: float = 0.85
    table: str = "faq"


class ConversationConfig(BaseModel):
    """会话历史:表名与保留轮数。

    轮数走配置,不在读、写两处各硬编码一个 5 —— 两边用的是同一个值,改一处两处都变。
    0 或负数由读侧拒绝(见 Session)。
    """

    table: str = "conversations"
    history_rounds: int = 5


class ApiConfig(BaseModel):
    """HTTP/WebSocket 服务(``project-rag-api``)的运行参数。"""

    # 启动预热:先把嵌入/重排模型、语料索引、Milvus 连接建好,把「第一个请求的冷启动」
    # 从**第一个用户**身上挪到启动阶段(实测:不预热时首个请求比热请求多付 4.4s)。
    # 预热只走到「检索出引用」为止,**不调大模型**,所以不花 token 钱;
    # 失败只记一条 WARNING,不拦启动。关掉它 = 启动更快、但首个请求自己扛冷启动。
    warmup: bool = True


class MilvusConfig(BaseModel):
    host: str = "127.0.0.1"
    # 宿主端口:服务端在容器里,compose 把 gRPC 19530 映射到 10086
    port: int = 10086
    # 只准填已存在的库(milvus内置):Milvus 不会自动建库,命名库得先 create_database
    database: str = "default"
    collection: str = "rag_chunks"


class EmbeddingConfig(BaseModel):
    # 目录既能相对项目根,也能写绝对路径 —— 由下面的 model_path 负责解析
    model_dir: str = "models/bge-m3"
    # 先把位置定下来,免得后面改两处配置
    rerank_model_dir: str = "models/bge-reranker-v2-m3"
    device: str = "cpu"
    use_f16: bool = False

    # property装饰器,将方法调用转为属性调用
    @property
    def model_path(self) -> Path:
        """输入:无。行为:model_dir 解析成绝对路径(相对路径挂到项目根下)。输出:嵌入模型绝对路径。

        pathlib 的 / 遇到绝对右值会丢弃左值,所以 YAML 里写绝对路径也照样对。
        """
        return (PROJECT_ROOT / self.model_dir).resolve()

    @property
    def rerank_model_path(self) -> Path:
        """输入:无。行为:rerank_model_dir 解析成绝对路径。输出:重排模型绝对路径。"""
        return (PROJECT_ROOT / self.rerank_model_dir).resolve()


class RagConfig(BaseModel):
    """分块与检索参数。"""

    parent_chunk_size: int = 1200
    child_chunk_size: int = 300
    chunk_overlap: int = 50
    retrieval_k: int = 5
    candidate_m: int = 2
    # 融合方式。两路 = 进程内 BM25 关键字 + Milvus 稠密向量(不用嵌入模型的稀疏向量:
    # 它和 BM25 都是词面匹配,重叠度高,留着只会让融合多一路、评估多一维)。
    #   rrf      —— 只用排名,免标定,对两路分数量纲不敏感
    #   weighted —— 各自归一化后加权,能表达「哪一路更可信」,但要调权重
    fusion: Literal["rrf", "weighted"] = "rrf"
    # RRF 的平滑常数:分数 = Σ w / (rrf_k + rank)。越大越弱化头部优势,60 是常见默认。
    rrf_k: int = 60
    # 两路权重:rrf 模式是排名权重,weighted 模式是分数权重。
    bm25_weight: float = 0.5
    vector_weight: float = 0.5
    # 重排:两路融合后,用 CrossEncoder 对**父块正文**再精排一次。
    #   顺序:召回 → 融合 → 折叠父块 → 重排 → 截断到 candidate_m
    #   CPU 上 ≈1.1 s/条(900 字),一条 query 多花 5~10s;关掉它就退回纯融合排序。
    #   它是评估用的消融开关,不是永久配置 —— 评估集上用它比「开/关重排」的指标
    rerank: bool = True

    @model_validator(mode="after")
    def _check_sizes(self) -> RagConfig:
        """输入:无。行为:在校验之后,再加一层自定义检验参数的逻辑。输出:self。

        为什么在配置层拦:overlap >= child_size 时切分器要么抛错、要么原地打转,
        等 ingest 跑一半才发现,代价是一整轮嵌入调用。
        """
        if self.chunk_overlap >= self.child_chunk_size:
            raise ValueError(
                f"chunk_overlap({self.chunk_overlap}) 必须小于 "
                f"child_chunk_size({self.child_chunk_size})"
            )
        if self.child_chunk_size > self.parent_chunk_size:
            raise ValueError(
                f"child_chunk_size({self.child_chunk_size}) 不能大于 "
                f"parent_chunk_size({self.parent_chunk_size})"
            )
        if self.bm25_weight <= 0 and self.vector_weight <= 0:
            raise ValueError(
                "bm25_weight 与 vector_weight 不能同时为 0 —— 两路都不给权重,"
                "融合结果全是 0,排序退化成随机"
            )
        if self.rrf_k < 0:
            raise ValueError(f"rrf_k 不能为负,收到 {self.rrf_k}")
        return self


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
    milvus: MilvusConfig = MilvusConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    rag: RagConfig = RagConfig()
    conversation: ConversationConfig = ConversationConfig()
    api: ApiConfig = ApiConfig()

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
    "ApiConfig",
    "ConversationConfig",
    "EmbeddingConfig",
    "FaqConfig",
    "LLMConfig",
    "MilvusConfig",
    "MySQLConfig",
    "RagConfig",
    "RedisConfig",
    "Settings",
    "get_settings",
]


if __name__ == "__main__":
    settings = get_settings()
    print(settings)
    print(settings.llm.base_url)
    print(settings.mysql.password)

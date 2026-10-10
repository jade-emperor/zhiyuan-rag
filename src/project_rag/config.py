from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # 连接超时(秒)。原先在 faq_mysql / conversation_mysql 里各写一个 5。
    # 它决定「MySQL 挂了之后要等多久才报错」,本机 Docker 与跨网部署的合理值不同。
    connect_timeout_s: int = 5

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
    # 读/连超时(秒)。原先在 cache_redis 里写死 1.0。
    # **它就是「降级放大」的时长**:Redis 一挂,_safe_get 要等这么久才按未命中返回,
    # 期间请求转去调大模型(见 docs/deferred.md §1.1)。调大 = 故障时每个请求多等一会儿。
    socket_timeout_s: float = 1.0

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


class CacheConfig(BaseModel):
    """FAQ 答案缓存的旁路参数(``answer_faq`` 的 cache-aside)。

    **为什么 TTL 走配置**:它和 ``faq.threshold`` 是同一类东西 —— 业务旋钮,不是机制。
    留在代码里意味着「把 5 分钟改成 15 分钟要重新部署」。

    **为什么不把缓存键前缀也搬进来**:``answer_faq.CACHE_KEY_PREFIX = "faq:v1:"`` 里的
    版本号跟着**缓存 value 的格式**(``_encode`` 的 ``<source>\\x1f<answer>``)走。让它可配
    = 允许两个格式不同的部署共用一份 Redis,读到对方的值会**静默解错**。
    版本号是机制,留在代码里,改 ``_encode`` 时手工 bump。

    **作用域提醒**:该缓存当前只服务 ``faq`` 单通道命令 —— 合流(``qa`` / FastAPI)
    不读也不写缓存,见 ``docs/deferred.md`` #21。
    """

    # 过期秒数。落配置之前的字面量就是 300,所以这里必须也是 300 ——
    # 加配置项不许悄悄改变默认语义(有验收脚本钉住类默认值)。
    ttl_s: int = 300

    # BM25 索引「语料指纹」的缓存秒数（加)。
    # 为什么值得一个旋钮:指纹查询在 3938 块时实测 **397.6ms**,而它**每次检索都要调**;
    # 真正的 BM25 打分只要 ~1ms。缓存后每查询变成一次 Redis GET(~0.1ms),
    # 代价是「语料变了」最多晚这么多秒被发现。
    # 为什么默认 20 而不是跟 ttl_s 一样 300:这个窗口决定"新入库的文档多久能被检索到",
    # 5 分钟太久(入库本身才几分钟);而 20 秒对体感无影响。
    # 要立刻生效得让写入方 bump 缓存键,见 docs/deferred.md 第 56 条。
    corpus_fp_ttl_s: int = 20


class ConversationConfig(BaseModel):
    """会话历史:表名与保留轮数。

    轮数走配置,不在读、写两处各硬编码一个 5 —— 两边用的是同一个值,改一处两处都变。
    0 或负数由读侧拒绝(见 Session)。
    """

    table: str = "conversations"
    history_rounds: int = 5


class RateLimitConfig(BaseModel):
    """限流(滑动窗口,默认计数在 Redis;见 ``adapters/ratelimit_redis.py``)。

    **为什么默认开着**:关着的时候,一个循环脚本就能把模型与 token 额度吃光,
    而这件事在日志里长得跟"今天大家都很活跃"一模一样。默认值按"同事几人到几十人"定:
    一分钟 60 次问答够任何人用,但挡住脚本。

    每一档 ``<= 0`` = 关掉这一档(不是"限 0 次")。
    """

    enabled: bool = True
    # 计数存在哪:`redis`(跨进程共享、重启不清零、时钟以 Redis 为准)/ `memory`(单进程)。
    # 默认 Redis —— 限流的前提是"换个进程/重启一下也绕不过去"。
    backend: str = "redis"
    # 键的命名空间 + 版本号(同一个 Redis 被别的应用或别的版本共用时不互相踩)。
    key_prefix: str = "ratelimit:v1:"
    window_s: int = 60
    # 问答:HTTP 的 /query 与 WS 的**每条消息**共用这一档(两条入口,一份额度)
    query: int = 60
    # 上传:一次上传背后是几分钟的 CPU 嵌入,闸门要更紧
    upload: int = 10
    # 登录:防口令爆破
    login: int = 10


class ApiConfig(BaseModel):
    """HTTP/WebSocket 服务(``project-rag-api``)的运行参数。"""

    # 启动预热:先把嵌入/重排模型、语料索引、Milvus 连接建好,把「第一个请求的冷启动」
    # 从**第一个用户**身上挪到启动阶段(实测:不预热时首个请求比热请求多付 4.4s)。
    # 预热只走到「检索出引用」为止,**不调大模型**,所以不花 token 钱;
    # 失败只记一条 WARNING,不拦启动。关掉它 = 启动更快、但首个请求自己扛冷启动。
    warmup: bool = True
    # 上传的原文件落盘目录(相对**项目根**,与 models/data 同一套解析方式)。
    # 落盘而不是只进向量库:解析要读原文件、前端要能"预览",而且删文档时三处痕迹要一起清。
    uploads_dir: str = "data/uploads"
    # 单个上传文件的大小上限(MB)。**不是安全边界,是一道礼貌的闸**:
    # 真正防滥用要靠反代/限流。定 50 是因为语料里最大的 PDF 也就几 MB。
    max_upload_mb: int = 50
    # 绑定地址。类默认**必须**留着 0.0.0.0:WSL 里绑回环时 Windows 浏览器连不上(实测),
    # 进容器后绑回环也映射不出去。改成可配是为了「本机调试只监听自己」这类场景。
    host: str = "0.0.0.0"
    port: int = 8080
    # 预热用的那条查询。**它是隐藏耦合**:预热要真的检索到东西,才能把
    # 「构造嵌入/重排/向量库/BM25」那 4.4s 付掉;语料换成别的领域后,
    # 这条查询可能检索不到任何块 —— 换成该领域里必然存在的一句即可。
    warmup_query: str = "装饰器有什么用"
    # CORS 允许的来源。默认全开是**本机开发**的选择;对外必须收紧,
    # 否则任何站点都能拿用户的浏览器打这个服务。
    # "全开 + 绑非回环地址"这个组合会在启动时**打一条 WARNING**(见 api/app.py 的
    # `_warn_wide_cors`):不是拦你,是让这件事在日志里留个痕。
    cors_origins: list[str] = ["*"]
    # 限流。**它同时是"防滥用"与"防爆破"**:问答烧 token、上传烧 CPU、登录烧口令。
    rate_limit: RateLimitConfig = RateLimitConfig()


class MilvusConfig(BaseModel):
    host: str = "127.0.0.1"
    # 宿主端口:服务端在容器里,compose 把 gRPC 19530 映射到 10086
    port: int = 10086
    # 只准填已存在的库(milvus内置):Milvus 不会自动建库,命名库得先 create_database
    database: str = "default"
    collection: str = "rag_chunks"
    # 读一致性档（Milvus 的 consistency_level）。**只作用于 search 那一路**；
    # 指纹（变更检测）与写后回查总数固定用 strong —— 那两处要的是"看到最新"，且不在热路径上
    # （见 adapters/milvus_store.py 里两个常量的分工）。
    #
 # 为什么要有这个旋钮：实测（本机 3927 块，各 30 次采样）
    #   strong  → 中位 **399ms**（22/30 落在 350–403ms）
    #   bounded → 中位 **2ms**
    # 同一份数据、同一个 FLAT 索引，只换这一档就是 **200×**。那 400ms 不是计算，
    # 是等"保证时间戳"对齐的同步往返（与 k、与取哪些字段都无关：k=1 取大字段同样 2ms）。
    # 代价：刚写完的一小段窗口（亚秒级）内，search 可能看不到新块。
    # ⚠️ 灌完立刻可见这件事**另有兜底**：ingest 的每一批写完都会做一次 strong 的 `_count()`
    # 回查（upsert 的返回值），那次强读会把可见性推上去，用户界面上还要点几下才会来搜。
    #
    # 类默认保持 "strong"：它是这段代码一直以来的行为，默认值一改语义就变
    # （有契约钉着，见 check_m1_1_config）。要那 400ms 就在 config.yaml 里写 bounded。
    read_consistency: Literal["bounded", "eventually", "strong"] = "strong"


class EmbeddingConfig(BaseModel):
    # 目录既能相对项目根,也能写绝对路径 —— 由下面的 model_path 负责解析
    model_dir: str = "models/bge-m3"
    # 先把位置定下来,免得后面改两处配置
    rerank_model_dir: str = "models/bge-reranker-v2-m3"
    device: str = "cpu"
    # 嵌入模型是否用 fp16。**与重排分开**：两者的收益与精度风险不是一回事。
    use_f16: bool = False
 # 重排模型是否用 fp16。实测（RTX 5080 Laptop，9 条 1104 字父块）
    #   fp32 398ms → fp16 101ms（**3.9×**）；嵌入侧 fp16 在批量 32 时 2.17 → 0.73ms/条（3×）。
    # 精度影响两边都要用评估集验（`data/eval/eval_set_bagu_pdf_v1.json` 那 50 条），
    # 所以默认 false —— **不改现有行为**，要开自己开，开完拿评估集说话。
    # 只在 CUDA 上生效（CPU 上 fp16 更慢，适配器里判 device）。
    rerank_use_f16: bool = False
    # 嵌入实现:**默认必须是真语义模型**。``hashing`` 只为演示与验收存在 ——
    # 它是字符 bigram 哈希(零依赖、零模型、确定性),只保证「文本越像、向量越近」,
    # **不保证语义相近**。默认值一旦被改成 hashing,没有模型的新机器会「看起来一切正常、
    # 检索全是噪声」,而这种坏法**不报错** —— 所以有条契约专门钉默认值(见 check_m3_rag)。
    # 演示用法(不必下载 4.4G 模型):`RAG_EMBEDDING__BACKEND=hashing`
    backend: Literal["bge_m3", "hashing"] = "bge_m3"

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
    # ingest 的嵌入批大小。**不是切分参数**,是「一次喂几条给嵌入模型」——
    # 旋钮而非机制:CPU 上批量越大越划算(padding 摊销 + 矩阵乘吃满),
    # 显存小的机器反而要调小。原先在 ingest_documents 与 ingest --batch-size 各写一个 64。
    ingest_batch_size: int = 64
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
        if self.ingest_batch_size <= 0:
            raise ValueError(
                f"ingest_batch_size 必须为正,收到 {self.ingest_batch_size}"
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
    # 日志轮转。原先在 observability/logging_setup.py 里写死 10MB × 5 份 ——
    # 而 log_dir 早就是配置了,同一件事的另外两个参数却没有,属于不一致。
    log_max_bytes: int = 10 * 1024 * 1024  # 单个日志文件上限(写满就滚成 .1)
    log_backup_count: int = 5  # 保留几份旧日志(0 = 不保留)

    # 嵌套配置,通过类型注解声明要把yaml中的层级,转化为LLMConfig类型的对象,自动将字典解包传入这个类中,若没有传入则使用默认值
    llm: LLMConfig = LLMConfig()
    mysql: MySQLConfig = MySQLConfig()
    redis: RedisConfig = RedisConfig()
    faq: FaqConfig = FaqConfig()
    cache: CacheConfig = CacheConfig()
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
    "CacheConfig",
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

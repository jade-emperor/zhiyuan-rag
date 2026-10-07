"""
限流的 **Redis** 实现:滑动窗口日志 + **一段 Lua 脚本**。

**为什么是 Redis**(对比进程内存那一份,三条):

1. **重启不清零** —— 内存版重启即reset,"重启一下就能再多打一百次"是真实存在的;
2. **多 worker / 多副本共享一份计数** —— 内存版每个进程各算各的,限流形同虚设
   (uvicorn 现在只起一个 worker,但那是"当前部署",不是"限流的前提");
3. **时钟只有一个** —— 脚本里取 **Redis 服务端时间**(``TIME``),
   多台机器时钟不同步也不会各算各的窗口。

**为什么必须是一段 Lua**(而不是 ``ZCARD`` 再 ``ZADD``):那两步之间能插进另一个请求 ——
两个并发请求会同时读到"还没满"、同时放行。限流最不该错的就是这里。
脚本里"裁剪 → 计数 → 判定 → 写入 → 设 TTL"是**一次往返、原子**的;
``check_m7_4_security.py`` 里那条并发用例(20 个线程抢 5 个名额,恰好 5 个通过)
就是冲这个来的。

**故障口径:降级放行(fail-open)**。Redis 挂了,请求照过,但:

* 打一条 WARNING(同一进程**每个窗口最多一条**,否则故障期间日志会被刷爆);
* ``degraded`` 计数加一 —— 排障时"限流到底有没有生效"要看得见。

为什么不 fail-closed(挂就 503):限流是**保护**,不是正确性。为了一道闸把整个问答
停掉,是把"可能被刷"换成"一定不可用" —— 与 P0-1 那条缓存降级同一个判断
(见 ``adapters/cache_redis.py`` 的模块 docstring)。代价照实说:Redis 挂着的这段时间,
**爆破与刷量是不设防的**,所以它必须有一条日志痕,而不是悄悄过去。
"""

from __future__ import annotations

import logging
import time

import redis

from project_rag.config import RedisConfig
from project_rag.ports import RateLimiter

_logger = logging.getLogger("project_rag.adapters.ratelimit_redis")

# 滑动窗口日志:成员用 `时间戳:随机后缀` 保证唯一(同一毫秒的多条命中不能互相顶掉),
# 分数是**Redis 服务端时间**(秒,带小数)。返回 0 = 放行;否则返回建议等待的秒数。
#
# 注意 `TIME` 返回 [秒, 微秒] 两个字符串,算术要用 tonumber 转。
_SLIDING_WINDOW = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])

local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
local cutoff = now - window

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = math.ceil(window - (now - tonumber(oldest[2])))
  if retry < 1 then retry = 1 end
  return retry
end

redis.call('ZADD', key, now, now .. ':' .. tostring(math.random(1000000)))
-- TTL 兜底:键不会永远留着(窗口过后自然过期,不必谁来清理)
redis.call('PEXPIRE', key, math.ceil(window * 1000))
return 0
"""


class RedisRateLimiter(RateLimiter):
    """跨进程共享的限流计数(滑动窗口日志,原子)。"""

    def __init__(self, config: RedisConfig, *, key_prefix: str = "ratelimit:v1:") -> None:
        """输入:Redis 配置、键前缀。行为:建客户端并注册脚本,此时不连库。输出:无。

        前缀是**命名空间 + 版本号**(与 FAQ 缓存 ``faq:v1:`` 同一套做法):
        同一个 Redis 被别的应用/别的版本共用时,键不会互相踩。
        """
        self._prefix = key_prefix
        self.degraded = 0  # 降级放行的次数(排障要看)
        self._last_warning = 0.0
        self._client = redis.Redis(
            host=config.host,
            port=config.port,
            password=config.password.get_secret_value(),
            db=config.db,
            socket_timeout=config.socket_timeout_s,
            socket_connect_timeout=config.socket_timeout_s,
            decode_responses=True,
        )
        # 注册一次,之后按 sha 调用(省得每次都把脚本正文发过去)
        self._script = self._client.register_script(_SLIDING_WINDOW)

    def check(self, key: str, *, limit: int, window_s: float) -> int:
        """输入:键、上限、窗口秒数。行为:一段 Lua 原子地裁决。输出:0 或等待秒数。

        ``limit <= 0`` 直接放行(明确关掉这一档,连 Redis 都不问);
        Redis 故障 → **降级放行** + 限速告警 + ``degraded`` 计数。
        """
        if limit <= 0:
            return 0
        if window_s <= 0:
            raise ValueError(f"window_s 必须为正,收到 {window_s}")
        try:
            # redis-py 的存根把脚本返回值标成 `Awaitable[str] | str`(同一个类也服务异步客户端);
            # 我们建的是同步 `redis.Redis`,运行时永远是 str —— 所以就地标一下。
            result = self._script(keys=[self._prefix + key], args=[limit, window_s])
        except redis.RedisError as exc:
            return self._degrade(exc, window_s)
        return int(str(result))

    def forget(self, key: str) -> None:
        """输入:键。行为:删掉它(登录成功时用)。输出:无。

        删除失败**不降级成"假装删了"**:记一条告警、返回 —— 调用方是登录成功那条路,
        不该因为 Redis 抖一下就让用户登不进来。
        """
        try:
            self._client.delete(self._prefix + key)
        except redis.RedisError as exc:
            _logger.warning("限流计数清不掉(Redis 故障,登录继续):%s", exc)

    def _degrade(self, exc: Exception, window_s: float) -> int:
        """输入:异常、窗口秒数。行为:计数 + **限速**告警(每个窗口最多一条)。输出:0(放行)。"""
        self.degraded += 1
        now = time.monotonic()
        if now - self._last_warning >= window_s:
            self._last_warning = now
            _logger.warning(
                "Redis 不可用,限流降级为放行(第 %d 次):%s;"
                "这段时间爆破与刷量不设防,恢复后自动生效",
                self.degraded,
                exc,
            )
        return 0

"""
Redis 适配器:旁路缓存的读/写两侧。

与 MySQL 相反,这里任何故障都不该让问答失败 —— 缓存是纯加速。
所以本文件只负责「把错误抛出去」,降级由调用方决定:
用例层吞掉并记 warning(调用期),cli 装配期吞掉并当作无缓存。
"""

from __future__ import annotations

import redis

# 只用于类型注解
from project_rag.config import RedisConfig
from project_rag.ports import Cache

_SOCKET_TIMEOUT = 1.0


class RedisCache(Cache):
    """Redis缓存功能,如果连接失败,会在第一次使用时暴露"""

    def __init__(self, config: RedisConfig) -> None:
        self._client = redis.Redis(
            host=config.host,
            port=config.port,
            password=config.password.get_secret_value(),  # 获取密码的明文
            socket_timeout=_SOCKET_TIMEOUT,
            db=config.db,
            decode_responses=True,  # 为了让get()返回字符串,而不是字节
            socket_connect_timeout=_SOCKET_TIMEOUT,
        )

    def get(self, key: str):
        """必须返回字符串,否则当没返回"""
        value = self._client.get(key)
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str, ttl: int):
        self._client.set(key, value, ex=ttl)  # ex=过期时间(time to live)


if __name__ == "__main__":
    from project_rag.config import get_settings

    config = get_settings().redis
    cache = RedisCache(config)
    cache.set("演示", "在订单页点「申请退款」。", 60)
    print("读回:", cache.get("演示"))
    print("不存在的 key:", cache.get("1234567890"))

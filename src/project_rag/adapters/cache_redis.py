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


class RedisCache(Cache):
    """
    继承端口,实现缓存功能
    Redis缓存功能,如果连接失败,会在第一次使用时暴露
    """

    def __init__(self, config: RedisConfig) -> None:
        """输入:Redis 配置。行为:建客户端并设超时,此时不连库。输出:无。"""
        self._client = redis.Redis(
            host=config.host,
            port=config.port,
            password=config.password.get_secret_value(),  # 获取密码的明文
            # 超时走配置:它决定 Redis 故障时每个请求要等多久才降级
            socket_timeout=config.socket_timeout_s,
            db=config.db,
            decode_responses=True,  # 为了让get()返回字符串,而不是字节
            socket_connect_timeout=config.socket_timeout_s,
        )

    def get(self, key: str) -> str | None:
        """输入:缓存键。行为:读 Redis,非字符串按未命中处理。输出:字符串值或 None。

        必须返回字符串,否则当没返回
        """
        value = self._client.get(key)
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str, ttl: int) -> None:
        """输入:键、值、过期秒数。行为:写入 Redis 并按 ttl 过期。输出:无。"""
        self._client.set(key, value, ex=ttl)  # ex=过期时间(time to live)


if __name__ == "__main__":
    from project_rag.config import get_settings

    config = get_settings().redis
    cache = RedisCache(config)
    cache.set("演示", "在订单页点「申请退款」。", 60)
    print("读回:", cache.get("演示"))
    print("不存在的 key:", cache.get("1234567890"))

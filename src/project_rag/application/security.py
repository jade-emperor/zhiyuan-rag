"""
认证策略:密码哈希与令牌生成(**标准库实现,可离线测**)。

**为什么不用 passlib / bcrypt**:它们是第三方包,而这个项目里"第三方的活"必须落在
``adapters/``,于是"密码怎么算"就跟着存储走了 —— 换数据库时会顺手换掉哈希算法,
而那是最不该被顺手改的东西。``hashlib.pbkdf2_hmac`` 是标准库、够用、参数可钉死、
而且能在这台没有 MySQL 的机器上把口径测全。

**哈希串自带参数**(``pbkdf2_sha256$迭代次数$salt$hash``,Django 同款):
换个迭代次数时**老密码仍能验证**(参数从串里读),不必强制所有人重设密码。
升级路径是"验证成功时顺手用新参数重算",不是"一刀切失效"。

**令牌存哈希不存原文**:库被读走时拿到的不是能直接用的凭证。
令牌是随机 32 字节,不需要慢哈希(它没有"弱口令"问题),所以用 sha256 就够 ——
给令牌上 pbkdf2 只会让每个请求多花几十毫秒,换不来任何东西。

**时间比较一律用 ``hmac.compare_digest``**:``==`` 会因提前返回而泄漏"前几个字符对了",
对密码哈希与令牌这类东西,那是可利用的信息。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 210_000  # OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议下限
_SALT_BYTES = 16
TOKEN_BYTES = 32
# 令牌默认有效期(秒)。12 小时 = 一个工作日,过期后重新登录。
TOKEN_TTL_SECONDS = 12 * 3600


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """输入:明文密码。行为:加随机盐做 PBKDF2。输出:``算法$迭代$盐$哈希``(盐与哈希是十六进制)。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """输入:明文密码 + 存下来的哈希串。行为:按串里的参数重算并**恒定时**比较。输出:是否匹配。

    任何解析不了的串都返回 False(**不抛异常**):一个坏行不该让整个登录接口 500 ——
    但调用方要清楚,返回 False 与"这个人不存在"在外部看来是同一件事(刻意的,防用户名枚举)。
    """
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations_text)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(expected, actual)


def new_token() -> str:
    """输入:无。行为:生成一个 URL 安全的随机令牌。输出:令牌原文(**只在签发那一刻存在**)。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """输入:令牌原文。行为:算 sha256 摘要(进库的是它)。输出:64 位十六进制。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_user_id() -> str:
    """输入:无。行为:生成用户 ID。输出:32 位十六进制。"""
    return secrets.token_hex(16)


def bearer_token(header: str | None) -> str | None:
    """输入:``Authorization`` 头的原文。行为:取出 ``Bearer`` 后面的令牌。输出:令牌;格式不对给 None。

    只认 ``Bearer``(不认裸令牌、不认 query 参数):两种传法并存时,
    "谁能读到令牌"的边界就说不清了(URL 会进日志、进浏览器历史)。
    WebSocket 是例外 —— 浏览器没法给 WS 加自定义头,那里必须走 query 参数,
    所以那条路径单独在 API 层处理并**只用于 WS**。
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()

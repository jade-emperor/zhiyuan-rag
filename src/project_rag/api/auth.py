"""
API 层的认证与授权:把「认证存储」包成 FastAPI 依赖 + WebSocket 校验器。

**为什么单独一个模块而不是塞进 app.py**:授权判断是最该被逐条读的东西
(它错了就是越权),混在 500 行的路由文件里没人读得完。这里只有四个入口:
登录、取当前用户、要求管理员、检查会话归属。

**一条最重要的口径:``auth_store is None`` = 无认证模式**。
那个状态**只可能来自显式注入**(``create_app(Deps())`` 或验收脚本),
``build_real_deps()`` 永远把认证接上 —— 所以"生产路径上没有无认证这一档"。
反过来,假如把"连不上库"也降级成无认证,那就是把一次数据库抖动变成一次**全网放行**,
那是最坏的一种失败方式;所以这里**连不上就 503**,不放行。

**会话归属的三种情况**(``check_owner``):

* 没配认证 → 放行(注入式测试);
* 这个会话**没有主人**(从没绑过) → **拒绝**。不能放行:``owner_of`` 返回 None 有两种可能 ——
  "加认证之前建的老会话"和"别人瞎编的一个 ID",两者都**不该**让人读到内容;
* 有主人但不是当前用户 → 拒绝(403)。
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from project_rag.application.security import (
    TOKEN_TTL_SECONDS,
    bearer_token,
    new_token,
    token_fingerprint,
    verify_password,
)
from project_rag.domain.auth import User
from project_rag.ports import AuthStore


class Authenticator:
    """一次装配、处处复用:HTTP 走依赖,WebSocket 走 ``user_for_ws_token``。"""

    def __init__(self, store: AuthStore | None) -> None:
        """输入:认证存储(``None`` = 无认证模式)。行为:记下。输出:无。"""
        self._store = store

    @property
    def enabled(self) -> bool:
        """输入:无。行为:判断是否开了认证。输出:布尔。"""
        return self._store is not None

    def _require_store(self) -> AuthStore:
        """输入:无。行为:拿存储,没配就 503。输出:存储。"""
        if self._store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="没有接上认证存储(auth_store_factory 为空)",
            )
        return self._store

    async def login(self, username: str, password: str) -> tuple[str, User]:
        """输入:用户名、明文密码。行为:验证并签发令牌。输出:``(令牌原文, 用户)``。

        **失败一律同一句话**:不区分"没这个人"和"密码不对" —— 区分开就等于
        提供了一个用户名枚举接口。**没这个用户时也走一次哈希验证**(用一个固定的假哈希),
        否则响应时间会把答案说出来(存在→慢、不存在→快)。
        """
        store = self._require_store()
        found = await run_in_threadpool(store.find_by_username, username)
        if found is None:
            # 时间侧信道:与"密码不对"那条路花一样多的 CPU
            verify_password(password, "pbkdf2_sha256$210000$00$00")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码不对"
            )
        user, password_hash = found
        if not verify_password(password, password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码不对"
            )
        token = new_token()
        await run_in_threadpool(
            store.issue_token, user, token_fingerprint(token), TOKEN_TTL_SECONDS
        )
        return token, user

    async def user(self, authorization: str | None = Header(default=None)) -> User | None:
        """输入:``Authorization`` 头(FastAPI 注入)。行为:换出当前用户。输出:用户;无认证模式给 None。

        401 的两种情形(缺令牌 / 令牌无效)分开写 detail —— 那是给**调用方**排障用的,
        不涉及账号是否存在。
        """
        if self._store is None:
            return None
        token = bearer_token(authorization)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少 Bearer 令牌(Authorization: Bearer <token>)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await run_in_threadpool(self._store.user_for_token, token_fingerprint(token))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="令牌无效或已过期,请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    async def logout(self, authorization: str | None) -> bool:
        """输入:``Authorization`` 头。行为:把这枚令牌从库里撤掉。输出:是否撤掉了。

        **为什么要有这一步**:前端"退出"原来只是把令牌从 localStorage 删掉 ——
        服务端那枚令牌还**照样有效到 TTL 结束**(默认 12 小时)。谁要是之前把它拷走了,
        "退出"对他没有任何作用。撤销才是真的退出。

        没带令牌 / 令牌本来就无效时返回 ``False`` 而不是报错:退出是**幂等**的,
        重复点不该报错(与删文档同一条口径)。
        """
        store = self._require_store()
        token = bearer_token(authorization)
        if token is None:
            return False
        before = await run_in_threadpool(self._store_exists, token)
        await run_in_threadpool(store.revoke_token, token_fingerprint(token))
        return before

    def _store_exists(self, token: str) -> bool:
        """输入:令牌原文。行为:看它现在是否还能换出用户。输出:布尔(给 logout 的返回值用)。"""
        store = self._store
        if store is None:
            return False
        return store.user_for_token(token_fingerprint(token)) is not None

    async def user_for_ws_token(self, token: str | None) -> User | None:
        """输入:WebSocket 的 ``token`` query 参数。行为:换出当前用户。输出:用户;失败给 None。

        **为什么 WebSocket 走 query 而不是头**:浏览器的 ``WebSocket`` 构造函数
        **不能设自定义头**。这是唯一一条必须把令牌放进 URL 的路径 —— 代价是它会进
        服务器日志,所以 WS 的令牌有效期与 HTTP 共用一份,登出即失效。
        """
        if self._store is None:
            return None
        if not token:
            return None
        return await run_in_threadpool(self._store.user_for_token, token_fingerprint(token))

    async def bind_session(self, session_id: str, user: User | None) -> None:
        """输入:会话 ID、当前用户。行为:记下归属(无认证模式什么都不做)。输出:无。"""
        if self._store is None or user is None:
            return
        await run_in_threadpool(self._store.bind_session, session_id, user.user_id)

    async def owner_violation(self, session_id: str | None, user: User | None) -> str | None:
        """输入:会话 ID、当前用户。行为:判断这次访问是不是越权。输出:原因串;放行给 None。

        **HTTP 与 WebSocket 共用这一份**:HTTP 要的是 403,WS 要的是"回一帧 error 但别断连接",
        两种出口、一套判断。分两份写的话,迟早有一边忘了某个分支 —— 而那是越权。

        没给 ``session_id`` 时放行 —— 那是一次**新**对话,还没有"谁的"这个问题。
        """
        if self._store is None or session_id is None:
            return None
        if user is None:  # 认证开着却没有用户 = 调用方漏加 Depends,按最严处理
            return "未认证"
        owner = await run_in_threadpool(self._store.owner_of, session_id)
        if owner is None:
            return "这段会话没有归属记录(可能是加认证之前建的,或会话 ID 不存在)"
        if owner != user.user_id:
            return "这段会话不属于当前账号"
        return None

    async def check_owner(self, session_id: str | None, user: User | None) -> None:
        """输入:会话 ID、当前用户。行为:越权就 403。输出:无(放行)。

        判断逻辑全在 ``owner_violation``,这里只负责把它变成 HTTP 的 403。
        """
        detail = await self.owner_violation(session_id, user)
        if detail is not None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    @staticmethod
    def require_admin(user: User | None) -> User | None:
        """输入:当前用户。行为:不是管理员就 403。输出:该用户(便于链式调用)。

        无认证模式放行(``user is None``):那是注入式测试,权限由注入方负责。
        """
        if user is not None and not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="这个操作需要管理员角色"
            )
        return user

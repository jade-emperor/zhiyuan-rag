#!/usr/bin/env python
"""的反向测试：把安全收口改回坏样子，确认检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m7_4_security.py

**为什么专门有一份**：`check_m7_4_security.py` 里有一半是**结构断言**（路由白名单扫描、
pyproject 解析、源码字符串），而结构断言的老毛病是它可能永远为真。这里每条都真的把代码
改坏一次，再看那一行变没变红（"没红过的检查 = 没有检查"）。

十条各自的坏法，共同点是**都不会报错**：

* RT-1 干脆不限流（`_enforce_rate_limit` 永远放行）;
* RT-2 WS 那条路不过闸（HTTP 限了、WS 没限 —— 换个入口就绕过去了）;
* RT-3 登录成功不清计数（输错几次的人被自己牵连一整分钟）;
* RT-4 登出只在本地清（服务端那枚令牌照样活到过期 —— 拷走它就等于没退）;
* RT-5 `/api/v1/sources` 又变成裸的（业务数据不需要令牌）;
* RT-6 去掉 CSP（注入一段 script 就能读走 localStorage 里的令牌）;
* RT-7 CORS 带上凭证（通配来源 + 带凭证 = 任意站点带着用户身份打这个服务）;
* RT-8 桌面壳只看 index.html 在不在（产物是旧的、assets 名字换了 → 安静的空白窗口）;
* RT-9 `?backend=` 认完不抹掉（地址留在地址栏里,截个屏就漏了）;
* RT-10 pywebview 挪进主依赖（GUI 栈跟着进镜像）;
* RT-11 限流计数不设 TTL（键永远留在 Redis 里）;
* RT-12 Lua 里不裁剪过期成员（窗口滑过去还是被拒）;
* RT-13 计算键不带命名空间前缀（与别的应用互相踩）;
* RT-14 `forget` 不删键（登录成功后额度没恢复）;
* RT-15 Redis 故障时 fail-closed（一道闸把整个问答停掉）;
* RT-16 把限流偷偷换回进程内存（重启清零、多 worker 各算各的 —— 换 Redis 就是为了这个）;
* RT-17 问答超限改回硬 429（把一句「稍后再试」做成错误码）;
* RT-18 兜底话术里不写等待秒数（用户只能靠反复重试去猜）;
* RT-19 被限流那一轮也写进历史;
* RT-20 WS 超限回 `error` 帧（而不是话术的 token+done 形制）;
* RT-21 前端把限流那条画成红字错误。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "project_rag" / "api" / "app.py"
AUTH = ROOT / "src" / "project_rag" / "api" / "auth.py"
DESKTOP = ROOT / "src" / "project_rag" / "desktop.py"
REDIS_LIMITER = ROOT / "src" / "project_rag" / "adapters" / "ratelimit_redis.py"
DEPS = ROOT / "src" / "project_rag" / "cli" / "deps.py"
PYPROJECT = ROOT / "pyproject.toml"
CLIENT_TS = ROOT / "web" / "src" / "api" / "client.ts"
MESSAGES = ROOT / "web" / "src" / "components" / "MessageList.vue"
SCRIPT = "check_m7_4_security.py"


@dataclass(frozen=True, slots=True)
class Case:
    """一个坏版本:改哪个文件、把哪段换成哪段、哪条检查必须因此变红。"""

    title: str
    target: Path
    old: str
    new: str
    needle: str  # 期望红的检查名片段(必须唯一命中)


CASES: tuple[Case, ...] = (
    Case(
        title="RT-1  干脆不限流（判定那一支永远不进）",
        target=APP,
        old="""    if limiter is None:
        return 0
    return limiter.check(f"{bucket}:{key}", limit=limit, window_s=window_s)""",
        new="""    return 0  # 反向测试:不限流""",
        needle="问答超限回的是**兜底话术**",
    ),
    Case(
        title="RT-2  WS 那条路不过闸（换个入口就绕过去了）",
        target=APP,
        old="""                retry_after = _rate_limit_check(
                    limiter,
                    "query",
                    _rate_limit_key(websocket, ws_user),
                    settings.api.rate_limit.query,
                    float(settings.api.rate_limit.window_s),
                )""",
        new="""                retry_after = 0  # 反向测试:WS 不过闸""",
        needle="HTTP 与 WS 共用同一份额度",
    ),
    Case(
        title="RT-3  登录成功不清计数（输错几次的人被自己牵连一整分钟）",
        target=APP,
        old="""        limiter.forget(f"login:ip:{caller}")""",
        new="""        pass  # 反向测试:不清失败计数""",
        needle="登录限流：连错会被挡住",
    ),
    Case(
        title="RT-4  登出只在本地清（服务端令牌照样活到过期）",
        target=AUTH,
        old="""        before = await run_in_threadpool(self._store_exists, token)
        await run_in_threadpool(store.revoke_token, token_fingerprint(token))
        return before""",
        new="""        return True  # 反向测试:不真的撤销""",
        needle="登出：撤销之后同一枚令牌立刻 401",
    ),
    Case(
        title="RT-5  /api/v1/sources 又变成裸的（业务数据不要令牌）",
        target=APP,
        old='    async def sources(user: User | None = Depends(auth.user)) -> SourcesOut:',
        new="    async def sources() -> SourcesOut:",
        needle="路由守卫：不要求认证的路由",
    ),
    Case(
        title="RT-6  去掉 CSP（注入一段 script 就能读走令牌）",
        target=APP,
        old='        if request.url.path.startswith("/static/app"):',
        new="        if False:  # 反向测试:不加安全头",
        needle="CSP：前端产物带 script-src",
    ),
    Case(
        title="RT-7  CORS 带上凭证（通配来源 + 带凭证）",
        target=APP,
        old="        allow_credentials=False,",
        new="        allow_credentials=True,",
        needle="CORS：明确不带凭证",
    ),
    Case(
        title="RT-8  桌面壳只看 index.html 在不在（产物是旧的 → 安静的白窗口）",
        target=DESKTOP,
        old="""    if missing:
        return [
            f"产物引用的资源不见了:{', '.join(missing)}",
            "重新构建:`cd web && npm run build`",
        ]""",
        new="""    if False:  # 反向测试:不查引用的资源
        pass""",
        needle="桌面壳：产物缺失或引用资源不见了",
    ),
    Case(
        title="RT-9  ?backend= 认完不抹掉（地址留在地址栏里）",
        target=CLIENT_TS,
        old="  params.delete('backend')",
        new="  void params.get('backend')  // 反向测试:不抹掉",
        needle="前端：?backend= 只认一次并抹掉",
    ),
    Case(
        title="RT-10  pywebview 挪进主依赖（GUI 栈跟着进镜像）",
        target=PYPROJECT,
        old='''desktop = [
    "pywebview>=5.0",
]''',
        new="desktop = []",
        needle="桌面壳：入口与可选依赖都写进了 pyproject",
    ),
    Case(
        title="RT-11  限流计数不设 TTL（键永远留在 Redis 里）",
        target=REDIS_LIMITER,
        old="redis.call('PEXPIRE', key, math.ceil(window * 1000))",
        new="-- 反向测试:不设 TTL",
        needle="限流语义（Redis 那份）",
    ),
    Case(
        title="RT-12  Lua 里不裁剪过期成员（窗口滑过去之后还是被拒）",
        target=REDIS_LIMITER,
        old="redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)",
        new="-- 反向测试:不裁剪过期成员",
        needle="限流语义（Redis 那份）",
    ),
    Case(
        title="RT-13  计算键不带命名空间前缀（与别的应用/版本互相踩）",
        target=REDIS_LIMITER,
        old='            result = self._script(keys=[self._prefix + key], args=[limit, window_s])',
        new='            result = self._script(keys=[key], args=[limit, window_s])',
        needle="限流语义（Redis 那份）",
    ),
    Case(
        title="RT-14  forget 不删键（登录成功后额度没恢复）",
        target=REDIS_LIMITER,
        old="            self._client.delete(self._prefix + key)",
        new="            pass  # 反向测试:不删",
        needle="限流语义（Redis 那份）",
    ),
    Case(
        title="RT-15  Redis 故障时不再降级（fail-closed:一道闸把整个问答停掉）",
        target=REDIS_LIMITER,
        old="            return self._degrade(exc, window_s)",
        new="            raise",
        needle="Redis 故障时降级放行",
    ),
    Case(
        title="RT-16  把计数偷偷换回进程内存（重启清零、多 worker 各算各的）",
        target=DEPS,
        old="""    from project_rag.adapters.ratelimit_redis import RedisRateLimiter

    return lambda: RedisRateLimiter(settings.redis, key_prefix=limit_config.key_prefix)""",
        new="""    from project_rag.adapters.ratelimit_memory import MemoryRateLimiter

    return lambda: MemoryRateLimiter()  # 反向测试:又变回进程内存""",
        needle="配置真的落地",
    ),
    Case(
        title="RT-17  问答超限改回硬 429（把一句「稍后再试」做成错误码）",
        target=APP,
        old="""        if retry_after > 0:
            key = _rate_limit_key(http_request, user)""",
        new="""        if retry_after > 0:
            raise HTTPException(status_code=429, detail="too many")
            key = _rate_limit_key(http_request, user)""",
        needle="问答超限回的是**兜底话术**",
    ),
    Case(
        title="RT-18  兜底话术里不写等待秒数（用户只能靠反复重试去猜）",
        target=APP,
        old='        f"问得有点密集,我需要缓一下 —— 请等约 {retry_after} 秒再试(这一档上限:{quota})。"',
        new='        f"问得有点密集,请稍后再试(这一档上限:{quota})。"',
        needle="问答超限回的是**兜底话术**",
    ),
    Case(
        title="RT-19  被限流那一轮也写进会话历史（下一轮提示词里多一句系统的话）",
        target=APP,
        old='''            response.headers["Retry-After"] = str(retry_after)''',
        new='''            if request.session_id:
                from project_rag.domain import ConversationTurn as _Turn

                _factory(deps, "conversation_store_factory").record(
                    request.session_id, _Turn(question="(限流)", answer="请稍后再试"), keep=5
                )
            response.headers["Retry-After"] = str(retry_after)''',
        needle="兜底话术**不写进会话历史**",
    ),
    Case(
        title="RT-20  WS 超限回 error 帧（而不是话术的 token+done 形制）",
        target=APP,
        old='''                    await websocket.send_json({"type": "token", "text": notice})''',
        new='''                    await websocket.send_json({"type": "error", "detail": notice})''',
        needle="HTTP 与 WS 共用同一份额度",
    ),
    Case(
        title="RT-21  前端把限流那一条画成红字错误（用户以为系统坏了,狂点重试）",
        target=MESSAGES,
        old="""        <p v-if="message.channel === 'throttled'" class="alert info">""",
        new="""        <p v-if="message.channel === 'throttled'" class="alert error">""",
        needle="前端：限流那一条用暖色提醒",
    ),
)


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env=dict(os.environ),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行:{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    bad = 0
    for case in CASES:
        original = case.target.read_text(encoding="utf-8")
        hits = original.count(case.old)
        if hits != 1:
            print(
                f"⚠️  {case.title}\n      锚点出现 {hits} 次（必须恰好 1 次）—— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(case.target, case.old, case.new):
            red, line = run(case.needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {case.title}\n      {line}")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

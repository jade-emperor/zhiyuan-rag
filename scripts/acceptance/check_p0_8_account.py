"""账号自服务验收:自助注册、图形验证码、改密、注册能力自述。

钉住的承诺:
* 验证码一次性、有 TTL、答案不出现在响应里,输错也作废;
* 自助注册只能建 ``employee``(请求体里根本没有 role 字段),口令策略逐条可见;
* 关闭自助注册 / 邀请码不对 → 拒绝,且不建号;
* 改密撤销该用户**全部**旧令牌(改密即踢下线),当前设备拿到新令牌;
* 前端:注册路由、主题变量、注册页的验证码与策略提示都随能力自述走。
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from _harness import SkipCheck, check, report
from project_rag.adapters.captcha_image import ImageCaptchaRenderer
from project_rag.adapters.captcha_memory import MemoryCaptchaStore
from project_rag.api.app import create_app
from project_rag.cli.deps import Deps
from project_rag.config import get_settings
from project_rag.domain.auth import User, UserRole

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"


class _Store:
    """``AuthStore`` 的内存实现:哈希与令牌由用例层算好传进来。"""

    def __init__(self) -> None:
        self.users: dict[str, tuple[User, str]] = {}
        self.tokens: dict[str, tuple[str, float]] = {}
        self.schemas = 0

    def init_schema(self) -> None:
        self.schemas += 1

    def create_user(self, username: str, password_hash: str, role: UserRole) -> User:
        if username in self.users:
            raise ValueError(f"用户名 {username!r} 已存在")
        user = User(user_id=f"u{len(self.users)}", username=username, role=role)
        self.users[username] = (user, password_hash)
        return user

    def find_by_username(self, username: str) -> tuple[User, str] | None:
        return self.users.get(username)

    def set_password(self, user_id: str, password_hash: str) -> None:
        for name, (user, _) in self.users.items():
            if user.user_id == user_id:
                self.users[name] = (user, password_hash)

    def delete_user(self, user_id: str) -> int:
        return 1 if self.users.pop(user_id, None) is not None else 0

    def list_users(self) -> list[User]:
        return [user for user, _ in self.users.values()]

    def issue_token(self, user: User, token_hash: str, ttl_seconds: int) -> None:
        self.tokens[token_hash] = (user.user_id, time.time() + ttl_seconds)

    def user_for_token(self, token_hash: str) -> User | None:
        item = self.tokens.get(token_hash)
        if item is None or item[1] <= time.time():
            return None
        return next((u for u, _ in self.users.values() if u.user_id == item[0]), None)

    def revoke_token(self, token_hash: str) -> None:
        self.tokens.pop(token_hash, None)

    def revoke_all_tokens(self, user_id: str) -> int:
        doomed = [h for h, (owner, _) in self.tokens.items() if owner == user_id]
        for token_hash in doomed:
            self.tokens.pop(token_hash, None)
        return len(doomed)

    def bind_session(self, session_id: str, user_id: str) -> None:
        ...

    def owner_of(self, session_id: str) -> str | None:
        return None


def _client() -> tuple[TestClient, _Store, MemoryCaptchaStore]:
    """输入:无。行为:建一份注入式应用(真验证码适配器 + 内存认证存储)。输出:客户端与两个假件。"""
    store = _Store()
    captcha = MemoryCaptchaStore()
    deps = Deps(
        auth_store_factory=lambda: store,
        captcha_store_factory=lambda: captcha,
        captcha_renderer_factory=ImageCaptchaRenderer,
        suffixes_factory=lambda: (".md",),
    )
    return TestClient(create_app(deps)), store, captcha


def _answer(captcha: MemoryCaptchaStore, ticket: str) -> str:
    """输入:验证码存放与票据。行为:取回这张票据的答案(仅验收能这么读)。输出:答案文本。"""
    item = captcha.__dict__["_items"].get(ticket)
    assert item is not None, f"票据 {ticket!r} 不在存放里 —— 下发与校验不是同一个实例"
    return str(item[0])


def _ticket(client: TestClient, captcha: MemoryCaptchaStore) -> tuple[str, str]:
    """输入:客户端与存放。行为:要一张新码并读回答案。输出:``(票据, 答案)``。"""
    response = client.get("/api/v1/auth/captcha")
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    return ticket, _answer(captcha, ticket)


@check("验证码:下发的是 PNG data URI,票据与有效期齐全,答案不出现在响应里")
def _() -> str:
    client, _store, captcha = _client()
    response = client.get("/api/v1/auth/captcha")
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body) == ["expires_in", "image", "ticket"], sorted(body)
    prefix = "data:image/png;base64,"
    assert body["image"].startswith(prefix), body["image"][:40]
    raw = base64.b64decode(body["image"][len(prefix) :])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8]
    answer = _answer(captcha, body["ticket"])
    assert answer not in response.text, "答案原样出现在响应里 —— 这张图等于没有"
    assert body["expires_in"] == get_settings().api.captcha.ttl_s
    return f"{len(raw)} 字节 PNG,{len(answer)} 位答案未泄露"


@check("验证码:一次一票 —— 同一张图用第二次必须失效(否则可以拿它穷举)")
def _() -> str:
    client, _store, captcha = _client()
    ticket, answer = _ticket(client, captcha)
    client.post("/api/v1/auth/captcha")  # 再发一张,不影响上一张的判定
    first = client.post(
        "/api/v1/auth/register",
        json={
            "username": "once",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer.lower(),
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/v1/auth/register",
        json={
            "username": "twice",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer,
        },
    )
    assert second.status_code == 400, second.text
    assert "失效" in second.json()["detail"], second.json()
    return "第一次 201,复用同票 400(已失效)"


@check("验证码:输错也作废(不能拿同一张图反复试)")
def _() -> str:
    client, store, captcha = _client()
    ticket, answer = _ticket(client, captcha)
    wrong = "ZZZZ" if answer.upper() != "ZZZZ" else "YYYY"
    bad = client.post(
        "/api/v1/auth/register",
        json={
            "username": "tester",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": wrong,
        },
    )
    assert bad.status_code == 400, bad.text
    assert "验证码" in bad.json()["detail"], bad.json()
    retry = client.post(
        "/api/v1/auth/register",
        json={
            "username": "tester",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer,
        },
    )
    assert retry.status_code == 400 and "失效" in retry.json()["detail"], retry.text
    assert "tester" not in store.users, "验证码没过却建了号"
    return "错一次 400,再用同一票 400(已作废),库里没有新账号"


@check("注册:成功即 201 + 令牌,角色固定 employee(请求体里没有 role)")
def _() -> str:
    client, store, captcha = _client()
    ticket, answer = _ticket(client, captcha)
    response = client.post(
        "/api/v1/auth/register",
        json={
            "username": "alice",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "employee", body
    assert body["token"], body
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["is_admin"] is False, me.text
    user = store.users["alice"][0]
    assert user.role == "employee", user
    # 显式给 role 也不该被采纳:请求模型里没有这个字段
    return f"alice → {user.role},令牌可直接调用 /auth/me"


@check("注册:口令策略逐条给出原因(长度 / 字母 / 数字 / 含登录名 / 弱口令)")
def _() -> str:
    client, _store, captcha = _client()
    settings = get_settings().api.registration
    cases = [
        ("短", "Ab1", "至少"),
        ("纯数字", "1234567890", "字母"),
        ("纯字母", "abcdefghijkl", "数字"),
        ("含登录名", "bobpassw0rd", "登录名"),
        ("弱口令", "password", "弱口令"),
    ]
    seen: list[str] = []
    for username, password, needle in cases:
        ticket, answer = _ticket(client, captcha)
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "bob" if needle == "登录名" else f"u{len(seen)}",
                "password": password,
                "captcha_ticket": ticket,
                "captcha_answer": answer,
            },
        )
        assert response.status_code == 400, f"{username}: {response.text}"
        detail = response.json()["detail"]
        assert needle in detail, f"{username} 的报错没点名 {needle}:{detail}"
        seen.append(needle)
    assert settings.min_password_chars >= 8, settings
    return f"{len(cases)} 种不合规口令都被拦下并点名原因({settings.min_password_chars} 位下限)"


@check("注册:重名 → 400,且不会覆盖已有账号的密码")
def _() -> str:
    client, store, captcha = _client()
    first = _ticket(client, captcha)
    created = client.post(
        "/api/v1/auth/register",
        json={
            "username": "dup",
            "password": "Str0ngPass!",
            "captcha_ticket": first[0],
            "captcha_answer": first[1],
        },
    )
    assert created.status_code == 201, created.text
    before = store.users["dup"][1]
    second = _ticket(client, captcha)
    again = client.post(
        "/api/v1/auth/register",
        json={
            "username": "dup",
            "password": "Other0Pass!",
            "captcha_ticket": second[0],
            "captcha_answer": second[1],
        },
    )
    assert again.status_code == 400, again.text
    assert store.users["dup"][1] == before, "重名注册把别人的密码改掉了"
    return "第二次 400,原哈希未变"


@check("注册:关闭自助注册 → 403(且不校验验证码、不建号)")
def _() -> str:
    settings = get_settings()
    original = settings.api.registration.enabled
    settings.api.registration.enabled = False
    try:
        client, store, captcha = _client()
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "nobody",
                "password": "Str0ngPass!",
                "captcha_ticket": "whatever",
                "captcha_answer": "ABCD",
            },
        )
        assert response.status_code == 403, response.text
        assert "自助注册" in response.json()["detail"], response.json()
        assert store.users == {}, "关着自助注册却建了号"
        caps = client.get("/api/v1/capabilities").json()["registration"]
        assert caps["enabled"] is False, caps
        return "403 + capabilities 报 enabled=false"
    finally:
        settings.api.registration.enabled = original


@check("注册:邀请码必填时错码 → 403(而且比验证码先挡住)")
def _() -> str:
    settings = get_settings()
    original = settings.api.registration.invite_code
    settings.api.registration.invite_code = "HR-2026"
    try:
        client, store, captcha = _client()
        ticket, answer = _ticket(client, captcha)
        bad = client.post(
            "/api/v1/auth/register",
            json={
                "username": "invited",
                "password": "Str0ngPass!",
                "captcha_ticket": ticket,
                "captcha_answer": answer,
                "invite_code": "WRONG",
            },
        )
        assert bad.status_code == 403 and "邀请码" in bad.json()["detail"], bad.text
        client2, store2, captcha2 = _client()
        ticket2, answer2 = _ticket(client2, captcha2)
        ok = client2.post(
            "/api/v1/auth/register",
            json={
                "username": "invited",
                "password": "Str0ngPass!",
                "captcha_ticket": ticket2,
                "captcha_answer": answer2,
                "invite_code": "HR-2026",
            },
        )
        assert ok.status_code == 201, ok.text
        assert "invited" in store2.users and "invited" not in store.users
        return "错码 403,对码 201"
    finally:
        settings.api.registration.invite_code = original


@check("改密:原密码错 → 401;成功 → 旧令牌立刻失效,新令牌可用")
def _() -> str:
    client, _store, captcha = _client()
    ticket, answer = _ticket(client, captcha)
    created = client.post(
        "/api/v1/auth/register",
        json={
            "username": "changer",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer,
        },
    )
    assert created.status_code == 201, created.text
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    wrong = client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"old_password": "NotMyPass!1", "new_password": "Fresh0Pass!"},
    )
    assert wrong.status_code == 401, wrong.text
    changed = client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"old_password": "Str0ngPass!", "new_password": "Fresh0Pass!"},
    )
    assert changed.status_code == 200, changed.text
    new_token = changed.json()["token"]
    assert new_token != created.json()["token"], "改密后令牌没换"
    stale = client.get("/api/v1/auth/me", headers=headers)
    assert stale.status_code == 401, f"旧令牌还能用:{stale.status_code}"
    fresh = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert fresh.status_code == 200, fresh.text
    login = client.post(
        "/api/v1/auth/login", json={"username": "changer", "password": "Fresh0Pass!"}
    )
    assert login.status_code == 200, login.text
    return "旧令牌 401,新令牌 200,新密码可登录"


@check("改密:新密码不合规 / 与原密码相同 → 400,密码不变")
def _() -> str:
    client, store, captcha = _client()
    ticket, answer = _ticket(client, captcha)
    created = client.post(
        "/api/v1/auth/register",
        json={
            "username": "picker",
            "password": "Str0ngPass!",
            "captcha_ticket": ticket,
            "captcha_answer": answer,
        },
    )
    assert created.status_code == 201, created.text
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    before = store.users["picker"][1]
    weak = client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"old_password": "Str0ngPass!", "new_password": "short"},
    )
    assert weak.status_code == 400, weak.text
    same = client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"old_password": "Str0ngPass!", "new_password": "Str0ngPass!"},
    )
    assert same.status_code == 400 and "相同" in same.json()["detail"], same.text
    assert store.users["picker"][1] == before, "被拒的改密却把密码改了"
    return "两条 400,哈希未变"


# ── 前端(静态) ───────────────────────────────────────────────────────────────

def _read(relative: str) -> str:
    path = WEB / relative
    if not path.is_file():
        raise SkipCheck(f"前端源码不在本地({relative})")
    return path.read_text(encoding="utf-8")


@check("前端:注册路由存在且被守卫放行(不在登录页与注册页之间绕圈)")
def _() -> str:
    router = _read("router.ts")
    assert "RegisterView" in router, "路由表里没有注册页"
    assert "path: '/register'" in router, router[:400]
    assert "to.name === 'register'" in router, "守卫没放行 register —— 会被弹回登录页"
    return "register 路由 + 守卫放行"


@check("前端:注册页按能力自述渲染(验证码 / 邀请码 / 口令下限都不写死)")
def _() -> str:
    view = _read("views/RegisterView.vue")
    for needle, why in (
        ("registration", "没读 capabilities.registration"),
        ("captcha_required", "验证码开关写死"),
        ("invite_required", "邀请码开关写死"),
        ("min_password_chars", "口令下限写死"),
        ("refreshCaptcha", "失败后没换一张图"),
    ):
        assert needle in view, f"{why}:缺 {needle}"
    assert "/^\\d{6,}$/" not in view and "minChars = 8" not in view, "口令规则被抄进了前端"
    return "5 个能力字段全部来自后端"


@check("前端:深浅色主题有同名变量覆盖,且选择会持久化")
def _() -> str:
    tokens = _read("styles/tokens.css")
    assert ":root[data-theme='dark']" in tokens, "没有深色变量块"
    for name in ("--bg:", "--text:", "--border:", "--accent:"):
        assert tokens.count(name) >= 2, f"{name} 只在浅色里定义,深色会漏色"
    theme = _read("stores/theme.ts")
    assert "localStorage" in theme and "setItem" in theme, "主题选择没有持久化"
    assert "prefers-color-scheme" in theme, "没跟随系统"
    assert "initTheme" in _read("main.ts"), "主题没在首帧之前应用"
    return "深色覆盖 4 个基础色 + 持久化 + 跟随系统"


@check("前端:表单错误按字段定位,登录页预填上次账号(不逼用户每月重打一次用户名)")
def _() -> str:
    login = _read("views/LoginView.vue")
    assert "storedUsername()" in login, "登录页没预填上次用户名(本地已经存了它)"
    assert "fieldErrors" in login, "错误没有按字段定位 —— 用户只能看到一条笼统提示"
    assert "fieldErrors.username" in login and "fieldErrors.password" in login, "两个字段都要有自己的位置"
    assert "请填写用户名" in login and "请填写密码" in login, "字段级提示要指名缺什么"
    register = _read("views/RegisterView.vue")
    assert "两次输入的密码不一致" in register, "确认密码不一致没有单独提示"
    return "登录 2 个字段 + 注册确认密码均为字段级"


@check("前端:Esc 关浮层、标题随路由变化(键盘可达与可辨识)")
def _() -> str:
    header = _read("components/AppHeader.vue")
    assert "'Escape'" in header, "顶栏浮层没有键盘出口"
    assert "removeEventListener" in header, "键盘监听没在卸载时摘掉(路由切换后会泄漏)"
    router = _read("router.ts")
    assert "document.title" in router, "标题不随路由变 —— 多标签页里认不出哪个是哪个"
    for name in ("login", "register", "chat"):
        assert f"{name}:" in router, f"标题表缺 {name}"
    return "Esc 关闭 + 标题表覆盖 login/register/chat 等"


if __name__ == "__main__":
    sys.exit(report("账号自服务验收（注册 / 验证码 / 改密 / 主题）"))

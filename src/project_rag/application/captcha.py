"""图形验证码的用例:签发挑战、校验答案。

判定规则:
* 答案一次性 —— 校验失败也作废,必须换一张图(否则可以拿同一张图穷举);
* 比较不区分大小写(用户分不清 O/0,已在字母表里去掉易混字符);
* 票据不透明,答案不出现在响应里。
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from project_rag.ports.captcha import CaptchaRenderer, CaptchaStore

CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    """下发给客户端的一张图。``image`` 是 data URI,前端直接塞进 ``<img src>``。"""

    ticket: str
    image: str
    expires_in: int


def new_code(length: int) -> str:
    """输入:位数。行为:从无易混字符集的字母表里随机取。输出:大写文本。"""
    return "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(max(1, length)))


def issue_captcha(
    renderer: CaptchaRenderer, store: CaptchaStore, *, ttl_s: int, length: int
) -> CaptchaChallenge:
    """输入:渲染器、存放、有效期、位数。行为:生成 → 画图 → 记票据。输出:挑战。"""
    code = new_code(length)
    ticket = secrets.token_urlsafe(18)
    store.put(ticket, code, ttl_s)
    png = renderer.render(code)
    return CaptchaChallenge(
        ticket=ticket,
        image="data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        expires_in=ttl_s,
    )


def verify_captcha(store: CaptchaStore, *, ticket: str, answer: str) -> None:
    """输入:存放、票据、用户输入。行为:取回答案比对(**取即作废**)。输出:无。

    失败抛 ``ValueError``,消息给人看但不提示正确答案。
    """
    expected = store.take(ticket)
    if expected is None:
        raise ValueError("验证码已失效,请刷新后重试")
    if expected.strip().casefold() != answer.strip().casefold():
        raise ValueError("验证码不正确")

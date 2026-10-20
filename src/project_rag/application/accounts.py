"""
用例:账号管理的**判定**(纯函数,不碰存储、不碰密码学)。

建号与删号是两个"错了就很贵"的动作:建号会给出一个能登录的凭证,删号会切断一个人的入口
(而且删错了没法撤销)。所以两边各有一组守卫,写在这里而不是接口层 ——
CLI(``project-rag user``)与网页端(管理员页)**必须同一套规矩**,而两份实现迟早不一样:

* 建号:登录名(长度 / 字符 / 重名)、密码(最短长度)、角色(必须是闭集里那三个);
* 删号:**不能删自己**、**不能删掉最后一个管理员**、目标必须存在。

**为什么"不能删自己"**:删掉自己之后当前这枚令牌立刻失效,人就被踢出登录页了 ——
而他刚才多半只是点错了行。要删自己的账号,正确的做法是先建一个新的管理员再用那个号删。

**为什么要护住"最后一个管理员"**:管理员是唯一能建号删号的角色。删光之后**没有任何
界面能再建一个**(网页端要求管理员、CLI 要在宿主机上跑)—— 一个只能靠进容器补救的系统
不叫安全,叫锁死。这条守卫与 ``domain/access.py`` 的"管理员永远可见"是同一类保护:
**别让一次误操作把系统变成没人能管的状态**。

重名比较用 ``casefold()``:MySQL 的 ``utf8mb4`` 默认排序规则**不区分大小写**,
``Boss`` 与 ``boss`` 在库里是同一个名字。守卫因此也得按不区分大小写比较 ——
否则界面会先放过、再被数据库的 IntegrityError 顶回来,而那句报错是英文的 SQL 错误。
"""

from __future__ import annotations

from collections.abc import Sequence

from project_rag.domain.access import Group
from project_rag.domain.auth import USER_ROLES, User, UserRole

MIN_USERNAME_CHARS = 2
"""登录名最短几个字。**1 个字的名字在列表里看不出是谁**,而账号列表是管理员唯一的入口。"""

MAX_USERNAME_CHARS = 32
"""登录名上限。与 ``users.username`` 的 ``VARCHAR(64)`` 留一半余量:超长名字进库前就该拦。"""

MIN_PASSWORD_CHARS = 8
"""密码最短长度。**建号路径与 CLI 共用这一条** —— 网页端不该比命令行更宽松。"""


def check_new_account(
    existing: Sequence[User], *, username: str, password: str, role: str
) -> str:
    """输入:现有账号、登录名、明文密码、角色。行为:逐条校验。

    输出:规范化后的登录名(去掉首尾空白)。任何一条不合规都抛 ``ValueError``,**消息给人看**。
    """
    name = username.strip()
    if len(name) < MIN_USERNAME_CHARS:
        raise ValueError(f"登录名至少 {MIN_USERNAME_CHARS} 个字,收到 {name!r}")
    if len(name) > MAX_USERNAME_CHARS:
        raise ValueError(f"登录名最多 {MAX_USERNAME_CHARS} 个字,收到 {len(name)} 个")
    if any(character.isspace() for character in name):
        raise ValueError(f"登录名里不能有空格:{name!r}")
    if role not in USER_ROLES:
        raise ValueError(f"角色只能是 {'/'.join(USER_ROLES)},收到 {role!r}")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"密码至少 {MIN_PASSWORD_CHARS} 位,收到 {len(password)} 位")
    taken = {user.username.casefold() for user in existing}
    if name.casefold() in taken:
        raise ValueError(f"登录名 {name!r} 已经被占用")
    return name


def check_new_password(password: str) -> None:
    """输入:明文密码。行为:校验长度。输出:无;不合规抛 ``ValueError``。

    改密码**不校验登录名与重名**(那两个字段没变),所以单独一条 —— 复用
    ``check_new_account`` 会逼调用方编一个假用户名出来。
    """
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"密码至少 {MIN_PASSWORD_CHARS} 位,收到 {len(password)} 位")


WEAK_PASSWORDS: tuple[str, ...] = (
    "12345678",
    "123456789",
    "1234567890",
    "password",
    "password1",
    "qwerty123",
    "admin123",
    "abcd1234",
    "11111111",
    "88888888",
)


def password_rules(password: str, *, username: str, min_chars: int) -> list[str]:
    """输入:密码、用户名、下限。行为:逐条检查。输出:**没满足的**规则说明(空列表 = 通过)。

    自助注册与改密共用这一份 —— 这两条入口是用户自己设密码,必须比管理员建号更严:
    长度下限更高,且要求字母 + 数字、不许包含登录名、不许是常见弱口令。
    返回列表而不是抛异常:前端要逐条显示"哪条没满足"。
    """
    unmet: list[str] = []
    if len(password) < min_chars:
        unmet.append(f"至少 {min_chars} 位")
    if not any(character.isdigit() for character in password):
        unmet.append("包含数字")
    if not any(character.isalpha() for character in password):
        unmet.append("包含字母")
    if username and username.casefold() in password.casefold():
        unmet.append("不要包含登录名")
    if password.casefold() in WEAK_PASSWORDS:
        unmet.append("不要用常见弱口令")
    return unmet


def check_registration_password(password: str, *, username: str, min_chars: int) -> None:
    """输入:密码、用户名、长度下限。行为:校验自助注册/改密的口令策略。输出:无。

    不合规抛 ``ValueError``,消息把**所有**没满足的规则一次说清。
    """
    unmet = password_rules(password, username=username, min_chars=max(min_chars, MIN_PASSWORD_CHARS))
    if unmet:
        raise ValueError("密码不合规:" + "、".join(unmet))



def check_delete_account(
    existing: Sequence[User], *, user_id: str, actor_id: str
) -> User:
    """输入:现有账号、要删的用户 ID、动手的人的用户 ID。行为:三条守卫。

    输出:被删的那个 ``User``(调用方要用它的名字写日志);不合法一律 ``ValueError``。
    """
    target = next((user for user in existing if user.user_id == user_id), None)
    if target is None:
        raise ValueError(f"没有这个账号({user_id!r})—— 可能已经被别人删掉了")
    if target.user_id == actor_id:
        raise ValueError("不能删自己的账号:删掉之后这枚令牌立刻失效,你就被踢出去了")
    if target.is_admin and sum(1 for user in existing if user.is_admin) <= 1:
        raise ValueError(
            f"{target.username!r} 是最后一个管理员 —— 删掉之后没人能再建号或改权限了。"
            "先建一个新的管理员,再用那个账号来删"
        )
    return target


def check_group_name(existing: Sequence[Group], *, name: str) -> str:
    """输入:现有的组、新组名。行为:校验长度与非重名(不区分大小写)。输出:规范化后的组名。

    组名是管理员在界面上唯一认得的标识(授权时看到的就是它),所以:
    重名要报错(重名建组会让"给销售部加个人"加到另一个销售部里),长度上下限与登录名同一套。
    """
    clean = name.strip()
    if len(clean) < MIN_USERNAME_CHARS:
        raise ValueError(f"组名至少 {MIN_USERNAME_CHARS} 个字,收到 {clean!r}")
    if len(clean) > MAX_USERNAME_CHARS + 32:
        raise ValueError(f"组名最多 {MAX_USERNAME_CHARS + 32} 个字,收到 {len(clean)} 个")
    taken = {group.name.casefold() for group in existing}
    if clean.casefold() in taken:
        raise ValueError(f"组名 {clean!r} 已经存在")
    return clean


def role_of(role: str) -> UserRole:
    """输入:角色文本。行为:校验落在闭集里(并让类型检查器收窄)。输出:``UserRole``。

    存储层也各校验一遍(``adapters/auth_mysql.py`` 的 ``_role``):那边防的是**库里的脏数据**,
    这里防的是**接口传来的字符串** —— 两个方向,都少了不行。
    """
    if role not in USER_ROLES:
        raise ValueError(f"角色只能是 {'/'.join(USER_ROLES)},收到 {role!r}")
    return role  # type: ignore[return-value]  # 上面已收窄到字面量集合

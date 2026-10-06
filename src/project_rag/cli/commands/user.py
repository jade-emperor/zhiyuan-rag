"""user 子命令:建账号、改密码、看列表。**这是唯一的建号入口**(没有注册接口)。

**为什么不做注册接口**:这是个知识库问答系统,用户是"同事",不是公众。开放注册等于
把"谁能检索内部资料"这件事交给互联网;而管理员手工建号 + 初始密码,是这类系统
最省事也最不该省的一步。

**为什么必须能从命令行建第一个管理员**:鸡生蛋 —— 没有账号就登不进去,登不进去就建不了账号。

**密码怎么传**:``--password`` 不给就**交互式输入**(``getpass``,不回显、不进 shell 历史)。
刻意不支持"从管道读"以外的自动化:把密码写在命令行里会进 shell 历史与进程列表。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from project_rag.domain.auth import USER_ROLES
from project_rag.observability import get_logger

_logger = get_logger("cli.user")

NAME = "user"


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """输入:子命令注册器。行为:注册 user,含 add / passwd / list 三个动作。输出:解析器。"""
    parser = sub.add_parser(NAME, help="账号管理:建号(add)/ 改密码(passwd)/ 列表(list)")
    actions = parser.add_subparsers(dest="action", required=True, metavar="<动作>")

    add = actions.add_parser("add", help="建一个账号(重名会报错,不会覆盖)")
    add.add_argument("--username", "-u", required=True, help="登录名")
    add.add_argument(
        "--role", choices=USER_ROLES, default="employee", help="角色(默认 employee)"
    )
    add.add_argument(
        "--password", default=None, help="密码;不给就交互式输入(推荐,不进 shell 历史)"
    )

    actions.add_parser("list", help="列出全部账号(不含密码)")

    passwd = actions.add_parser("passwd", help="改密码")
    passwd.add_argument("--username", "-u", required=True, help="登录名")
    passwd.add_argument("--password", default=None, help="新密码;不给就交互式输入")
    return parser


def _ask_password(confirm: bool = True) -> str:
    """输入:是否需要再输一遍确认。行为:交互式读密码。输出:密码。

    两次不一致 / 空密码直接退出(返回码 1):这是建号路径,宁可多敲一次也不能写进一个空密码。
    """
    first = getpass.getpass("密码:")
    if not first:
        print("密码不能为空", file=sys.stderr)
        raise SystemExit(1)
    if confirm and getpass.getpass("再输一遍:") != first:
        print("两次输入不一致", file=sys.stderr)
        raise SystemExit(1)
    return first


def run(args: argparse.Namespace) -> int:
    """输入:解析好的参数(含 args.deps)。行为:按动作建号 / 改密码 / 列表。输出:退出码。"""
    from project_rag.application.security import hash_password, verify_password

    deps = args.deps
    if deps.auth_store_factory is None:
        print("没有接上认证存储(auth_store_factory 为空)", file=sys.stderr)
        return 1
    store = deps.auth_store_factory()
    store.init_schema()  # 幂等:第一次跑这个命令时把三张表建出来

    if args.action == "add":
        password = args.password or _ask_password()
        try:
            user = store.create_user(args.username, hash_password(password), args.role)
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        print(f"已建号:{user.username}({user.role}) user_id={user.user_id}")
        if user.role == "admin":
            print("这是管理员账号:能上传/删除文档、看观测报表。")
        return 0

    if args.action == "passwd":
        found = store.find_by_username(args.username)
        if found is None:
            print(f"没有这个用户:{args.username}", file=sys.stderr)
            return 1
        password = args.password or _ask_password()
        if verify_password(password, found[1]):
            print("新密码与旧密码相同 —— 换个吧", file=sys.stderr)
            return 1
        # 没有"改密码"端口方法:删号重建会丢 user_id(会话归属挂在它上面),
        # 所以这里必须有一条 UPDATE。见 ports/auth.py 的 set_password。
        store.set_password(found[0].user_id, hash_password(password))
        print(f"已改密码:{args.username}(旧令牌仍然有效,要立刻失效请让 TA 重新登录前先 revoke)")
        return 0

    if args.action == "list":
        users = store.list_users()
        if not users:
            print("还没有任何账号 —— 先 `project-rag user add -u 你的名字 --role admin`")
            return 0
        print(f"{'登录名':<20}{'角色':<10}user_id")
        for user in users:
            print(f"{user.username:<20}{user.role:<10}{user.user_id}")
        return 0

    print(f"不认识的动作:{args.action}", file=sys.stderr)
    return 1

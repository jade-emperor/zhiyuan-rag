"""命令行入口。只做:建解析器 → 装配进程级依赖 → 分发。

子命令本体都在 cli/commands/ 下,一个模块一个子命令,
所以这个文件不会随着子命令变多而变长。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from project_rag.cli import commands
from project_rag.observability import configure_logging, get_logger
from project_rag.ports import LLMClient

# 创建日志子节点project_rag.cli
_log = get_logger("cli")

PROG = "project-rag"


def _build_parser() -> argparse.ArgumentParser:
    """构建根解析器和子命令解析器,然后把 commands.ALL 里每个模块的参数声明循环装进子命令解析器。"""
    # 参数1表示根参数解析器的名字,参数2表示根参数解析器的描述
    parser = argparse.ArgumentParser(prog=PROG, description="RAG 项目命令行入口")
    # 开启子命令,参数1表示命令行第一个参数传给args.cmd属性,required=True表示必须传子命令,参数3表示子命令帮助信息的描述
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<子命令>")
    # 通过conmmands中__init__中的ALL 循环注册子命令
    for module in commands.ALL:
        module.add_parser(sub)  # 解耦,调用每个命令模块的add_parser函数注册子命令
    return parser


def cli(
    argv: list[str] | None = None,
    *,
    llm_factory: Callable[[], LLMClient] | None = None,
) -> int:
    """解析参数并分发到子命令。返回进程退出码。

    llm_factory 是留给测试的缝:传一个假适配器工厂,整条 CLI 链路就能跑测试,
    不联网、不花钱、结果确定。传了它,连 .env 都不需要。
    """
    # --help / 用法错误在 parse_args 里以 SystemExit(0 / 2) 结束,原样抛给进程
    # 加载参数解析器
    parser = _build_parser()
    # 从命令行解析命令行参数,并且获取这些参数组成的 argparse.Namespace 对象
    args = parser.parse_args(argv)

    if llm_factory is None:
        # 只有真实运行才装配日志。
        # 注入 factory = 进程装配归调用方负责(测试场景),不碰真实日志目录,也不读.env
        try:
            configure_logging()  # 进行全局的日志配置
        except Exception as exc:  # 配置坏了就 fail-fast,别带着半个配置往下跑
            print(f"读取配置失败：{exc}", file=sys.stderr)
            return 1
        _log.debug("执行子命令:%s", args.cmd)

    # 组合根:把「怎么造 LLMClient」挂到 args 上,哪个子命令需要就自己取
    # python的动态加属性方式
    args.llm_factory = llm_factory

    # 构建字典,内容是模块名字:run函数,所以后面新建子命令入口一定要是run函数
    handlers = {module.NAME: module.run for module in commands.ALL}

    # args.cmd表示命令行打的子命令handlers[args.cmd]表示执行的函数,args则是一个命名空间对象
    # 这里是真正的核心逻辑,命令行写子命令+其他参数,这里就调用对应子命令的run函数并且将包括子命令名字的所有参数传入
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(cli())

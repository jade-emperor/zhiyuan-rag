"""子命令注册表:一个子命令一个模块,cli.py 只管循环注册和分发。

新增一个子命令只要两步:
  1. 复制一份最像的模块(比如 chat.py),改 NAME / add_parser / run;
  2. 在下面的 ALL 里加一项。
cli.py 完全不用动 —— 它不认识任何一个具体子命令。

模块契约(只要这三个名字,cli.py 不认别的):
  NAME                              子命令名,必须和 add_parser 里用的完全一致
  add_parser(sub) -> ArgumentParser 在这里声明本子命令的参数,返回解析器
  run(args) -> int                  干活,返回进程退出码;不要自己 sys.exit

进程级依赖由 cli.py 挂在 args 上 需要就从 args 里取。

两条硬规矩:
  - 命令模块顶层只 import 轻量东西(标准库 / domain / ports)。
    重依赖(langchain、transformers、chromadb...)一律放进 run() 内部,
    否则 uv run project-rag --help 会被这些包拖慢。
"""

from . import chat

ALL = (chat,)

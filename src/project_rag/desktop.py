"""
桌面壳(M7.4):把一个原生窗口套在前端产物上。**壳只做一件事 —— 开窗**。

为什么壳里**没有**任何业务代码:这个 app 的全部环境就是"一个后端地址"(见
``docs/frontend.md`` §1)。壳不需要认识 Milvus / RAG / 会话,它只要把页面打开、
让页面自己去连后端。所以这里没有网络请求、没有登录逻辑、没有配置存储 ——
那些都在页面里(并且浏览器版本照样能用)。

两种开法,差别只在**页面从哪来**:

* ``--local``(默认)—— 用**回环 HTTP** 托管随包发布的 `static/app/`(见 ``_BundleServer``),
  窗口打开 ``http://127.0.0.1:<随机端口>/?backend=…``。壳自带界面,后端只提供 API。
* ``--remote URL`` —— 直接打开后端托管的 ``URL/static/app/``。省掉"填地址"这一步,
  代价是界面来自那台后端(版本必须与壳里的代码兼容)。

**为什么本地模式不是 ``file://``(实测)**:产物里是 ``<script type="module">``,
而浏览器对 module 脚本按 CORS 取 —— ``file://`` 的源是 opaque(``null``),
**加载被挡,页面白屏**(控制台一行 "Access to script ... from origin 'null' has been blocked")。
所以壳必须自己起一个回环服务,而不是把本地文件直接喂给 webview。
好处顺带还有一条:来源变成 ``http://127.0.0.1:<port>``,localStorage 是正常可用的。

为什么 ``--help`` / ``--check`` **不需要装 GUI 依赖**:``pywebview`` 是在参数解析与
产物检查**之后**才 import 的(与 llm_deepseek / embedder 同一条规矩:
重依赖延迟 import)。所以 CI 或没有图形界面的机器上能跑 ``--check``,
而不会因为缺 GUI 栈炸在 import 上。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 打包后 __file__ 就在 project_rag/ 下,静态产物是它的兄弟目录
_BUNDLE = Path(__file__).resolve().parent / "api" / "static" / "app" / "index.html"


def bundle_index() -> Path:
    """输入:无。行为:前端产物 index.html 的路径。输出:路径(不保证存在)。"""
    return _BUNDLE


def bundle_problems() -> list[str]:
    """输入:无。行为:检查产物在不在、它引用的资源在不在。输出:问题列表(空 = 没问题)。

    **为什么要检查引用的资源**:只判断 index.html 存不存在会漏掉最常见的一种坏法 ——
    产物是旧的、``assets/`` 里的文件名已经换了(hash 变了),页面打开就是白屏,
    而那时壳会安静地开一个白窗口。
    """
    index = bundle_index()
    if not index.is_file():
        return [
            f"前端产物不在 {index}",
            "先在仓库根跑 `cd web && npm install && npm run build`(产物是入库的)",
        ]
    import re

    html = index.read_text(encoding="utf-8")
    missing = [
        name
        for name in re.findall(r'(?:src|href)="\./([^"]+)"', html)
        if not (index.parent / name).is_file()
    ]
    if missing:
        return [
            f"产物引用的资源不见了:{', '.join(missing)}",
            "重新构建:`cd web && npm run build`",
        ]
    return []


def target_url(*, remote: str | None, backend: str | None) -> str:
    """输入:远端地址(可选)、后端地址(可选)。行为:算出窗口该打开哪个 URL。输出:URL 字符串。

    只处理 ``--remote`` 这一支(它不需要起服务);本地模式的 URL 由 ``_local_url`` 在
    服务起来之后拼(端口是系统分配的,提前算不出来)。

    ``--backend`` 拼成 ``?backend=…``:页面启动时会认这个参数并记下来
    (见 ``web/src/api/client.ts`` 的 ``seedBackendFromQuery``),然后从地址栏抹掉。
    这样壳不必自己实现"配置存储",也不需要在页面里注入任何脚本。
    """
    if remote:
        return f"{remote.rstrip('/')}/static/app/"
    raise ValueError("本地模式的 URL 要等服务起来才知道(见 _local_url)")


class _BundleServer:
    """把前端产物用**回环 HTTP** 托管起来(端口由系统分配)。

    见模块 docstring:``file://`` 下 ES module 会被 CORS 挡掉,所以本地模式必须走 HTTP。
    只绑 ``127.0.0.1``(不对外),线程是 daemon(进程退出即收)。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._server: object | None = None

    def start(self) -> str:
        """输入:无。行为:起一个只绑回环的静态服务(端口 0 = 让系统挑)。输出:base URL。"""
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.directory))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """输入:无。行为:关掉服务。输出:无。"""
        server = self._server
        if server is not None:
            server.shutdown()  # type: ignore[attr-defined]
            server.server_close()  # type: ignore[attr-defined]
            self._server = None


def build_parser() -> argparse.ArgumentParser:
    """输入:无。行为:建参数解析器。输出:解析器(供 --help 与测试复用)。"""
    parser = argparse.ArgumentParser(
        prog="project-rag-desktop",
        description="project-rag 桌面壳:开一个窗口,里面是 RAG 前端(app 只需要一个后端地址)。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--local",
        action="store_true",
        help="打开随包发布的界面(file://,默认);首次在页面里填后端地址",
    )
    mode.add_argument(
        "--remote",
        metavar="URL",
        default=None,
        help="打开后端托管的界面,例如 http://192.168.1.5:8080(省掉填地址这一步)",
    )
    parser.add_argument(
        "--backend",
        metavar="URL",
        default=None,
        help="预置后端地址(本地模式用):页面启动时认一次并记住,例如 http://127.0.0.1:8080",
    )
    parser.add_argument("--title", default="RAG Assistant", help="窗口标题")
    parser.add_argument("--width", type=int, default=1280, help="窗口宽(默认 1280)")
    parser.add_argument("--height", type=int, default=860, help="窗口高(默认 860)")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="只起本地产物的回环服务并打印 URL(不开窗;想用自己浏览器看时有用)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查产物在不在、会打开什么,不开窗(可进 CI / 无图形界面的机器)",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="打印窗口会打开的 URL 后退出(不开窗)",
    )
    return parser


def _local_url(server: _BundleServer, backend: str | None) -> str:
    """输入:已启动的回环服务、后端地址。行为:拼出窗口要打开的 URL。输出:URL。"""
    base = server.start()
    return f"{base}/index.html" + (f"?backend={backend}" if backend else "")


def main(argv: list[str] | None = None) -> int:
    """输入:命令行参数(默认取 sys.argv)。行为:检查产物 →(可选)开窗/起服务。输出:退出码。

    退出码:0 = 正常;1 = 产物有问题;2 = 缺 GUI 依赖(话术里给安装命令)。
    """
    args = build_parser().parse_args(argv)
    problems = bundle_problems() if not args.remote else []

    # --check / --print-url:不开窗、也不需要 GUI 栈;会真的绑一次端口,好把 URL 打准
    if args.check or args.print_url:
        if args.remote:
            print(target_url(remote=args.remote, backend=args.backend))
        elif problems:
            print("产物有问题,没法起本地界面:", file=sys.stderr)
            for line in problems:
                print(f"  ✘ {line}", file=sys.stderr)
            return 1
        else:
            server = _BundleServer(bundle_index().parent)
            try:
                print(_local_url(server, args.backend))
            finally:
                server.stop()
            if args.check:
                print("  ✔ 产物与它引用的资源都在")
        return 0

    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        return 1

    if args.serve:
        server = _BundleServer(bundle_index().parent)
        url = _local_url(server, args.backend)
        print(f"本地产物服务已起:{url}", flush=True)
        print("(Ctrl-C 结束;窗口模式不带 --serve)", flush=True)
        try:
            import time

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0

    server = _BundleServer(bundle_index().parent)
    url = _local_url(server, args.backend)

    try:
        # webview 是**可选依赖**(desktop 那一档):没装时这条 import 就是失败的那一步
        # (返回码 2 并给出安装命令)。静态检查器看不到它,所以就地标一下。
        import webview  # type: ignore[import-not-found]
    except ImportError:
        print(
            "桌面壳要 pywebview,当前环境没装。两条路:\n"
            "  1) 装它:uv sync --extra desktop(或 pip install pywebview)\n"
            "  2) 不用壳:直接开浏览器访问后端的 /static/app/(界面完全一样)",
            file=sys.stderr,
        )
        return 2

    try:
        webview.create_window(args.title, url, width=args.width, height=args.height)
        webview.start()
    finally:
        server.stop()
    return 0


def _report_problems() -> int:
    """输入:无。行为:把 --check 的产物检查结果打出来。输出:退出码。"""
    problems = bundle_problems()
    for line in problems:
        print(f"  ✘ {line}")
    if not problems:
        print("  ✔ 产物与它引用的资源都在")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

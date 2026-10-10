"""验收脚本的公共壳子 —— 全部 ``check_*.py``(19 份)与反向 runner 共用一份,不再各抄一遍。

**为什么抽出来**:同一段壳子(``PASS/FAIL/SKIP`` + ``@check`` + ``record`` + 报告打印)
在当时那 13 份脚本里各抄一遍 ≈ **687 行**,而且已经漂出 6 个版本的 ``check``、9 个版本的 ``SkipCheck`` ——
改一次口径要改 13 处。这里只放"所有脚本一字不差都需要"的东西:

* 三态常量与自己的异常类型 ``SkipCheck``;
* ``check`` 装饰器 —— **装饰即执行**:检查在模块 import 时就全部跑完,报告在 ``report`` 里统一打
  (所以脚本里 ``@check`` 的定义顺序 = 报告的打印顺序);
* ``report(title)`` —— 打印 + 退出码;
* ``env`` —— 临时改环境变量(6 份脚本要用,一字不差);
* **import 时清一次 ``src/project_rag`` 的 ``.pyc``** —— 反向测试会读到坏版本的字节码
  (见 ``_purge_project_bytecode`` 的注释;``DEBUG=1`` 时会打印删了几个)。

**刻意不放进来的**:具体假件(在 ``_fakes.py``;而且**刻意的残缺必须留在脚本里** ——
例如 ``check_m5_qa`` 的假向量库故意不实现 ``upsert``,那条残缺本身就是"合流层没有越权写库"的证据)、
各脚本自己的 ``_require`` 懒加载、以及任何"只有一个脚本用"的东西。

用法(每个 ``check_*.py`` 里只剩这几行)::

    from _harness import FAIL, PASS, SKIP, SkipCheck, check, report

    @check("某条契约")
    def _() -> str:
        ...
        return "证据"

    def main() -> int:
        return report("M3 向量知识库 + RAG 验收")

    if __name__ == "__main__":
        sys.exit(main())

``_harness`` / ``_fakes`` 都是同目录下的兄弟模块:直接跑脚本时 ``sys.path[0]`` 就是
``scripts/acceptance/``,所以 ``import`` 得到,不需要把它们做成包(做成包反而会让
``python scripts/acceptance/check_x.py`` 这种用法失效)。
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

def _purge_project_bytecode() -> int:
    """输入:无。行为:删掉 ``src/project_rag`` 下所有 ``__pycache__/*.pyc``。输出:删了几个。

    **为什么验收要在每次 import 时做这件事**（真踩过):反向测试把源码改坏再还原
    之后,文件的**大小与 mtime(秒级)可能完全一样**(``cursor += 2`` → ``cursor += 1``),
    CPython 会认为缓存仍然有效,于是继续用**坏版本编译出来的 .pyc** ——
    检查读到的根本不是你刚还原的源码。实测症状:P0-5 的契约报「区间到 66,正文 68」,
    而源码里明明是 ``cursor += 2``;删掉那个 ``.pyc`` 立刻全绿。

    **两个方向都会骗人**:假红(改坏的字节码留着),以及更危险的**假绿**
    (反向测试读到好版本的旧字节码 → 报「检查是摆设」,或者真回归被漏掉)。

    **只清项目自己的**:第三方包(torch / pymilvus / langchain…)的缓存是慢的那部分,
    它们不会在两次运行之间被改坏,清了纯属浪费时间。代价是项目模块每次重编译一遍(几十毫秒)。
    """
    root = Path(__file__).resolve().parents[2] / "src" / "project_rag"
    if not root.is_dir():
        return 0
    removed = 0
    for cache in root.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            pyc.unlink()
            removed += 1
    return removed


# **在装饰器执行之前就跑**:`@check` 是装饰即执行,而检查函数里是惰性 import
# `project_rag.*` 的 —— 所以模块级这一句必须在这里。
_PURGED = _purge_project_bytecode()

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARK = {PASS: "✔", FAIL: "✘", SKIP: "○"}
_results: list[tuple[str, str, str]] = []

# 反向测试改源码时的备份根目录。**镜像源码树的相对路径**放在这里:
# `.cache/reverse_backup/src/project_rag/adapters/document_loader.py`。
# 镜像而不是拍平文件名:还原时要能从备份反推出"这该写回哪个文件",不能靠猜。
_PATCH_BACKUP = Path(__file__).resolve().parents[2] / ".cache" / "reverse_backup"


def drop_bytecode(path: Path) -> None:
    """输入:源码路径。行为:删掉它对应的 ``.pyc``。输出:无。

    ``_harness`` 在 import 时会整体清一遍(见 ``_purge_project_bytecode``),所以**检查脚本**
    这条路上已经不需要它了;``patched_source`` 还原之后再清一次是给**同一个进程里接着
    读这个模块**的 runner 用的(双保险,代价是零)。
    """
    for pyc in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
        pyc.unlink()


def restore_stale_patches() -> list[str]:
    """输入:无。行为:把上次被打断时留下的源码**原件**还原回去。输出:还原了的相对路径列表。

    **为什么必须有这个**（两次真实事故):反向测试在「改坏 → 跑检查 → 还原」之间有一个窗口,
    进程在这个窗口里被杀,``finally`` 就跑不到,``src/**`` 会**留在坏版本上** ——
    而它看起来完全正常(语法没错、能 import、只是行为变了),下一次跑检查会得到一堆莫名其妙的结果:

    * 第一次:反向用例把 WS 校验改成 `if False:` 之后检查挂死 → 外层 300 秒 SIGTERM,
      `api/app.py` 里留着 `if False:`(见 STATUS §0.3 第 21 条);
    * 第二次:**把 runner 的输出管进 `head`** —— `head` 读够行数就关管道,Python 收到 SIGPIPE 当场死,
      而那一刻正好在 RT-2 的窗口里(`document_loader.py` 的排序被去掉了)。症状是下一轮报
      「RT-2 锚点出现 0 次」—— 看着像锚点过时,其实是**源码正被改坏着**。

    第二次的教训不是"记得别那么管"(那是纪律),而是**机制上要能自愈**(这是工程):
    备份先落盘、启动先扫残留。``reverse_p0_3`` / ``reverse_p0_5`` 早就有这一层,
    现在抽到这里,其余 runner 共用。
    """
    restored: list[str] = []
    if not _PATCH_BACKUP.is_dir():
        return restored
    for backup in sorted(_PATCH_BACKUP.rglob("*")):
        if not backup.is_file():
            continue
        relative = backup.relative_to(_PATCH_BACKUP)
        # 只认镜像布局,而且只认**这几个**顶层目录(``src/`` 是产品代码,``config/`` 是配置 ——
        # 阈值那条反向用例改的就是 ``config/config.yaml``)。
        # ``reverse_p0_3`` / ``reverse_p0_5`` 用的是拍平的旧布局,它们自己管自己的还原,
        # 这里不碰(免得把 `pdf_files.py` 之类写到仓库根上去)。
        if relative.parts[0] not in ("src", "config"):
            continue
        original = _PATCH_BACKUP.parents[1] / relative
        if not original.is_file():
            continue
        original.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        drop_bytecode(original)
        backup.unlink()
        restored.append(relative.as_posix())
    return restored


@contextmanager
def patched_source(path: Path, old: str, new: str) -> Iterator[None]:
    """输入:源码路径 + 锚点 + 替换文本。行为:落备份 → 替换 → 退出时还原(崩溃可自愈)。输出:无。

    **调用前自己核对锚点出现次数**:出现 0 次说明锚点过时(那条反向测试等于没跑),
    出现多次说明改的地方不对 —— 两种都必须当失败,不能"反正替换一下看看"。

    用法::

        with patched_source(path, old, new):
            red, line = run(script, needle)
    """
    original = path.read_text(encoding="utf-8")
    backup = _PATCH_BACKUP / path.resolve().relative_to(_PATCH_BACKUP.parents[1])
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(original, encoding="utf-8")
    try:
        path.write_text(original.replace(old, new), encoding="utf-8")
        drop_bytecode(path)
        yield
    finally:
        path.write_text(original, encoding="utf-8")
        drop_bytecode(path)
        backup.unlink(missing_ok=True)


class SkipCheck(Exception):
    """该检查在当前环境下无法判定(容器没起 / 语料不在本地 / 要真 GPU),或这条路还没做。

    **理由串就是报告的一部分**:必须写清「为什么跳过」并点名 **owner**(环境 / 数据装载 / 某个里程碑)。
    不允许退化成一句「跳过」—— 跳过不算通过。
    """


def record(name: str, status: str, detail: str = "") -> None:
    """只记录、不打印:打印统一放 ``report``,这样输出顺序与文件里的定义顺序一致。"""
    _results.append((name, status, detail))


def check(name: str):
    """跑一条检查并记录三态。**装饰即执行**,所以所有检查在 import 时就跑完了。

    四条 except 各有来历(每一条都对应一次真实事故):

    * ``SkipCheck`` → SKIP(不算通过);
    * ``SystemExit`` —— argparse 的用法错误抛的是它,而它继承 ``BaseException`` 而不是 ``Exception``:
      不拦就**穿透装饰器**、打断模块 import,结果是整份报告一行都打不出来(踩过);
    * ``ImportError`` —— 点名缺哪个模块,比通用分支那句裸 ``ModuleNotFoundError`` 有用得多;
    * 其余 ``Exception`` —— 一律记 FAIL。

    ``DEBUG=1`` 时把 traceback 追加进 detail(默认只给一行,报告才读得下去)。
    """

    def decorate(fn):
        try:
            detail = fn() or ""
            record(name, PASS, str(detail))
        except SkipCheck as exc:
            record(name, SKIP, str(exc))
        except SystemExit as exc:
            record(name, FAIL, f"SystemExit({exc.code})：子命令或参数还没实现，契约未落地")
        except ImportError as exc:
            record(name, FAIL, f"模块还没落地：{exc}（这一步的代码还没贴进来）")
        except Exception as exc:  # noqa: BLE001 - 验收脚本要显示任何失败
            detail = f"{type(exc).__name__}: {exc}"
            if os.environ.get("DEBUG"):
                detail += "\n" + traceback.format_exc()
            record(name, FAIL, detail)
        return fn

    return decorate


@contextmanager
def env(**kv: str | None) -> Iterator[None]:
    """临时设置环境变量;传 None 表示临时删除。"""
    saved = {key: os.environ.get(key) for key in kv}
    try:
        for key, value in kv.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def report(title: str, *, notes: Sequence[str] = (), hint: str | None = None) -> int:
    """输入:报告标题(+ 可选的额外说明)。行为:按记录顺序打印整份报告。输出:退出码(有 FAIL 就是 1)。

    ``notes`` 打在「未通过」之前(用于"这次报告是在降级模式下得的"这类全局说明),
    ``hint`` 打在最后(用于"连不上容器先看 Docker Desktop"这类修法提示)。
    """
    if os.environ.get("DEBUG"):
        print(f"(import 时清掉 {_PURGED} 个项目字节码缓存文件)")
    print(f"{title}\n" + "─" * 64)
    for name, status, detail in _results:
        suffix = f"  — {detail}" if detail else ""
        print(f"  {_MARK[status]} [{status}] {name}{suffix}")
    failed = [name for name, status, _ in _results if status == FAIL]
    skipped = [name for name, status, _ in _results if status == SKIP]
    print("─" * 64)
    print(
        f"结果：{len(_results) - len(failed) - len(skipped)} 通过 / {len(failed)} 失败 / {len(skipped)} 跳过"
    )
    for note in notes:
        print(f"\n{note}")
    if failed:
        print("\n未通过：")
        for name in failed:
            print(f"  ✘ {name}")
    if skipped:
        # 两种跳过都不算通过:① 前置条件没满足(容器没起 / 语料不在本地);
        # ② 计划内未实现 —— 但必须在理由里点名 owner。理由串就是报告。
        print("\n跳过（不算通过；理由见上方每条括注，计划内的必须点名 owner）：")
        for name in skipped:
            print(f"  ○ {name}")
    if hint:
        print(f"\n{hint}")
    return 1 if failed else 0

#!/usr/bin/env python
"""反向测试：把 BM25 指纹缓存逐个改回「坏的样子」，确认对应那条检查真的会红。

用法：

    uv run python scripts/acceptance/reverse_m3_bm25.py            # 全部
    uv run python scripts/acceptance/reverse_m3_bm25.py RT-2       # 只跑某几条

改的是 ``src/project_rag/adapters/bm25_index.py``（**原地改、`finally` 里还原**）——
这与 `reverse_m5_8.py` 同一套做法。为什么不用 `reverse_m6_deploy.py` 那种"复制到临时目录"：
这条契约验的是**真适配器**的行为，`check_m3_bm25_cache.py` 直接 import 装好的
``project_rag``（editable 指向 `src/`），复制一份配置目录骗不过 import 系统。

四个用例各自对应一条**真实的坏写法**（不是编出来像坏的版本）：

* RT-1 忘了读缓存 → 那 400ms 白省（每次检索还是回源）；
* RT-2 缓存未命中时沿用旧摘要 → **静默错误**：新入库的文档一直检索不到；
* RT-3 把缓存故障往上抛 → 缓存一挂，整个检索跟着挂（缓存本该是纯加速）；
* RT-4 存整条指纹而不是摘要 → 3938 块约 250KB 塞进 Redis。

两条断言纪律（同 `reverse_m5_8.py`）：锚点必须**恰好出现一次**（0 次 = 锚点过时，这条反向
测试等于没跑；多次 = 改的不止一处，结论不可信）；期望红的检查名片段只能**命中一行**，
且只在带 `[PASS]/[FAIL]/[SKIP]` 裁决标记的行里找。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _harness import patched_source, restore_stale_patches

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "src/project_rag/adapters/bm25_index.py"
SCRIPT = "check_m3_bm25_cache.py"

# (标题, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str]] = [
    (
        "RT-1  忘了读缓存（每查询照样回源向量库）",
        """            cached = self._safe_get()
            if cached is not None:
                return cached""",
        """            cached = None
            if cached is not None:
                return cached""",
        "TTL 内第 2、3 次检索不再问向量库要指纹",
    ),
    (
        "RT-2  缓存未命中时沿用旧摘要（语料变了永远发现不了）",
        """            if cached is not None:
                return cached
        digest = _digest(self._store.fingerprint())""",
        """            if cached is not None:
                return cached
            if self._state is not None:
                return self._state[0]
        digest = _digest(self._store.fingerprint())""",
        "TTL 过期后语料变了仍会被发现",
    ),
    (
        "RT-3  缓存故障往上抛（缓存一挂，检索跟着挂）",
        """        except Exception as e:  # noqa: BLE001 - 缓存是纯加速,任何故障都不该让检索失败
            _logger.warning("语料指纹缓存读取失败,回源向量库(每查询多付约 400ms): %s", e)
            return None""",
        """        except Exception:
            raise""",
        "缓存读/写抛异常只降级",
    ),
    (
        "RT-4  存整条指纹而不是 sha256 摘要（3938 块 ≈ 250KB 进 Redis）",
        '''    return hashlib.sha256("\\x1e".join(fingerprint).encode("utf-8")).hexdigest()''',
        '''    return "\\x1e".join(fingerprint)''',
        "进缓存的是 sha256 摘要",
    ),
]


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
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    stale = restore_stale_patches()
    if stale:
        print(f"⚠️  上次被打断时留下的坏源码,已自动还原:{stale}\n")
    original = TARGET.read_text(encoding="utf-8")
    print(f"改动目标：{TARGET.relative_to(ROOT)}（跑完在 finally 里还原）\n")
    bad = 0
    try:
        for title, old, new, needle in CASES:
            if only and not any(title.startswith(tag) for tag in only):
                continue
            hits = original.count(old)
            if hits != 1:
                print(
                    f"⚠️  {title}\n      锚点出现 {hits} 次（必须恰好 1 次）"
                    f"—— 锚点过时了，这条反向测试等于没跑"
                )
                bad += 1
                continue
            with patched_source(TARGET, old, new):
                red, line = run(needle)
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    finally:
        TARGET.write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

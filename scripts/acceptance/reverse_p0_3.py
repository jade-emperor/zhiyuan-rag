#!/usr/bin/env python
"""P0-3a 反向测试：把认证/授权逐个改回「坏的样子」，确认对应那条检查真的会红。

用法::

    uv run python scripts/acceptance/reverse_p0_3.py          # 全部
    uv run python scripts/acceptance/reverse_p0_3.py RT-4     # 只跑某几条

改的是 ``application/security.py`` 与 ``api/auth.py``（**原地改、`finally` 还原**，同 `reverse_m5_8.py`）。
十六个用例都是真实会有人那么写的坏版本（按主题分四组:认证六条 / WS 两条 / 文档四条 / 对账四条）：

* RT-1 **固定盐**（同一密码所有人一个哈希 —— 彩虹表直接命中）；
* RT-2 坏哈希串**当成通过**（库里一行脏数据 = 一把万能钥匙）；
* RT-3 ``bearer_token`` 连**裸令牌**也收（URL 里带令牌会进日志）；
* RT-4 ``check_owner`` 对**没有归属**的会话放行（老会话与瞎编的 ID 全都读得到）；
* RT-5 ``require_admin`` **不拦**（员工也能看观测报表、删文档）；
* RT-15 清理孤儿块时**顺手全清一遍**（把登记过的块也删了 —— 删少了看得见，删多了没人发现）；
* RT-16 清缺块行时**不分青红皂白**（连空清单都会删登记行）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "check_p0_3_auth.py"
# 备份放 .cache(它已经在 .gitignore 里):**改坏源码期间被 SIGKILL/SIGTERM 时 finally 跑不到**,
# 源码就会留着坏版本 —— 真踩了一次(WS 那条检查挂死 → 外层超时杀进程 →
# `if auth.enabled and ws_user is None:` 变成了 `if False:`)。所以:改之前先落一份备份,
# 启动时先检查有没有残留的备份并还原,再开始跑。
BACKUP_DIR = ROOT / ".cache" / "reverse_backup"

CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  固定盐（同一密码两次哈希相同）",
        "src/project_rag/application/security.py",
        "    salt = secrets.token_bytes(_SALT_BYTES)",
        '    salt = b"0123456789abcdef"',
        "密码哈希：自带参数、盐随机",
    ),
    (
        "RT-2  坏哈希串当成通过（脏数据变万能钥匙）",
        "src/project_rag/application/security.py",
        """    except (ValueError, AttributeError):
        return False""",
        """    except (ValueError, AttributeError):
        return True""",
        "密码哈希：自带参数、盐随机",
    ),
    (
        "RT-3  bearer_token 连裸令牌也收",
        "src/project_rag/application/security.py",
        """    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()""",
        """    if scheme.lower() != "bearer":
        return header.strip()
    if not value.strip():
        return None
    return value.strip()""",
        "令牌只认 Bearer",
    ),
    (
        "RT-4  对没有归属的会话放行（老会话与瞎编的 ID 全都读得到）",
        "src/project_rag/api/auth.py",
        '        if owner is None:\n            return "这段会话没有归属记录(可能是加认证之前建的,或会话 ID 不存在)"',
        "        if owner is None:\n            return None",
        "会话归属：别人的会话读不到",
    ),
    (
        "RT-14  回填把状态写成 pending（前端显示处理中，对账又把它报成缺块）",
        "src/project_rag/application/reconcile.py",
        '            status="processed",',
        '            status="pending",',
        "对账 回填：",
    ),
    (
        "RT-15  清理孤儿块时「顺手全清一遍」（把登记过的块也删了）",
        "src/project_rag/application/reconcile.py",
        "    return tuple((doc_id, store.delete_by_doc_id(doc_id)) for doc_id in doc_ids)",
        "    return tuple(\n"
        "        (chunk.doc_id, store.delete_by_doc_id(chunk.doc_id))\n"
        "        for chunk in store.load_all()\n"
        "    )",
        "对账 清理入口：",
    ),
    (
        "RT-16  清缺块行时不分青红皂白（空清单也把登记行删掉）",
        "src/project_rag/application/reconcile.py",
        "    return tuple((doc_id, store.remove(doc_id)) for doc_id in doc_ids)",
        "    return tuple(\n"
        "        (record.document_id, store.remove(record.document_id))\n"
        "        for record in store.list_all()\n"
        "    )",
        "对账 清理入口：",
    ),
    (
        "RT-13  对账把 pending 也算成缺块（天天误报，真问题被淹掉）",
        "src/project_rag/application/reconcile.py",
        '            if record.status == "processed" and chunk_counts.get(record.document_id, 0) == 0',
        '            if chunk_counts.get(record.document_id, 0) == 0',
        "对账：孤儿块与缺块",
    ),
    (
        "RT-12  文件名不做 basename（../x.md 能写到 uploads 目录外面）",
        "src/project_rag/api/app.py",
        "        raw_name = Path(filename).name  # ← 只取 basename:防路径穿越",
        "        raw_name = filename",
        "上传：文件名只取 basename",
    ),
    (
        "RT-11  登记 ID 与装载器 doc_id 不一致（删除会静默删不掉，留下孤儿块）",
        "src/project_rag/application/documents.py",
        "    document_id = Path(filename).as_posix()",
        '    document_id = f"doc-{Path(filename).name}"',
        "文档 ID 口径",
    ),
    (
        "RT-8  删文档只删登记行、不删向量（库里留下孤儿块）",
        "src/project_rag/application/documents.py",
        "    deleted_chunks = vector_store.delete_by_doc_id(document_id)",
        "    deleted_chunks = 0",
        "文档删除：**先标记 deleting",
    ),
    (
        "RT-9  删向量失败也照样删登记行（表和库从此不一致）",
        "src/project_rag/application/documents.py",
        "    deleted_chunks = vector_store.delete_by_doc_id(document_id)",
        "    try:\n        deleted_chunks = vector_store.delete_by_doc_id(document_id)\n    except Exception:\n        deleted_chunks = 0",
        "删向量失败就抛",
    ),
    (
        "RT-10  删除不要求管理员（员工也能删库里的文档）",
        "src/project_rag/api/app.py",
        '        auth.require_admin(user)\n        outcome = await _call(_delete_document, deps, document_id)',
        '        outcome = await _call(_delete_document, deps, document_id)',
        "文档接口：",
    ),
    (
        "RT-6  WebSocket 不校验令牌（任何人都能连上来问）",
        "src/project_rag/api/app.py",
        "        if auth.enabled and ws_user is None:",
        "        if False:",
        "WebSocket：无令牌连不上",
    ),
    (
        "RT-7  WebSocket 不做归属检查（拿别人的 session_id 继续问）",
        "src/project_rag/api/app.py",
        "                violation = await auth.owner_violation(request.session_id, ws_user)",
        "                violation = None",
        "WebSocket：带令牌但用别人的会话",
    ),
    (
        "RT-5  require_admin 不拦（员工也能看报表）",
        "src/project_rag/api/auth.py",
        """        if user is not None and not user.is_admin:""",
        """        if False:""",
        "角色：报表只有管理员能看",
    ),
]


def _backup_path(rel: str) -> Path:
    """输入:相对路径。行为:算备份文件路径(把斜杠压成下划线)。输出:路径。"""
    return BACKUP_DIR / rel.replace("/", "__")


def recover_leftovers() -> list[str]:
    """输入:无。行为:把上次没还原干净的源码还原回去,并删掉备份。输出:还原过的相对路径列表。"""
    if not BACKUP_DIR.exists():
        return []
    recovered: list[str] = []
    for backup in sorted(BACKUP_DIR.glob("*.py")):
        rel = backup.name.replace("__", "/")
        (ROOT / rel).write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
        recovered.append(rel)
    return recovered


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑契约脚本、只看带裁决标记的行。输出:(是否 FAIL, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, f"scripts/acceptance/{SCRIPT}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,  # 检查挂死时要早点回来 —— 见 recover_leftovers 的注释
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
    recovered = recover_leftovers()
    if recovered:
        print(f"⚠️  上次没还原干净,已自动还原:{recovered}\n")
    originals: dict[str, str] = {}
    for _, rel, _, _, _ in CASES:
        originals.setdefault(rel, (ROOT / rel).read_text(encoding="utf-8"))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"改动目标：{len(originals)} 个文件（跑完在 finally 里还原）\n")
    bad = 0
    try:
        for title, rel, old, new, needle in CASES:
            if only and not any(title.startswith(tag) for tag in only):
                continue
            path = ROOT / rel
            original = originals[rel]
            hits = original.count(old)
            if hits != 1:
                print(f"⚠️  {title}\n      锚点在 {rel} 里出现 {hits} 次（必须恰好 1 次）")
                bad += 1
                continue
            backup = _backup_path(rel)
            backup.write_text(original, encoding="utf-8")  # 崩溃也能还原
            path.write_text(original.replace(old, new), encoding="utf-8")
            try:
                red, line = run(needle)
            finally:
                path.write_text(original, encoding="utf-8")
                backup.unlink(missing_ok=True)
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    finally:
        for rel, original in originals.items():
            (ROOT / rel).write_text(original, encoding="utf-8")
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏代码")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

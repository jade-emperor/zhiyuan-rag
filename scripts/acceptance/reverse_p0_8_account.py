"""账号自服务契约的反向测试:把实现改坏一次,确认对应的那条检查会红。

用法：

    .venv/bin/python scripts/acceptance/reverse_p0_8_account.py

每条都要么「红了」,要么报出锚点过时 —— 不红的说明那条检查是摆设。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import patched_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/acceptance/check_p0_8_account.py"


def run(needle: str) -> tuple[bool, str]:
    """输入:期望红的检查名片段。行为:跑检查、只看带裁决标记的行。输出:(是否真红了, 那一行)。"""
    proc = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, timeout=600)
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行,反向测试无法判定:{hits}"
    return ("[FAIL]" in hits[0]), hits[0]


CASES = [
    (
        "验证码取用不删除(同一张图可以重复用)",
        ROOT / "src/project_rag/adapters/captcha_memory.py",
        "item = self._items.pop(ticket, None)",
        "item = self._items.get(ticket, None)",
        "一次一票",
    ),
    (
        "验证码不比对答案(填什么都放行)",
        ROOT / "src/project_rag/application/captcha.py",
        "    if expected.strip().casefold() != answer.strip().casefold():",
        "    if False:",
        "输错也作废",
    ),
    (
        "改密不撤销旧令牌(改完密码旧设备照样能用)",
        ROOT / "src/project_rag/api/auth.py",
        "await run_in_threadpool(store.revoke_all_tokens, user.user_id)",
        'await run_in_threadpool(store.revoke_token, "")',
        "改密:原密码错",
    ),
    (
        "关掉自助注册也照建号",
        ROOT / "src/project_rag/api/app.py",
        "        if not settings.api.registration.enabled:",
        "        if False:",
        "关闭自助注册",
    ),
    (
        "登录页不再预填上次账号(每次都要重打)",
        ROOT / "web/src/views/LoginView.vue",
        "const username = ref(storedUsername() ?? '')",
        "const username = ref('')",
        "表单错误按字段定位",
    ),
    (
        "深色变量块被改名(切主题只换一半颜色)",
        ROOT / "web/src/styles/tokens.css",
        ":root[data-theme='dark']",
        ":root[data-theme='dusk']",
        "深浅色主题",
    ),
]


def main() -> int:
    """输入:无。行为:逐条打补丁 → 跑检查 → 还原。输出:退出码(有没红的就 1)。"""
    bad = 0
    for title, path, old, new, needle in CASES:
        hits = path.read_text(encoding="utf-8").count(old)
        if hits != 1:
            print(
                f"⚠️  {title}\n      锚点在 {path.relative_to(ROOT)} 里出现 {hits} 次(必须恰好 1 次)"
                f" —— 锚点过时了,这条反向测试等于没跑"
            )
            bad += 1
            continue
        with patched_source(path, old, new):
            red, line = run(needle)
        if not red:
            bad += 1
        print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")

    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏实现")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""M6.4 反向测试：把部署配置逐个改回「坏的样子」，确认对应那条检查真的会红。

用法::

    uv run python scripts/acceptance/reverse_m6_deploy.py           # 全部
    uv run python scripts/acceptance/reverse_m6_deploy.py RT-3     # 只跑某几条

**为什么不是原地改**：`reverse_m5_8.py` 改的是 `src/*.py`（改完 `finally` 里还原）。
这份检查读的是 `docker-compose.yaml` / `Dockerfile` / `pyproject.toml` / `uv.lock` /
`.dockerignore` —— 都是作者手写的、也是构建真正吃进去的文件，原地改坏一次（哪怕有 finally）
风险不对称。所以这里换一种做法：把这几份文件**复制到临时目录**，用 `RAG_CHECK_ROOT`
把检查指过去，改副本、跑检查、断言那条检查红了。真实仓库一个字节都不动。

两个断言纪律（与 `reverse_m5_8.py` 同源）：

* 锚点必须在该文件里**恰好出现一次** —— 出现 0 次说明文件变了、这条反向测试已经失效
  （假绿：它什么都没改，检查当然是绿的）；出现多次说明改的不止一处，结论不可信。
* 期望红的**检查名片段只能命中一行**（只在带 `[PASS]/[FAIL]/[SKIP]` 裁决标记的行里找）。
  命中多行就不敢判定 —— 报告末尾还会把失败的检查名列一遍，不排除那些行每条都会命中两行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/acceptance/check_m6_deploy.py"
FILES = (
    "docker-compose.yaml",
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
    ".dockerignore",
    ".env.example",
    # 第六节（端口分层）读的这几份：dev.sh 会被**真的执行**（用假 uv），
    # 所以临时目录里必须有一份完整的、可运行的 dev.sh，而不是只抄出被改的那几行。
    "scripts/dev.sh",
    "web/.env.development",
    "config/config.yaml",
    "API.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
)

# (标题, 相对路径, 锚点（恰好 1 次）, 改成, 期望红的检查名片段)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "RT-1  Milvus 地址写回宿主（127.0.0.1）",
        "docker-compose.yaml",
        "RAG_MILVUS__HOST: standalone",
        "RAG_MILVUS__HOST: 127.0.0.1",
        "连中间件用服务名",
    ),
    (
        "RT-2  MySQL 端口写成宿主映射的 10093",
        "docker-compose.yaml",
        'RAG_MYSQL__PORT: "3306"',
        'RAG_MYSQL__PORT: "10093"',
        "连中间件用服务名",
    ),
    (
        "RT-3  Redis 也要 service_healthy（它没有 healthcheck）",
        "docker-compose.yaml",
        "      redis:\n        condition: service_started",
        "      redis:\n        condition: service_healthy",
        "condition 与目标服务",
    ),
    (
        "RT-4  MySQL 不等健康（它有 healthcheck，首启会连不上）",
        "docker-compose.yaml",
        "      mysql:\n        condition: service_healthy",
        "      mysql:\n        condition: service_started",
        "condition 与目标服务",
    ),
    (
        "RT-5  宿主端口映射指向容器内不存在的端口",
        "docker-compose.yaml",
        '- "8080:8080"',
        '- "8080:8000"',
        "EXPOSE",
    ),
    (
        "RT-6  模型挂载没落在镜像的 WORKDIR 下",
        "docker-compose.yaml",
        "- ./models:/app/models:ro",
        "- ./models:/models:ro",
        "只读挂载",
    ),
    (
        "RT-7  镜像 tag 漂成 latest",
        "docker-compose.yaml",
        "image: project-rag:0.1.0",
        "image: project-rag:latest",
        "镜像 tag 是固定值",
    ),
    (
        "RT-8  密钥不从 .env 注入（改指模板文件）",
        "docker-compose.yaml",
        "    env_file:\n      - .env",
        "    env_file:\n      - .env.example",
        "密钥从 .env 注入",
    ),
    (
        "RT-9  服务集合少了 attu（compose 与文档不一致）",
        "docker-compose.yaml",
        "  attu:\n    image: zilliz/attu:v2.4.9",
        "  attu_off:\n    image: zilliz/attu:v2.4.9",
        "服务集合",
    ),
    (
        "RT-10 unstructured 回到运行时依赖（构建期又要碰 GitHub）",
        "pyproject.toml",
        '    "pdfminer-six>=20260107",\n]',
        '    "pdfminer-six>=20260107",\n    "unstructured>=0.16,<1.0",\n]',
        "运行时依赖里没有",
    ),
    (
        "RT-11 可选档被改名（`uv sync --extra unstructured` 失效）",
        "pyproject.toml",
        # 锚点只取那一行:原来带着 `[project.optional-dependencies]` 头一起匹配,
        # 2026-10-06 加 `desktop` 那一档时插在头与它之间,锚点当场漂了(机制抓到了)
        "unstructured = [\n",
        "unused = [\n",
        "那一档在 optional-dependencies 里",
    ),
    (
        "RT-12 锁没跟着重锁（provides-extras 丢了）",
        "uv.lock",
        'provides-extras = ["desktop", "unstructured"]',
        'provides-extras = []',
        "uv.lock 与 pyproject 一致",
    ),
    (
        "RT-13 .dockerignore 漏掉 .env（密钥会进镜像层）",
        ".dockerignore",
        "注入)\n.env\n",
        "注入)\n",
        "镜像不带密钥",
    ),
    (
        "RT-14 Dockerfile 去掉 USER（退化成 root 跑）",
        "Dockerfile",
        "USER app\n",
        "",
        "镜像不带密钥",
    ),
    (
        "RT-15 Dockerfile 退化成单阶段（runtime 那个 FROM 没了）",
        "Dockerfile",
        "FROM python:3.13-slim AS runtime",
        "FROM python:3.13-slim",
        "两阶段",
    ),
    (
        "RT-16 加回 `# syntax=`（构建第一步就要去 registry 拉 frontend）",
        "Dockerfile",
        "# =============================================================================\n# project-rag 应用镜像(服务化)",
        "# syntax=docker/dockerfile:1.7\n# =============================================================================\n# project-rag 应用镜像(服务化)",
        "不用外挂 frontend",
    ),
    (
        "RT-17 uv 换回从 ghcr.io 拷（国内经常连不上）",
        "Dockerfile",
        'RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv==0.12.11',
        "COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/",
        "不拉 ghcr.io",
    ),
    (
        "RT-18 把 GPU 预约删掉、只留 device=cuda（/health 照样绿，第一次真提问才炸）",
        "docker-compose.yaml",
        "    gpus: all\n",
        "",
        "推理设备与 GPU 预约",
    ),
    (
        "RT-19 删掉静态元数据声明（解析期又得去 github 取那个 wheel，46s 就失败）",
        "pyproject.toml",
        '    { name = "en-core-web-sm", version = "3.8.0" },\n',
        "",
        "静态元数据",
    ),
    (
        "RT-20 Dockerfile 不拷 README/LICENSE（构建死在最后一层：Readme file does not exist）",
        "Dockerfile",
        "COPY LICENSE README.md ./\n",
        "",
        "拷齐了 pyproject 声明的",
    ),
    # ── 端口分层（第六节）：这一组的翻车形状是「注释/文档与代码分家」 ──────────────
    (
        "RT-21 dev.sh 的注释把代码后端写成别的端口（注释 18090、代码 18080 —— 本轮翻车的原形状）",
        "scripts/dev.sh",
        "（默认 18080；不碰前端、不用镜像）",
        "（默认 18090；不碰前端、不用镜像）",
        "端口分层",
    ),
    (
        "RT-22 代码后端端口改成容器那个（两个后端只能活一个，裸 uv run 会 Errno 98）",
        "scripts/dev.sh",
        "DEV_BACKEND_PORT=18080",
        "DEV_BACKEND_PORT=8080",
        "端口分层",
    ),
    (
        "RT-23 web/.env.development 指回容器端口（vite 调前端时会打到容器那份上）",
        "web/.env.development",
        "VITE_DEFAULT_BACKEND=http://127.0.0.1:18080",
        "VITE_DEFAULT_BACKEND=http://127.0.0.1:8080",
        "默认后端 = 代码后端端口",
    ),
    (
        "RT-24 代码后端与容器共用一个日志目录（两个进程轮转同一个 app.log，会丢行）",
        "scripts/dev.sh",
        'DEV_LOG_DIR="logs/dev"',
        'DEV_LOG_DIR="logs"',
        "写自己的日志目录",
    ),
    (
        "RT-25 把端口守卫摘掉（又变回等 uvicorn 抛 Errno 98）",
        "scripts/dev.sh",
        '  ensure_port_free "$port" || exit 3',
        '  true  # ensure_port_free "$port" || exit 3',
        "起服务前的守卫",
    ),
    (
        "RT-26 报 bug 模板写回「裸 uv run project-rag-api + 写死端口」（教人 curl 错端口）",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        "- 容器里的 app：`docker compose up -d`（8080，页面和接口同一个进程）",
        "- 宿主上 `uv run project-rag-api`（18080）",
        "报 bug 的模板",
    ),
    (
        "RT-27 API.md 的配置表把 api.port 默认值写成别的（文档说的与代码跑的不是一个口）",
        "API.md",
        "| `api.host` / `api.port` | `0.0.0.0` / `18080` |",
        "| `api.host` / `api.port` | `0.0.0.0` / `8080` |",
        "端口分层",
    ),
    (
        "RT-28 config.yaml 的 api.port 改回容器口（裸跑又和容器抢；2026-10-08 作者定的是 18080）",
        "config/config.yaml",
        "  port: 18080",
        "  port: 8080",
        "端口分层",
    ),
    (
        "RT-29 删掉上传目录那条可写挂载（容器里的「上传文档」又必然 EROFS）",
        "docker-compose.yaml",
        "      - ./data/uploads:/app/data/uploads\n",
        "",
        "上传目录与 logs 可写",
    ),
]


def run(needle: str, root: Path) -> tuple[bool, str]:
    """输入:期望红的检查名片段 + 被检查的临时仓库根。输出:(是否真红了, 那一行)。"""
    proc = subprocess.run(
        [sys.executable, str(CHECK)],
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(os.environ, RAG_CHECK_ROOT=str(root)),
    )
    marks = ("[PASS]", "[FAIL]", "[SKIP]")
    body = [line.strip() for line in proc.stdout.splitlines() if any(m in line for m in marks)]
    hits = [line for line in body if needle in line]
    if len(hits) != 1:
        return False, f"⚠️ 片段 {needle!r} 命中了 {len(hits)} 行，反向测试无法判定：{hits}"
    if proc.returncode == 0:
        return False, f"⚠️ 那条红了但整份检查仍然退出 0（报告口径坏了）：{hits[0]}"
    return "[FAIL]" in hits[0], hits[0]


def main() -> int:
    only = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    workdir = Path(tempfile.mkdtemp(prefix="m6_deploy_reverse_"))
    pristine = {}
    for name in FILES:
        target = workdir / name
        target.parent.mkdir(parents=True, exist_ok=True)  # scripts/、web/、.github/… 这些子目录
        shutil.copyfile(ROOT / name, target)
        pristine[name] = target.read_text(encoding="utf-8")
    print(f"临时仓库根：{workdir}（真实仓库只读，一个字节都不动）\n")
    bad = 0
    try:
        for title, name, old, new, needle in CASES:
            if only and not any(title.startswith(tag) for tag in only):
                continue
            path = workdir / name
            original = pristine[name]
            hits = original.count(old)
            if hits != 1:
                print(
                    f"⚠️  {title}\n      锚点在 {name} 里出现 {hits} 次（必须恰好 1 次）"
                    f"—— 锚点过时了，这条反向测试等于没跑"
                )
                bad += 1
                continue
            path.write_text(original.replace(old, new), encoding="utf-8")
            try:
                red, line = run(needle, workdir)
            finally:
                path.write_text(original, encoding="utf-8")
            if not red:
                bad += 1
            print(f"{'✔ 红了(检查有效)' if red else '✘ 还是绿的(检查是摆设)'}  {title}\n      {line}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("─" * 68)
    print("全部反向测试都红了" if not bad else f"{bad} 条没红 —— 那些检查抓不到坏配置")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

# 故意**不写** `# syntax=docker/dockerfile:1.7`:
#   写它 = 让 BuildKit 先去 registry 拉一个 frontend 镜像(docker.io/docker/dockerfile:1.7),
#   那是整个构建的**第一个网络请求**,国内网络 / 代理一坏就死在这一行。2026-09-30 实测:
#     failed to resolve source metadata for docker.io/docker/dockerfile:1.7:
#     ... dial tcp 127.0.0.1:7897: connectex: No connection could be made ...
#   本文件只用 `RUN --mount=type=cache` 与多阶段 COPY —— **内置 frontend 全都支持**
#   (cache mount 从 frontend 1.2 起就是稳定语法),所以不需要外挂 frontend。
#   代价:引擎太老时 `--mount=type=cache` 会报语法错 —— 那时删掉那两个挂载(见文末),
#   **不要**加回 `# syntax=`。
# =============================================================================
# project-rag 应用镜像(服务化)
#
# 把「服务本体」装进镜像;Milvus / MySQL / Redis 是**外部依赖**,不进这个镜像
# (它们用 docker-compose.yaml 起,见 README「容器里跑服务」)。
#
# 构建:
#   docker build -t project-rag:0.1.0 .                 # 在项目根执行
#
# 跑(**推荐:就在 compose 里**)—— `docker-compose.yaml` 的 `app` 服务已经把服务名地址
# (standalone:19530 / mysql:3306 / redis:6379)、挂载、depends_on 健康条件都配好了:
#   docker compose up -d --build
#   docker compose ps                                   # app 那行要 healthy(首启最多等 3 分钟)
#
# 手工 `docker run`(中间件在**宿主**上,所以地址得用 host.docker.internal + 1008x/1009x):
#   docker run --rm -p 8080:8080 \
#     -e RAG_LLM__DEEPSEEK_API_KEY=sk-xxxx \
#     -e RAG_MYSQL__HOST=host.docker.internal -e RAG_MYSQL__PORT=10093 \
#     -e RAG_REDIS__HOST=host.docker.internal -e RAG_REDIS__PORT=10087 \
#     -e RAG_MILVUS__HOST=host.docker.internal -e RAG_MILVUS__PORT=10086 \
#     -v "$PWD/models:/app/models:ro" -v "$PWD/data:/app/data:ro" \
#     -v "$PWD/logs:/app/logs" \
#     project-rag:0.1.0
#
# 依赖分了两档:运行时(进这个镜像)与可选 `unstructured`(**不进**)—— 后者那一档里
# `en-core-web-sm` 的 wheel 只有 GitHub 一个来源,留在运行时依赖里会让 `uv sync --locked`
# 在构建时去 github.com 取一个产品路径不用的模型。加/减依赖见 pyproject.toml 的注释。
#
# 三个必须知道的事实:
#
# 1. **模型与语料不进镜像**:`models/`(4.4G)与 `data/` 挂卷。镜像只装代码与依赖。
#    挂进来后**路径与宿主一致**(`/app/models/bge-m3`),因为配置里的相对路径是按
#    **项目根**解析的(config.PROJECT_ROOT),不是按 CWD。
# 2. **默认按 CPU 跑**(`RAG_EMBEDDING__DEVICE=cpu`):容器里看不见宿主 GPU。
#    要用 GPU:`docker run --gpus all -e RAG_EMBEDDING__DEVICE=cuda`,基础镜像换成
#    `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04` 之类(本文件不提供,没在真机上验过)。
# 3. **镜像大(~9G)**:`uv.lock` 里 torch 走的是 PyPI 的 CUDA 版,会拖进 nvidia-*
#    (实测本地 `.venv` 7.1G,其中 nvidia 3.2G + triton 0.9G)。CPU-only 能砍到 ~1.5G,
#    但那条路要 `uv pip install --torch-backend=cpu`(重新解析 + 走 pytorch 官方 index),
#    **与 `--frozen` 互斥**、也没在真机上验证过,所以这里先保证「构建必成功且与本地一致」。
#    要瘦身见文末注释。
# =============================================================================

# -----------------------------------------------------------------------------
# 一、builder:装依赖(带 uv 缓存挂载),产物是 /app/.venv
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# uv 用 pip 装(源可用 --build-arg 覆盖),**故意不用**
# `COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/`:ghcr.io 在国内经常连不上,
# 而这条链上只需要 `uv sync`(uvx 用不到);版本照样钉死(与本地开发同一条 0.12.11)。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv==0.12.11

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 依赖层单独一层:先只 COPY 依赖清单,改 src/ 不会让「装依赖」这层失效(它最慢)。
# `--locked` 而不是 `--frozen`:前者会在 pyproject 与 uv.lock 不一致时**直接构建失败** ——
# 那正是我们要的(静默装上一份过期的依赖比报错难查得多)。加依赖记得先 `uv lock`。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# 再放代码,装项目本体(editable 指向 /app/src)
COPY src/ src/
COPY config/ config/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# -----------------------------------------------------------------------------
# 二、runtime:只要 venv + 代码,不要 uv、不要构建缓存
# -----------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# 非 root 跑:语料与模型是只读挂载,只有 /app/logs 需要写
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/logs \
    && chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/config /app/config

# 默认值只放「容器里必然如此」的:设备(看不见宿主 GPU)、绑定地址(要映射出去)。
# 密钥 / 数据库地址**不进镜像** —— 由 `-e` 或 compose 的 env_file 注入。
# (没装 curl / ca-certificates:python 官方 slim 镜像自带 ca-certificates,
#  所以调 https://api.deepseek.com 没问题;探活也用 python 而不是 curl。)
ENV RAG_EMBEDDING__DEVICE=cpu \
    RAG_API__HOST=0.0.0.0 \
    RAG_API__PORT=8080

USER app
EXPOSE 8080

# 探活打的是 /health(不探中间件 —— 那是我 check_env_services 的活)。
# start-period 给足:CPU 上启动预热要加载 BGE-M3 + 重排模型(实测数十秒)。
# 用 python 而不是 curl:镜像里没装 curl,不为一条探活再装一个包。
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"]

# 用 CMD 而不是 ENTRYPOINT:这样 `docker run <镜像> project-rag ingest --path …`
# 能直接把命令行换成一次性任务(入库不需要起服务)。
CMD ["project-rag-api"]

# =============================================================================
# 想瘦身(未在真机验证,验过再改成默认):
#
#   1) torch 换 CPU 轮子 —— 省 ~4G:
#        RUN uv export --frozen --no-dev --no-emit-project -o /tmp/req.txt \
#         && uv venv /app/.venv \
#         && uv pip install --python /app/.venv/bin/python --torch-backend=cpu -r /tmp/req.txt
#      代价:重新解析依赖(不再严格等于 uv.lock)、多一个 index(要能连 download.pytorch.org)。
#
#   2) 用不到 OCR / 图片那一路时,可从 pyproject 摘掉 `opencv-python` /`rapidocr-onnxruntime`
#      / `PyMuPDF` 之外的重包(摘之前先确认没有别的代码 import)。
#
#   3) 构建缓存挂载要求 BuildKit(docker buildx / Docker Desktop 默认开)。
#      老引擎会把 `--mount=type=cache` 当语法错误 —— 那时删掉这两个挂载即可,
#      **不要**加回 `# syntax=docker/dockerfile:1.7`(那会多一次 registry 请求,见文件头)。
#
#   4) 构建期**不需要** github.com / ghcr.io:依赖走 pyproject 里的清华源,uv 自身走 pip 清华源。
#      在能直连 ghcr 的机器上想换回官方镜像,把上面那两行 pip 换回
#      `COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/` 即可。
# =============================================================================

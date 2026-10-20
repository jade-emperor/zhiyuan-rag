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
# 依赖只有一档:**运行时**。`unstructured` / `en-core-web-sm` 那一档已整块删除 ——
# 它的 wheel 只有 GitHub 一个来源,uv 任何一次重新解析都要去取(实测 172s,默认 46s 放弃),
# 于是 `docker build` 与日常 `uv run` 会随机死在网络上,而 `src/` 里 0 处 import 它。
# 加依赖见 pyproject.toml 的注释;**别再把 direct-URL 依赖加回来**。
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
FROM python:3.14-slim AS builder

# 国内构建优先走镜像源;要换回官方源就 `--build-arg DEBIAN_MIRROR=deb.debian.org` 覆盖。
ARG DEBIAN_MIRROR=mirrors.aliyun.com

# `oss2` 依赖 `crcmod`,而 crcmod 在 PyPI 只有 sdist(没有 manylinux wheel);
# 它可选编译 C 扩展,但 setup.py 仍需要编译器。builder 里补 gcc,运行层不带它。
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# uv 用 pip 装(源可用 --build-arg 覆盖),**故意不用**
# `COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/`:ghcr.io 在国内经常连不上,
# 而这条链上只需要 `uv sync`(uvx 用不到);版本照样钉死(与本地开发同一条 0.12.11)。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv==0.12.11

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_HTTP_TIMEOUT=300

# UV_HTTP_TIMEOUT=300(默认 30):镜像要在国内网络上拉 torch 这类大包,30s 不够。
# 这条只是**网络宽容度**,不是任何依赖的来源问题 —— 依赖全部来自 PyPI 镜像。

WORKDIR /app

# 依赖层单独一层:先只 COPY 依赖清单,改 src/ 不会让「装依赖」这层失效(它最慢)。
# `--locked` 而不是 `--frozen`:前者会在 pyproject 与 uv.lock 不一致时**直接构建失败** ——
# 那正是我们要的(静默装上一份过期的依赖比报错难查得多)。加依赖记得先 `uv lock`。
COPY pyproject.toml uv.lock ./

# `--build-arg TORCH_BACKEND=cpu` = 装 CPU 版 torch,**镜像能小一半以上**(见文末"想瘦身"第 1 条)——
# 代价是**容器里再也不能用 GPU**(compose 的 `gpus: all` + `RAG_EMBEDDING__DEVICE=cuda` 会失败,
# 那就是预热静默失败那条老路)。所以默认仍是 `cuda`:默认配置与 compose 的 GPU 路径一致,
# 想瘦身的部署自己显式选 cpu。实测（GPU 版镜像内):
#   venv 合计 7.0G,其中 nvidia 3.2G + torch 1.2G + triton 0.9G = **5.3G 是 CUDA 相关**;
#   CPU 版这条路走 `uv export` + `uv pip install --torch-backend=cpu`。
ARG TORCH_BACKEND=cuda
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$TORCH_BACKEND" = "cpu" ]; then \
      uv export --locked --no-dev --no-emit-project -o /tmp/req.txt \
      && uv venv /app/.venv \
      && uv pip install --python /app/.venv/bin/python --torch-backend=cpu -r /tmp/req.txt ; \
    else \
      uv sync --locked --no-dev --no-install-project ; \
    fi

# 再放代码,装项目本体(editable 指向 /app/src)
COPY src/ src/
COPY config/ config/
# LICENSE 与 README 也要进来:pyproject 里声明了 `license-files = ["LICENSE"]` 与
# `readme = "README.md"`（开源化加的),缺任何一个 hatchling 在**这一步构建元数据**
# 时就会报 "Readme file does not exist" / "license file not found" —— 实测踩过一次。
# 放在这一层而不是上面那层:改版权年份/改 README 不该让 5G 的依赖层失效。
COPY LICENSE README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# -----------------------------------------------------------------------------
# 二、runtime:只要 venv + 代码,不要 uv、不要构建缓存
# -----------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

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
# 想瘦身:
#
#   1) torch 换 CPU 轮子 —— 实测可省的量与代价见上面 `ARG TORCH_BACKEND` 那段。
#      用法(默认仍是 GPU 版,**必须显式选**):
#        docker build --build-arg TORCH_BACKEND=cpu -t project-rag:0.1.0-cpu .
#      代价:① 容器里再也不能用 GPU(compose 的 `gpus: all` / `device=cuda` 会失败);
#            ② 装依赖不再走 `uv sync --locked`,而是 `uv export`(锁仍被校验) + `uv pip install`,
#               并且要多连一个 index(`download.pytorch.org`,国内网络常常连不上)。
#
#   2) 用不到 OCR / 图片那一路时,可从 pyproject 摘掉 `opencv-python` /`rapidocr-onnxruntime`
#      / `PyMuPDF` 之外的重包(摘之前先确认没有别的代码 import)。实测这些也不小:
#      pyarrow 156M / opencv 116M+72M / transformers 115M / scipy 109M / kubernetes 82M。
#
#   3) 构建缓存挂载要求 BuildKit(docker buildx / Docker Desktop 默认开)。
#      老引擎会把 `--mount=type=cache` 当语法错误 —— 那时删掉这两个挂载即可,
#      **不要**加回 `# syntax=docker/dockerfile:1.7`(那会多一次 registry 请求,见文件头)。
#
#   4) 构建期**不需要** ghcr.io，也**不需要** github.com(依赖全部来自 PyPI 镜像)。
#      在能直连 ghcr 的机器上想换回官方镜像,把上面那两行 pip 换回
#      `COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/` 即可。
# =============================================================================

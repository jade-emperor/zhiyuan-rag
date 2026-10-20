# MinerU 独立解析服务镜像。
#
# 说明:
# - MinerU 很重(模型/OCR 依赖/GPU 运行时),基础镜像与版本要按你的部署环境选;
#   这里用官方 pip 包 `mineru[core]` 作为起点,构建时间与镜像体积都会很大。
# - 模型缓存默认落在 /root/.cache,compose 里应挂一个持久卷,否则每次重建都重新下载。
FROM python:3.12-slim

# 国内构建优先走镜像源;要换回官方源就 `--build-arg` 覆盖。
ARG DEBIAN_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINERU_OUTPUT_DIR=/data/mineru \
    MINERU_BIN=mineru \
    MINERU_MODEL_SOURCE=modelscope \
    MINERU_DEVICE_MODE=cpu

# Debian 12/13 用 `debian.sources`;12 之前可能是 `sources.list`。两处都 sed,没有也不报错。
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

# MinerU 本体。**钉 2.6.4**:`content_list` 字段与 CLI(`mineru -p ... -o ...`)
# 是这套适配器验证过的形状;换 4.x 前先跑一份带图 PDF 看输出目录与 CLI 是否变。
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" "mineru[core]==2.6.4"

COPY app.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

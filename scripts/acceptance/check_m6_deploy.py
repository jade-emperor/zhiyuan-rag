#!/usr/bin/env python
"""部署契约：应用编进 ``docker-compose.yaml`` 之后，那份配置必须自洽。

用法：

    uv run python scripts/acceptance/check_m6_deploy.py

**这份检查证明什么、不证明什么**（先说清楚，免得被当成"镜像能构建"）：

* 证明：`docker-compose.yaml` / `Dockerfile` / `pyproject.toml` / `uv.lock` / `.dockerignore`
  这几份**静态文件之间彼此对得上** —— 地址用服务名、端口用容器内端口、挂载点落在镜像的
  WORKDIR 下、依赖档位没把构建期要联网的东西塞回运行时。
  第六节还把 `scripts/dev.sh` / `web/.env.development` / `config/config.yaml` /
  `.github/ISSUE_TEMPLATE/bug_report.yml` 拉进来核对**宿主代码后端与容器的端口分层**
  （两者必须各占一个端口；文档里写的端口必须就是代码里跑的那个）—— 它对应的翻车是
  「dev.sh 注释写 18080、代码跑 8080，裸 `uv run project-rag-api` 直接 Errno 98」。
* **不证明**：镜像在**你这一台**机器上也能构建、能起来。那要跑
  `docker compose up -d --build` + `docker compose ps`（app 要是 `healthy`）。
  这里做的是"把能静态查出来的错先查掉"；镜像在作者那台机器上构建并实测过（README §8），
  但换机器 / 换网络 / 换依赖缓存之后仍要自己跑一遍。

为什么这些检查值得存在（三条都对应一次真实的坑）：

1. **地址与端口是最容易写错、又最难看出来的一处**：容器里 `127.0.0.1` 是容器自己，
   而 compose 映射出来的 `1008x/1009x` 只对**宿主**有意义。写错了不会立刻报错 ——
   会连到"什么都没有"的超时或者别的服务上。所以这里把「服务名 + 内部端口」同时钉住，
   并且要求声明的内部端口必须出现在该服务自己公布的端口映射**右侧**（防两处漂移）。
2. **`depends_on` 的 condition 有硬约束**：目标服务没写 `healthcheck` 却要
   `service_healthy`，compose 直接报错、整套起不来。mysql 首次启动要初始化数据目录（~30s），
   不等它 app 会连不上 —— 所以只有它有 healthcheck 时必须等。
3. **构建期不许依赖 GitHub**：`en-core-web-sm`（spacy 模型）的 wheel 只有 GitHub 一个来源，
   而它在产品路径里 **0 处 import**（`src/` 里只有 4 处 docstring 在解释为什么不用
   unstructured）。一旦它回到主依赖，`uv sync --locked` / `docker build` 每次都要去
   github.com 取一个用不上的模型 —— 本机实测就是卡在这里（`uv run` 卡满 90s 超时，
   GitHub 连接超时）。所以「它在可选档、不在运行时档」是**契约**，不是洁癖。
4. **注释不是配置、子串不是匹配**：第一版这里拿 `compose` 的**原文**去搜 `host.docker.internal`，
   结果被我自己那句「不再是 host.docker.internal 那一串宿主映射」的注释判成失败 ——
   假红（现在只扫**解析后的值**）；Dockerfile 同理只看**逻辑行**（跳过注释、把 `\\` 续行接起来，
   否则多行的 `ENV` 块读不到 `RAG_API__PORT`）。反方向也踩了一次：`.dockerignore` 原本用
   `item in 全文` 判 `.env` 在不在，而 `.venv/` 这行里就含 `.env` —— 把 `.env` 那行删掉
   检查照样绿（假绿，比假红坏得多）。现在按**整行**比。两次都是 `reverse_m6_deploy.py` 逼出来的。

``RAG_CHECK_ROOT`` 环境变量可以换掉被检查的仓库根 —— `reverse_m6_deploy.py` 就是靠它
把这几份文件复制到临时目录、逐个改坏、再确认对应那条检查真的会红。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from _harness import check, report

ROOT = Path(os.environ.get("RAG_CHECK_ROOT") or Path(__file__).resolve().parents[2])
COMPOSE = ROOT / "docker-compose.yaml"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
ENV_EXAMPLE = ROOT / ".env.example"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
# 第六节（宿主入口的端口分层）要读的几份。**定义在最上面**：`@check` 是装饰即执行，
# 定义在下面的话，前面那些检查跑到时这些名字还不存在（实测 NameError）。
DEV_SH = ROOT / "scripts/dev.sh"
DEV_ENV = ROOT / "web/.env.development"
CONFIG_YAML = ROOT / "config/config.yaml"
BUG_TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml"
API_MD = ROOT / "API.md"

# app 连的三个中间件：compose 服务名 -> (地址变量, 端口变量)
MIDDLEWARES: dict[str, tuple[str, str]] = {
    "standalone": ("RAG_MILVUS__HOST", "RAG_MILVUS__PORT"),
    "mysql": ("RAG_MYSQL__HOST", "RAG_MYSQL__PORT"),
    "redis": ("RAG_REDIS__HOST", "RAG_REDIS__PORT"),
}
# 宿主侧映射出来的端口：容器里出现这些数字，说明地址还是按宿主写的
HOST_PORTS = {"10086", "10087", "10088", "10089", "10090", "10091", "10092", "10093"}
# 这些包曾经进过一个只有 GitHub 来源的可选档;现在整档删除,它们**任何时候**都不许回主依赖
OPTIONAL_ONLY = {"unstructured", "langchain-unstructured", "en-core-web-sm"}


def _compose() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _app() -> dict:
    return _compose()["services"]["app"]


def _yaml(path: Path) -> dict:
    """读任意一份 YAML（compose 之外的几份也用它；定义在这里是因为**第三节就要用**）。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _names(requirements: list[str]) -> set[str]:
    """把 ``unstructured>=0.16,<1.0`` 这样的依赖串削成包名（小写）。"""
    return {re.split(r"[<>=!\[; ]", item.strip(), maxsplit=1)[0].lower() for item in requirements}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _logical_lines() -> list[str]:
    """Dockerfile 的**逻辑行**：跳过注释、把 ``\\`` 续行接起来。

    两道必须：① 多行 ``ENV`` 块（``ENV A=1 \\`` + ``    B=2``）不接起来就读不到第二行起的内容；
    ② 注释里会提到 ``COPY . .``、``host.docker.internal`` 这类"反面教材"，拿原文搜会假红。
    """
    lines: list[str] = []
    buffer = ""
    for raw in _dockerfile_text().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        if buffer.strip():
            lines.append(buffer.strip())
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def _dockerfile_env(name: str) -> str | None:
    """从逻辑行里取 ``ENV`` 给的某个默认值（找不到返回 None）。"""
    for line in _logical_lines():
        if not line.startswith("ENV "):
            continue
        found = re.search(rf"(?<![\w.]){re.escape(name)}=(\S+)", line)
        if found:
            return found.group(1)
    return None


def _service_env_values() -> list[tuple[str, str]]:
    """compose 里所有服务声明的环境变量 ``(服务名, 值)``，**只取解析后的值**（不扫原文）。"""
    out: list[tuple[str, str]] = []
    for service, spec in _compose()["services"].items():
        raw = spec.get("environment") or {}
        pairs = raw.items() if isinstance(raw, dict) else (str(item).split("=", 1) for item in raw)
        for _key, value in pairs:
            out.append((service, str(value)))
    return out


# ── 一、文件与构建入口 ────────────────────────────────────────────────────────
@check("compose 是合法 YAML，服务集合 = 中间件 6 个 + app + MinerU 解析服务")
def _services() -> str:
    services = _compose()["services"]
    expected = {
        "etcd", "minio", "standalone", "redis", "mysql", "attu", "app",
        # 图片模态的独立解析服务。它不进 app 进程(见 services/mineru/README.md)。
        "mineru",
    }
    if set(services) != expected:
        raise AssertionError(
            f"服务集合对不上：多了 {sorted(set(services) - expected)}，"
            f"少了 {sorted(expected - set(services))}"
        )
    return " / ".join(services)


@check("app 从仓库根的 Dockerfile 构建，且是两阶段（builder → runtime）")
def _build_entry() -> str:
    build = _app().get("build")
    if not isinstance(build, dict):
        raise AssertionError(f"build 要写成映射（能指名 dockerfile），实际 {build!r}")
    if build.get("context") != ".":
        raise AssertionError(f"构建上下文应是仓库根 '.'，实际 {build.get('context')!r}")
    if build.get("dockerfile") != "Dockerfile":
        raise AssertionError(f"应显式写 dockerfile: Dockerfile，实际 {build.get('dockerfile')!r}")
    if not DOCKERFILE.is_file():
        raise AssertionError(f"{DOCKERFILE} 不存在，构建上下文里没有 Dockerfile")
    stages = [line.split()[3] for line in _logical_lines() if re.match(r"FROM\s+\S+\s+AS\s+\S+", line)]
    if stages != ["builder", "runtime"]:
        raise AssertionError(f"两阶段的名字应是 ['builder', 'runtime']，实际 {stages}")
    return f"context=. / Dockerfile / 阶段 {stages}"


@check("app 的镜像 tag 是固定值（不是 latest），且与 pyproject 的版本一致")
def _image_tag() -> str:
    image = _app().get("image")
    if not isinstance(image, str) or ":" not in image or image.endswith(":latest"):
        raise AssertionError(f"image 要带固定版本号，不能是 latest/裸名字：{image!r}")
    version = _pyproject()["project"]["version"]
    want = f"project-rag:{version}"
    if image != want:
        raise AssertionError(f"tag 与 pyproject 的版本漂了：期望 {want}，实际 {image!r}")
    return image


# ── 二、地址与端口 ────────────────────────────────────────────────────────────
@check("app 连中间件用服务名 + 容器内端口（不出现 127.0.0.1 / host.docker.internal / 1008x）")
def _addresses() -> str:
    env = _app()["environment"]
    seen = []
    for service, (host_key, port_key) in MIDDLEWARES.items():
        got_host, got_port = env.get(host_key), env.get(port_key)
        if got_host != service:
            raise AssertionError(
                f"{host_key} 应是服务名 {service!r}（容器里 127.0.0.1 是容器自己），实际 {got_host!r}"
            )
        if not isinstance(got_port, str):
            raise AssertionError(
                f"{port_key} 必须写成字符串：YAML 会把 3306 解析成 int，compose 里类型不一致"
            )
        if got_port in HOST_PORTS:
            raise AssertionError(
                f"{port_key}={got_port} 是**宿主**映射端口；容器同一网络里要用内部端口"
            )
        seen.append(f"{service}:{got_port}")
    for service, value in _service_env_values():
        if "host.docker.internal" in value:
            raise AssertionError(
                f"{service} 的环境变量里还有 host.docker.internal —— 中间件就在同一网络里，"
                f"用服务名（手工 docker run 那份才需要它）"
            )
    return " / ".join(seen)


@check("app 声明的容器内端口 = 各中间件自己映射的右侧端口（防两处漂移）")
def _ports_match_published() -> str:
    services = _compose()["services"]
    env = _app()["environment"]
    out = []
    for service, (_, port_key) in MIDDLEWARES.items():
        published = services[service].get("ports") or []
        inside = set()
        for entry in published:
            text = entry if isinstance(entry, str) else f"{entry.get('published')}:{entry.get('target')}"
            parts = str(text).split(":")
            if len(parts) >= 2:
                inside.add(parts[-1].split("/")[0])
        want = env[port_key]
        if want not in inside:
            raise AssertionError(
                f"{service} 公布的容器内端口是 {sorted(inside) or '（它没写 ports）'}，app 却连 {want}"
            )
        out.append(f"{service}={want}")
    return " / ".join(out)


@check("app 的宿主端口映射到镜像 EXPOSE 的端口，探活打 /health")
def _expose_and_healthcheck() -> str:
    lines = _logical_lines()
    exposed = [line.split()[1] for line in lines if line.startswith("EXPOSE ")]
    if len(exposed) != 1:
        raise AssertionError(f"EXPOSE 应恰好一条，实际 {exposed}")
    env_port = _dockerfile_env("RAG_API__PORT")
    if not env_port:
        raise AssertionError("Dockerfile 里没有给 RAG_API__PORT 设默认值")
    if env_port != exposed[0]:
        raise AssertionError(f"ENV RAG_API__PORT={env_port} 与 EXPOSE {exposed[0]} 不一致")
    mapping = _app()["ports"]
    if len(mapping) != 1:
        raise AssertionError(f"ports 应恰好一条（宿主端口可改，容器内端口不能），实际 {mapping}")
    host, _, container = str(mapping[0]).partition(":")
    container = container.split("/")[0]
    if container != exposed[0]:
        raise AssertionError(f"映射右侧 {container} != EXPOSE {exposed[0]}（右侧是容器内端口）")
    if not host.isdigit():
        raise AssertionError(f"映射左侧应是宿主端口号，实际 {host!r}")
    healthcheck = next((line for line in lines if line.startswith("HEALTHCHECK")), "")
    if f":{exposed[0]}/health" not in healthcheck:
        raise AssertionError(f"HEALTHCHECK 没打 /health（或端口不对）：{healthcheck!r}")
    return f"{host}:{container}；探活 = http://127.0.0.1:{exposed[0]}/health"


# ── 三、启动顺序与挂载 ────────────────────────────────────────────────────────
@check("depends_on 的 condition 与目标服务有没有 healthcheck 相匹配")
def _depends_on() -> str:
    services = _compose()["services"]
    depends = _app()["depends_on"]
    if set(depends) != set(MIDDLEWARES):
        raise AssertionError(
            f"depends_on 应恰好是 {sorted(MIDDLEWARES)}，实际 {sorted(depends)}"
        )
    out = []
    for name, spec in depends.items():
        condition = spec["condition"] if isinstance(spec, dict) else "service_started"
        has_healthcheck = "healthcheck" in services[name]
        if has_healthcheck and condition != "service_healthy":
            raise AssertionError(
                f"{name} 有 healthcheck，app 却只等它 {condition} —— 首次启动会连不上"
            )
        if not has_healthcheck and condition == "service_healthy":
            raise AssertionError(
                f"{name} 没有 healthcheck，写 service_healthy 会让 compose 直接报错、整套起不来"
            )
        out.append(f"{name}:{condition}")
    return " ".join(out)


@check("模型/语料只读挂载、上传目录与 logs 可写，路径都在镜像的 WORKDIR 下")
def _mounts() -> str:
    workdir_match = next((line for line in _logical_lines() if line.startswith("WORKDIR ")), "")
    workdir = workdir_match.split()[1] if workdir_match else "/app"
    mounts: dict[str, str] = {}
    for entry in _app()["volumes"]:
        parts = str(entry).split(":")
        if len(parts) < 2:
            raise AssertionError(f"挂载写法看不懂（要 host:容器[:ro]）：{entry!r}")
        mounts[parts[1]] = parts[2] if len(parts) > 2 else "rw"
    # 上传目录必须是**唯一**多出来的那条可写挂载，而且要与 config.yaml 的 api.uploads_dir 对得上：
    # 少了它，容器里的「上传文档」必然失败。
    uploads = str(_yaml(CONFIG_YAML)["api"]["uploads_dir"]).strip("/")
    want = {
        f"{workdir}/models": "ro",
        f"{workdir}/data": "ro",
        f"{workdir}/{uploads}": "rw",
        # MinerU 结果缓存:下载的 zip 要解压到这里,而 /app/data 是只读的。
        f"{workdir}/data/mineru": "rw",
        f"{workdir}/logs": "rw",
    }
    if mounts != want:
        raise AssertionError(
            f"挂载应是 {want}，实际 {mounts} —— "
            "语料读得到、上传写得进、日志写得出，这三件事各要一条挂载"
        )
    return " ".join(f"{k}:{v}" for k, v in mounts.items())


@check("app 的密钥从 .env 注入，且 .env.example 备齐三个必填键")
def _env_file() -> str:
    files = _app().get("env_file") or []
    names = [f if isinstance(f, str) else f.get("source") for f in files]
    if ".env" not in names:
        raise AssertionError(f"env_file 里应含 .env（密钥只从这里进容器），实际 {names}")
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    need = ["RAG_LLM__DEEPSEEK_API_KEY", "RAG_MYSQL__PASSWORD", "RAG_REDIS__PASSWORD"]
    missing = [key for key in need if not re.search(rf"(?m)^{re.escape(key)}=", text)]
    if missing:
        raise AssertionError(f".env.example 少了必填键：{missing}")
    return f"env_file={names}；模板备齐 {'/'.join(need)}"


# app 的推理设备可能来自三处，优先级：compose.environment > env_file 里的 .env > 镜像 ENV。
# 只看 compose 那一处是不够的：`.env` 里写一行 DEVICE 同样能把它打开。
def _app_device() -> str | None:
    """输入:无。行为:算出 app 服务**有效**的 ``RAG_EMBEDDING__DEVICE``。输出:值或 None。"""
    name = "RAG_EMBEDDING__DEVICE"
    raw = _app().get("environment") or {}
    pairs = raw.items() if isinstance(raw, dict) else (str(item).split("=", 1) for item in raw)
    for key, value in pairs:
        if key == name:
            return str(value)
    for entry in _app().get("env_file") or []:
        path = entry if isinstance(entry, str) else str(entry.get("path") or entry.get("source"))
        target = ROOT / path
        if not target.is_file():
            continue
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
    return _dockerfile_env(name)


def _app_has_gpu() -> bool:
    """输入:无。行为:app 服务有没有申请 GPU。输出:有/没有。

    两种写法都认：`gpus: all`（compose 简写）与
    ``deploy.resources.reservations.devices[].capabilities`` 里带 `gpu`。
    """
    app = _app()
    if app.get("gpus"):
        return True
    devices = app.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices") or []
    for device in devices:
        caps = [str(item).lower() for item in device.get("capabilities") or []]
        if "gpu" in caps:
            return True
    return False


@check("依赖源：pyproject 里没有任何 direct-URL 源（任何一次重新解析都不会碰 GitHub）")
def _no_direct_url() -> str:
    """历史:`en-core-web-sm` 的 wheel 只有 GitHub 一个来源,uv 每次重新解析都要取它
    (实测取一次 172s,默认 46s 放弃),`uv run` 与 `docker build` 会随机死在网络上。
    它只被一个用不上的可选档引用,那一档已整块删除 —— 这条盯住"别再回来"。
    """
    data = _pyproject()
    uv = data.get("tool", {}).get("uv", {})
    sources = uv.get("sources") or {} if isinstance(uv, dict) else {}
    offenders = {
        str(name): spec
        for name, spec in (sources or {}).items()
        if isinstance(spec, dict) and ("url" in spec or "path" in spec)
    }
    if offenders:
        raise AssertionError(
            f"这些依赖从 URL / 本地路径取包：{offenders} —— 首次解析必然联外网，"
            f"`uv run` 与 `docker build` 会随机超时。放回 PyPI 镜像，或把 wheel vendored 进仓库"
        )
    count = len(data.get("project", {}).get("dependencies", []))
    return f"[tool.uv.sources] 为空，{count} 个主依赖全部来自 PyPI 镜像"


@check("Dockerfile 拷齐了 pyproject 声明的 readme / license 文件（缺了要到构建最后一层才报错）")
def _build_context_files() -> str:
    """加（开源化第 1 批）。给 pyproject 补 `readme` 与 `license-files` 之后，
    **镜像构建当场失败**：hatchling 在 `uv sync --locked --no-dev`（装项目本体那一步）要读这两个
    文件，而 Dockerfile 只 COPY 了 pyproject/uv.lock/src/config，于是构建死在最后一层：

        OSError: Readme file does not exist: README.md   （exit 1，前面 8 层全白跑）

    这类"元数据声明了一个文件、构建上下文里没有"的错，**静态就能查**：从 pyproject 取
    `readme` 与 `license-files`，逐个看 Dockerfile 的 COPY 行里出现过。慢的验证（真构建）
    只该用来兜底，不该用来发现。
    """
    project = _pyproject().get("project", {})
    needed: list[str] = []
    readme = project.get("readme")
    if isinstance(readme, str):
        needed.append(readme)
    elif isinstance(readme, dict) and readme.get("file"):
        needed.append(str(readme["file"]))
    needed.extend(str(item) for item in project.get("license-files") or [])
    if not needed:
        raise AssertionError(
            "pyproject 里既没有 readme 也没有 license-files —— 那这条检查与开源化那段该一起删"
        )
    copies = [line for line in _logical_lines() if line.startswith("COPY ")]
    missing = [
        name
        for name in needed
        if not any(name in line or Path(name).name in line for line in copies)
    ]
    if missing:
        raise AssertionError(
            f"这些文件 pyproject 声明了、Dockerfile 却没 COPY 进镜像：{missing} —— "
            f"hatchling 在装项目本体那步要读它们，构建会死在最后一层"
            f"（`OSError: Readme file does not exist`）。补一条 `COPY {' '.join(missing)} ./`"
        )
    return f"COPY 齐了 {needed}"


@check("app 的推理设备与 GPU 预约一致（写 cuda 却没申请 GPU → 预热静默失败、/health 照样绿）")
def _device_matches_gpu() -> str:
    """真机踩到的：compose 里 `RAG_EMBEDDING__DEVICE: cuda` 而服务没有 GPU 预约。

    后果**不是**启动失败（所以没人发现），而是:① 启动预热静默失败,日志里一行 WARNING
    "Found no NVIDIA driver on your system（不影响启动,首个请求会自己补做）";
    ② `/health` 照样 200 —— 探活只问"进程在不在";③ **第一次真提问**才会炸,
    而那时报的是模型加载错误,看着像"模型没挂对"。这正是「绿的健康检查 ≠ 能干活」那条
    「服务端接受 ≠ 支持」的同族:绿的健康检查不等于能干活。
    """
    device = _app_device()
    if device is None or not device.startswith("cuda"):
        return f"RAG_EMBEDDING__DEVICE = {device or '(未设 → 用镜像默认 cpu)'}，不需要 GPU"
    if _app_has_gpu():
        return f"{device} + app 已申请 GPU"
    raise AssertionError(
        f"app 要 `{device}` 却没申请 GPU（既没有 `gpus:` 也没有 "
        f"deploy.resources.reservations.devices）—— 容器里看不见宿主 GPU，"
        f"结果是**预热静默失败 + /health 照样 200 + 第一次真提问才炸**。"
        f"要么删掉这一行（镜像默认 cpu），要么把 GPU 预约一起加上。"
    )


# ── 四、依赖档位：构建期不许碰 GitHub ─────────────────────────────────────────
@check("运行时依赖里没有 unstructured 那一档（它不进镜像）")
def _optional_not_runtime() -> str:
    """**别把这条的标题读成「构建期不必访问 GitHub」** —— 真机构建证明那是错的:
    即使 `--no-dev`、即使没开这个 extra,`uv sync --locked` 仍会去 github 取
    `en-core-web-sm` 的**元数据**来校验锁(容器里实测 186s)。这条只管「它不进镜像」;
    「构建期要访问哪些网络」由 Dockerfile 的 `UV_HTTP_TIMEOUT` 那段注释负责说清。"""
    names = _names(_pyproject()["project"]["dependencies"])
    hit = sorted(names & OPTIONAL_ONLY)
    if hit:
        raise AssertionError(
            f"这些又回到运行时依赖了：{hit} —— en-core-web-sm 的 wheel 只有 GitHub 一个来源，"
            f"留在主依赖里会让 uv sync --locked / docker build 每次去 github.com 取一个用不上的模型"
        )
    return f"主依赖 {len(names)} 个，不含 {sorted(OPTIONAL_ONLY)}"


@check("那个只有 GitHub 来源的可选档已删除（留着它 = 每次重新解析都要碰 GitHub）")
def _extra_removed() -> str:
    """`unstructured` 一档（含 `en-core-web-sm`）已整块删掉：`src/` 里 0 处 import，
    而它让解析阶段常驻一个 GitHub 依赖。要用时由人显式加回并接受那条网络风险。
    """
    data = _pyproject()
    extras = data.get("project", {}).get("optional-dependencies", {})
    if "unstructured" in extras:
        raise AssertionError(
            "optional-dependencies 里又有 unstructured 了 —— 那一档会重新把 GitHub 轮子带进解析，"
            "`uv run` 会再次随机超时"
        )
    sources = data.get("tool", {}).get("uv", {}).get("sources", {}) or {}
    if "en-core-web-sm" in sources:
        raise AssertionError("en-core-web-sm 又被声明成依赖源了")
    return f"extras = {sorted(extras)}（无 unstructured）"


@check("uv.lock 已跟着重锁：GitHub 轮子没有残留，且 extras 与 pyproject 一致")
def _lock_consistent() -> str:
    """锁是**产物**:pyproject 删掉的包,锁里不清掉等于"下次解析还会去取它"。
    `provides-extras` 用集合比对,新增档位不必改这里 —— 钉的是"锁与 pyproject 不漂"。
    """
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    packages = {str(item.get("name")) for item in lock.get("package", [])}
    stale = sorted(packages & OPTIONAL_ONLY)
    if stale:
        raise AssertionError(
            f"uv.lock 里还有 {stale} —— 它们已从 pyproject 删除，跑一次 `uv lock` 清掉；"
            f"留着的话下次重新解析仍会去 GitHub 取那个 wheel"
        )
    root = next((item for item in lock["package"] if item.get("name") == "project-rag"), None)
    if root is None:
        raise AssertionError("uv.lock 里没有 project-rag 自己的条目")
    main = {str(d.get("name")) for d in root.get("dependencies", [])}
    leaked = sorted(main & OPTIONAL_ONLY)
    if leaked:
        raise AssertionError(f"锁的默认依赖里还有 {leaked} —— 与 pyproject 不一致，忘了 uv lock")
    declared_extras = set(_pyproject().get("project", {}).get("optional-dependencies", {}))
    locked_extras = set(root.get("metadata", {}).get("provides-extras") or [])
    if locked_extras != declared_extras:
        raise AssertionError(
            f"provides-extras 与 pyproject 的档位不一致：锁 {sorted(locked_extras)} vs "
            f"pyproject {sorted(declared_extras)} —— 忘了 uv lock"
        )
    return f"锁里无 {sorted(OPTIONAL_ONLY)}；extras = {sorted(declared_extras)} 与锁一致"


# ── 五、镜像卫生 ──────────────────────────────────────────────────────────────
@check("镜像不带密钥/模型/语料，且以非 root 运行")
def _image_hygiene() -> str:
    ignore = DOCKERIGNORE.read_text(encoding="utf-8")
    # **整行**匹配，不许用子串：`.venv/` 里含 `.env`，用 `in` 会让「删掉 .env 那行」照样通过
    # —— 这条假绿就是 reverse_m6_deploy.py 的 RT-13 揪出来的。
    ignored = {
        line.strip()
        for line in ignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    need = [".env", "models/", "data/", ".venv/"]
    missing = [item for item in need if item not in ignored]
    if missing:
        raise AssertionError(
            f".dockerignore 少了 {missing} —— 会被打进镜像层或把构建上下文拖大十倍"
        )
    lines = _logical_lines()
    if not any(line.startswith("USER ") for line in lines):
        raise AssertionError("Dockerfile 没有 USER —— 默认 root 跑")
    if any(re.match(r"COPY\s+\.\s", line) for line in lines):
        raise AssertionError(
            "Dockerfile 里有 COPY . … —— 那样全靠 .dockerignore 兜底，太脆（只 COPY src/ config/ 与依赖清单）"
        )
    if any(re.match(r"COPY\s+[^\n]*\.env", line) for line in lines):
        raise AssertionError("Dockerfile 把 .env 拷进镜像了")
    return f"忽略 {need}；USER 就位；无 COPY ."


@check("构建期只依赖 docker 镜像源 + pip 源：不用外挂 frontend、不拉 ghcr.io")
def _no_extra_registries() -> str:
    # `# syntax=` 是**唯一**一条「注释即配置」的行（BuildKit 会照着它去 registry 拉 frontend 镜像），
    # 所以这里必须扫**原文**，不能用 `_logical_lines()`（那会把注释全跳过）。
    directives = [
        line.strip()
        for line in _dockerfile_text().splitlines()
        if line.strip().startswith("# syntax")
    ]
    if directives:
        raise AssertionError(
            f"`# syntax=` 会让 BuildKit 去 registry 拉 frontend 镜像 —— 那是整个构建的**第一个**"
            f"网络请求，国内网络 / 代理一坏就死在这一行（实测报错见 Dockerfile 头部）：{directives}"
        )
    leaked = [line for line in _logical_lines() if "ghcr.io" in line]
    if leaked:
        raise AssertionError(
            f"构建期还在从 ghcr.io 拿东西（国内经常连不上，且这条链上不需要）：{leaked}"
        )
    uv_lines = [line for line in _logical_lines() if "uv==" in line]
    if not uv_lines:
        raise AssertionError("找不到钉住版本的 uv 安装步骤（期望 `pip install … uv==0.12.11`）")
    return "无 `# syntax=`；无 ghcr.io；uv 由 pip 源安装且版本钉死"


# ── 六、宿主代码后端与容器的端口分层 ─────────────────────────────────────────
# 这一节是给「想边调代码、边留着容器当产品」这个姿势兜底的：两者必须各占一个端口，
# 而且**文档里写的端口必须就是代码里用的那个**。本轮就是这么翻的车：`dev.sh` 的注释写着
# 「起后端（宿主 18080）」，实际跑的是 config.yaml 的 8080 —— 于是裸 `uv run project-rag-api`
# 直接 `[Errno 98] Address already in use`（容器占着 8080），而报 bug 的模板还在教人
# `curl localhost:18080`。所以这里不抄一份端口，而是**从 dev.sh 里读**再和 compose 核对。
# 假 uv：守卫过了没有，看它有没有被调用就知道；同时保证**永远不会真起一个后端**
# （反向测试会故意把守卫摘掉，那时真 uv 会在这里被换成打印器）。
_FAKE_UV = """#!/bin/sh
printf 'FAKE-UV %s | RAG_API__PORT=%s RAG_LOG_DIR=%s\\n' "$*" "${RAG_API__PORT:-}" "${RAG_LOG_DIR:-}" >> "$FAKE_UV_OUT"
exit 99
"""

# dev.sh 里「四/五位数字必须是端口」的例外：1000 是**容器里的 uid**，不是端口。
# 用白名单而不是放宽正则 —— 放宽了，真正的端口漂移（18090 那种）就悄悄漏过去了。
_NON_PORT_NUMBERS = {1000}


def _dev_sh_text() -> str:
    return DEV_SH.read_text(encoding="utf-8")


def _dev_const(name: str) -> str:
    """输入:dev.sh 里的常量名。行为:读出它的字面值。输出:字符串（去引号）。

    **不在这份检查里抄一份端口**：抄了就是两处真相 —— 而两处真相正是本轮漂移的形状。
    """
    match = re.search(rf'^{re.escape(name)}=("?)([^"\n]+)\1\s*$', _dev_sh_text(), re.M)
    if not match:
        raise AssertionError(f"dev.sh 里找不到 `{name}=…`（端口分层的单一来源没了）")
    return match.group(2)


def _dev_port(name: str) -> int:
    value = _dev_const(name)
    if not value.isdigit():
        raise AssertionError(f"dev.sh 的 {name}={value!r} 不是端口号")
    return int(value)


def _published_host_ports() -> list[int]:
    """compose 里 app 映射到**宿主**那一侧的端口（`8080:8080` 的左边那个）。"""
    ports: list[int] = []
    for item in _app().get("ports", []):
        parts = str(item).split(":")
        if len(parts) >= 2 and parts[-2].isdigit():
            ports.append(int(parts[-2]))
    if not ports:
        raise AssertionError(f"compose 里读不到 app 的宿主端口映射：{_app().get('ports')!r}")
    return ports


def _run_dev_sh(args: list[str], out: Path) -> subprocess.CompletedProcess[str]:
    """用**假 uv** 跑 dev.sh：既看得见守卫放没放行，又不会真起一个后端。"""
    bin_dir = out.parent / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text(_FAKE_UV, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_UV_OUT"] = str(out)
    return subprocess.run(
        ["bash", str(DEV_SH), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@check("端口分层：容器 8080（compose 发布 + 镜像 ENV）与代码后端 18080（config.yaml）各就各位，互不重叠")
def _port_layering() -> str:
    dev = _dev_port("DEV_BACKEND_PORT")
    vite = _dev_port("VITE_PORT")
    declared_container = _dev_port("CONTAINER_PORT")
    published = _published_host_ports()
    if declared_container not in published:
        raise AssertionError(
            f"dev.sh 说容器在 {declared_container}，compose 实际发布的是 {published} —— 两处真相又分家了"
        )
    if dev in published:
        raise AssertionError(
            f"代码后端端口 {dev} 与容器发布的 {published} 撞了：那样两个后端只能活一个，"
            "「边调代码边留着容器」就不成立（实测裸 uv run 直接 Errno 98）"
        )
    if dev == vite:
        raise AssertionError(f"代码后端端口 {dev} 与 vite 的 {vite} 撞了")
    # 注释漂移：文件里出现的**所有**四/五位数字都必须是这三个角色之一。
    # 这条就是本轮翻车的形状：注释 18080、代码 8080，谁都没红。
    allowed = set(published) | {dev, vite}
    stray = sorted(
        {int(n) for n in re.findall(r"\b\d{4,5}\b", _dev_sh_text())} - allowed - _NON_PORT_NUMBERS
    )
    if stray:
        raise AssertionError(
            f"dev.sh 里出现了不属于任何角色的端口 {stray}（只认 {sorted(allowed)}，"
            f"非端口数字白名单 {sorted(_NON_PORT_NUMBERS)}）—— 注释与代码分家就是这么开始的"
        )
    config_port = int(_yaml(CONFIG_YAML)["api"]["port"])
    # 分工是定死的：**仓库里的宿主配置 = 代码后端端口**（裸 `uv run project-rag-api`
    # 与 `./scripts/dev.sh serve` 读的是同一个值），容器则靠镜像里的 ENV 盖住它。
    # 两半都要钉住 —— 只钉一半，另一半就能悄悄漂回「两个后端抢一个口」。
    if config_port != dev:
        raise AssertionError(
            f"config.yaml 的 api.port={config_port} 不是代码后端端口 {dev}："
            "裸 `uv run project-rag-api` 会绑到别的口上（曾经就是 8080，与容器抢，直接 Errno 98）"
        )
    container_env = _dockerfile_env("RAG_API__PORT")
    if container_env != str(published[0]):
        raise AssertionError(
            f"镜像里 RAG_API__PORT={container_env!r}，而 compose 发布的是 {published[0]} —— "
            "yaml 现在是 18080（代码后端），容器能绑回 8080 全靠这个 ENV：它一丢，容器就绑到 18080 上，"
            "compose 的映射与探活全落空"
        )
    # 文档里的数字也要跟代码核对：API.md 的配置表曾经把默认值写成 18080 而代码是 8080
    # （之后两者都是 18080），与注释漂移是同一个形状 ——
    # 「文档说的端口」和「代码跑的端口」必须有一处对得上。
    api_md = API_MD.read_text(encoding="utf-8")
    documented = re.search(
        r"\|\s*`api\.host`\s*/\s*`api\.port`\s*\|\s*`[^`]+`\s*/\s*`(\d+)`", api_md
    )
    if not documented:
        raise AssertionError("API.md 的配置表里读不到 `api.host` / `api.port` 那一行（文档漂移的起点）")
    if int(documented.group(1)) != config_port:
        raise AssertionError(
            f"API.md 说 api.port 默认 {documented.group(1)}，config.yaml 却是 {config_port} —— "
            "文档里的数字必须由检查去跟代码核对（本轮就是这么漂的：文档 18080、代码 8080）"
        )
    return (
        f"container={declared_container}(=compose {published}，镜像 ENV={container_env}) / "
        f"dev={dev}(=config.yaml，裸跑与 dev.sh serve 都读它) / vite={vite}；dev.sh 里再无别的端口"
    )


@check("web/.env.development 默认后端 = 代码后端端口（vite 调前端时打的是你正在改的那份）")
def _dev_env_default_backend() -> str:
    match = re.search(r"^VITE_DEFAULT_BACKEND=(\S+)\s*$", DEV_ENV.read_text(encoding="utf-8"), re.M)
    if not match:
        raise AssertionError("web/.env.development 里没有 VITE_DEFAULT_BACKEND（vite dev 就不知道打哪儿）")
    url = match.group(1)
    found = re.search(r"^https?://(localhost|127\.0\.0\.1):(\d+)/?$", url)
    if not found:
        raise AssertionError(f"VITE_DEFAULT_BACKEND={url!r} 不是「本机 http 地址」—— 开发态默认值只该指本机")
    port = int(found.group(2))
    dev = _dev_port("DEV_BACKEND_PORT")
    if port != dev:
        raise AssertionError(
            f"VITE_DEFAULT_BACKEND 指向 {port}，而代码后端在 {dev}：vite 调前端时会打到别的后端上"
            "（要打容器那份就在页面左上角「后端」里填，而不是改这个默认值）"
        )
    return f"{url}（= 代码后端；容器那份由页面左上角「后端」临时指定）"


@check("代码后端写自己的日志目录：两个进程不轮流转 logs/app.log（RotatingFileHandler 不是多进程安全的）")
def _dev_log_dir() -> str:
    text = _dev_sh_text()
    dev_log = _dev_const("DEV_LOG_DIR")
    config_log = str(_yaml(CONFIG_YAML)["log_dir"]).strip("/")
    if dev_log.strip("/") == config_log:
        raise AssertionError(
            f"代码后端的日志目录与 config.yaml 的 log_dir 是同一个（{dev_log}）："
            "容器挂载的也是它，两个进程会各自轮转同一个 app.log（轮转瞬间丢行）"
        )
    if Path(config_log) not in Path(dev_log).parents:
        raise AssertionError(
            f"代码后端日志目录 {dev_log!r} 不在 config.yaml 的 log_dir（{config_log}）之下："
            "那样 `docker compose` 挂载的 ./logs 里就看不到调试日志了"
        )
    if 'RAG_LOG_DIR="$DEV_LOG_DIR"' not in text:
        raise AssertionError(
            "dev.sh 里没有把 DEV_LOG_DIR 传给进程（少了 `RAG_LOG_DIR=\"$DEV_LOG_DIR\"`）——"
            "常量写得再对，没接线也等于没有"
        )
    return f"{dev_log}/app.log（容器仍写 {config_log}/app.log）"


@check("dev.sh 起服务前的守卫：容器端口会被拒、被占的端口会被拒，两种情况都不去启动进程")
def _dev_sh_port_guards() -> str:
    dev_log = _dev_const("DEV_LOG_DIR")
    container = _published_host_ports()[0]
    with tempfile.TemporaryDirectory(prefix="dev_entry_") as raw:
        work = Path(raw)
        # ① 正常路径：端口空闲 → 放行，并把端口与日志目录**真的传下去**
        free = _free_port()
        out = work / "ok.txt"
        proc = _run_dev_sh(["serve", str(free)], out)
        called = out.read_text(encoding="utf-8") if out.exists() else ""
        if "run project-rag-api" not in called:
            raise AssertionError(
                f"`dev.sh serve {free}`（端口空闲）没走到启动那一步 —— 输出：{proc.stdout}{proc.stderr}"
            )
        for wanted in (f"RAG_API__PORT={free}", f"RAG_LOG_DIR={dev_log}"):
            if wanted not in called:
                raise AssertionError(f"serve 没把 {wanted.split('=')[0]} 传给后端：{called.strip()!r}")
        # ② 容器端口：静态拒绝，而且要说清为什么
        out = work / "container.txt"
        proc = _run_dev_sh(["serve", str(container)], out)
        if out.exists() and out.read_text(encoding="utf-8"):
            raise AssertionError(f"把容器端口 {container} 当代码后端端口，居然还是启动了：{out.read_text()}")
        if proc.returncode == 0:
            raise AssertionError(f"`dev.sh serve {container}` 居然返回 0")
        if "容器" not in proc.stdout + proc.stderr:
            raise AssertionError(
                f"拒绝了容器端口但没说清为什么（得点名「容器」）：{proc.stdout}{proc.stderr}"
            )
        # ③ 被占用的端口：动态拒绝，并把端口号说出来。
        # 用一个**自己握着的**监听口当占用者：与这台机器上恰好跑着什么无关。
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            busy_port = int(busy.getsockname()[1])
            out = work / "busy.txt"
            proc = _run_dev_sh(["serve", str(busy_port)], out)
            text = proc.stdout + proc.stderr
        if out.exists() and out.read_text(encoding="utf-8"):
            raise AssertionError(f"端口 {busy_port} 被占着还是启动了：{out.read_text()}")
        if proc.returncode == 0:
            raise AssertionError(f"`dev.sh serve {busy_port}`（被占）居然返回 0")
        if str(busy_port) not in text or "占用" not in text:
            raise AssertionError(f"端口被占时没说清是哪个端口、被占着：{text}")
    return f"空闲放行（含 RAG_API__PORT / RAG_LOG_DIR）/ 容器端口 {container} 被拒 / 占用端口被拒"


@check("启动入口自报角色：up=中间件三件（且明说没有前端）、app=容器那份、status=能读出谁在发前端")
def _entry_points_self_report() -> str:
    """同一个「启动」有多个入口时，**每个入口必须自己说清它起了什么**。

    真实的混乱：「为什么我有时候启动 docker 会自动启动前端，有时候不会」——
    两个入口的服务集合本来就不一样，而差别只存在于"上次好像有页面"这种记忆里：

    * ``./scripts/dev.sh up`` → ``docker compose up -d standalone mysql redis``（**没有 app**）;
    * ``docker compose up -d``  → compose 里全部服务（含 ``app`` 与 ``attu``），前端在 8080。

    更隐蔽的是第二种：**镜像里的前端产物是构建那一刻的** —— ``npm run build`` 只改工作区，
    容器照旧发旧产物，所以"前端在"也可能"不是你改的那份"。``status`` 把两份摘要读出来对比，
    这条检查钉的就是"这三个入口各自还在做那件事"（**连它调的那个函数一起看**：
    分支里只写一行 ``start_container_app`` 的话，实现在别处漂了同样没人知道）。
    """
    text = _dev_sh_text()

    def branch(name: str) -> str:
        match = re.search(rf"^  {re.escape(name)}\)\n(.*?)^    ;;$", text, re.M | re.S)
        if match is None:
            raise AssertionError(f"dev.sh 里找不到 `{name})` 分支 —— 那个入口没了？")
        return match.group(1)

    def func(name: str) -> str:
        match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}$", text, re.M | re.S)
        if match is None:
            raise AssertionError(f"dev.sh 里找不到 `{name}()` 函数")
        return match.group(1)

    up = branch("up")
    services = set(re.findall(r"docker compose up -d ([a-z ]+)", up)[0].split())
    if services != {"standalone", "mysql", "redis"}:
        raise AssertionError(
            f"`up` 起的是 {sorted(services)}：它的语义是「**只起中间件**」（前端用 app / serve 两个入口）。"
            "集合一变，那句「这一步没有前端」就变成假话 —— 而用户正是靠它判断前端该不该在"
        )
    for entry in ("dev.sh app", "dev.sh backend", "dev.sh web"):
        if entry not in up:
            raise AssertionError(
                f"`up` 起完没说清另外三条路（缺 `{entry}`）：只说「起了 3 件」，"
                "用户还是会去 8080 找一个不存在的前端"
            )

    if "start_container_app" not in branch("app"):
        raise AssertionError("`app` 分支没有走 start_container_app：那个入口与实现分家了")
    body = func("start_container_app")
    if "docker compose up -d app" not in body:
        raise AssertionError("start_container_app 没有真的起 app 容器")
    if "docker compose build app" not in body or '"$build" = "--build"' not in body:
        raise AssertionError(
            "`app` 不支持 `--build`：改了后端代码之后，容器只能靠重建镜像跟上"
        )
    if "npm run build" in body:
        raise AssertionError(
            "`app --build` 又在构建前端：容器默认是 API-only，前端由 `dev.sh web` 单独发；"
            "两条链不该再绑在一起"
        )

    if "show_status" not in branch("status"):
        raise AssertionError("`status` 分支没有走 show_status：入口与实现分家了")
    status = func("show_status")
    if "source-digest.txt" not in text:
        raise AssertionError(
            "dev.sh 里没人读前端产物的摘要（source-digest.txt）：那样「谁在发前端」只能回答"
            "「在不在跑」—— 而「在跑」正是最不容易搞错的那一半"
        )
    for const in ("CONTAINER_PORT", "DEV_BACKEND_PORT", "VITE_PORT"):
        if const not in status:
            raise AssertionError(f"`status` 没有把三个角色都摆出来（少了 {const}）")
    # 「读了两份摘要」还不够，必须**比较**：只打印不比对，用户还是得自己拿眼睛看两个哈希
    if '"$in_container" != "$in_workspace"' not in status:
        raise AssertionError(
            "`status` 读了容器与工作区两份摘要却没有比较：那样它只会把两个哈希并排打出来，"
            "而「容器发的是旧前端」这件事仍然要人自己判断"
        )
    return (
        f"up={sorted(services)} 且点名 app/backend/web 三条路；app→start_container_app "
        "（API-only，--build 只重建镜像）；status→show_status 读 source-digest.txt 并列出三个角色"
    )


@check("报 bug 的模板里两个入口各自带着正确的端口（别让提问的人 curl 错端口）")
def _bug_template_ports() -> str:
    dev = _dev_port("DEV_BACKEND_PORT")
    container = _published_host_ports()[0]
    body = _yaml(BUG_TEMPLATE).get("body", [])
    how = next((item for item in body if item.get("id") == "how"), None)
    if how is None:
        raise AssertionError("bug 模板里找不到「怎么跑的」那一项")
    options = [str(item) for item in how.get("attributes", {}).get("options", [])]
    if not any(str(container) in option for option in options):
        raise AssertionError(f"「怎么跑的」里没有带容器端口 {container} 的选项：{options}")
    dev_options = [option for option in options if "dev.sh serve" in option]
    if not dev_options:
        raise AssertionError(f"「怎么跑的」里没有「宿主代码后端」这一项（该写 ./scripts/dev.sh serve）：{options}")
    if str(dev) not in dev_options[0]:
        raise AssertionError(f"代码后端的选项没写对端口（该是 {dev}）：{dev_options[0]!r}")
    stale = [option for option in options if "uv run project-rag-api" in option and re.search(r"\d{4}", option)]
    if stale:
        raise AssertionError(
            f"模板里还留着「裸 uv run project-rag-api + 写死端口」这种说法 {stale} —— "
            "裸跑读的是 config.yaml，而那个端口的角色是可变的，写死就会像本轮这样教人 curl 错端口"
        )
    repro = next((item for item in body if item.get("id") == "repro"), None)
    placeholder = "" if repro is None else str(repro.get("attributes", {}).get("placeholder", ""))
    curl_ports = {int(n) for n in re.findall(r"localhost:(\d+)", placeholder)}
    if curl_ports != {dev}:
        raise AssertionError(f"复现片段里的 curl 端口 {sorted(curl_ports)} 与代码后端 {dev} 对不上：{placeholder!r}")
    return f"容器 {container} / 代码后端 {dev}（curl 示例 {sorted(curl_ports)}）"


def main() -> int:
    return report(
        "部署契约：compose / Dockerfile / 依赖档位三方自洽 + 宿主入口的端口分层",
        notes=(
            "说明：这些检查证明的是「配置自洽」，**不是**「镜像能构建」。"
            "后者 已在真机上跑过一次（`docker compose build app` + `up -d app`，"
            "宿主 8080 实测）—— 但那是**那台机器那一次**的结果，"
            "换机器/换网络仍要自己跑一遍 `docker compose up -d --build`。",
        ),
        hint=(
            "提示：真机构建失败时先看三处 —— ① 镜像体积（torch 是 CUDA 版，~11G，"
            "CPU-only 的换法在 Dockerfile 末尾注释）；② `uv sync --locked` 与 github.com："
            "**解析期已经不碰它了**（静态元数据，本检查最后一条盯着），只有冷缓存首次 sync 会下载"
            "一次那个 wheel 做哈希校验（12MB，可能慢到超时 → 兜底是 Dockerfile 的 `UV_HTTP_TIMEOUT=300`）；"
            "③ 构建缓存（`--mount=type=cache` 命中时这一笔不用再付）。"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

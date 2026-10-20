#!/usr/bin/env bash
# 开发/验收的短命令入口。
#
# 为什么不是 Makefile：这台开发机（以及作者的 WSL）**没装 make** —— 一个跑不起来的入口
# 比没有入口更糟（文档里写 `make check`，用户敲下去是 command not found）。
# 这里只用 POSIX 工具（bash + uv），任何机器都能跑；CI 里也有等价命令（见 .github/workflows/ci.yml）。
#
# 端口分层：**三个角色，不要再混用**（`./scripts/dev.sh ports` 打印实时占用情况）：
#   8080  = 容器里的 app（**交付形态**，API-only）：`docker compose up -d app` 只发接口
#   18080 = 代码后端（**调试形态**）：本文件的 backend / serve / demo-serve，跑当前源码
#   5173  = Vite 前端（**开发形态**）：`./scripts/dev.sh web`，连哪个后端由参数决定
#
# 为什么代码后端必须换一个端口：同一个端口会和容器抢 —— 曾经 `config.yaml` 与容器都是 8080，
# 裸 `uv run project-rag-api` 直接 `[Errno 98] Address already in use`，而那正是"想边调代码边留着
# 容器当产品"的常用姿势。现在 `config.yaml` 写的是 18080（**代码后端**的口），容器靠镜像里的
# `ENV RAG_API__PORT=8080` 盖住它 —— 这条分工由 check_m6_deploy 的「端口分层」盯着。
# 两份后端**可以同时跑**（会话/向量/限流配额都在 MySQL/Milvus/Redis 里，不会各算一套），
# 代价是模型显存各占一份（实测单进程约 5G）；日志则必须分开写（见 DEV_LOG_DIR）：
# `logging.handlers.RotatingFileHandler` **不是多进程安全的**，两个进程轮转同一个 app.log 会丢行。
# 注意：**裸 `uv run project-rag-api` 不带这个隔离**（它按 config.yaml 写 logs/app.log，与容器共写一个
# 文件）；配置源只有一份 `.env`、被宿主与容器共用，所以"按角色分日志"这件事只能由命令来做 ——
# 想两个后端一起跑，就用 `./scripts/dev.sh backend`。
#
# 用法：
#   ./scripts/dev.sh            # 列出所有命令
#   ./scripts/dev.sh ci         # 零容器零网络：pyright + 26 套契约 + 26 份反向（CI 跑的就是这条）
#   ./scripts/dev.sh check      # 只跑契约
#   ./scripts/dev.sh reverse    # 只跑反向 runner
#   ./scripts/dev.sh services   # 需要真中间件的那 5 套（先 up）
#   ./scripts/dev.sh local      # 还需要真语料 / 真 DeepSeek key 的 3 套（花钱）
#   ./scripts/dev.sh all        # 全套 35 套契约 + 26 份反向
#   ./scripts/dev.sh up|down    # 起/停中间件（up 会先建好 ./logs 与 ./data/uploads）
#   ./scripts/dev.sh app [--build]  # 起**容器后端**（8080 API-only；--build 重建镜像）
#   ./scripts/dev.sh status     # 三个角色 + 容器/工作区的产物摘要 + 在跑的服务
#   ./scripts/dev.sh demo       # 灌演示语料（哈希嵌入，**不需要 4.4G 模型**）→ 见 demo/README.md
#   ./scripts/dev.sh demo-serve # 用演示配置起**代码后端**（默认 18080，零模型）
#   ./scripts/dev.sh frontend   # 前端 typecheck + build（dist + 兼容副本）
#   ./scripts/dev.sh backend    # 只起**代码后端**（默认 18080；不碰前端、不用镜像）
#   ./scripts/dev.sh serve      # backend 的旧名字，行为相同
#   ./scripts/dev.sh web [API]  # 起 Vite 前端（5173）；API 默认 18080，传 8080 连容器
#   ./scripts/dev.sh ports      # 端口分层 + 谁占着（带端口参数时：占用则退出 3）
#   ./scripts/dev.sh -n <命令>  # 只打印要跑的命令，不执行（**守卫照跑**，它不改判定）

set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# ── 端口分层的单一来源 ──────────────────────────────────────────────────────
# 这三个值被 check_m6_deploy.py 的「宿主代码后端与容器的端口分层」钉着：
# 改这里就得同步改那套检查里的期望（它会去 compose 里核对容器端口，不是抄一遍）。
CONTAINER_PORT=8080
DEV_BACKEND_PORT=18080
VITE_PORT=5173
# 代码后端的日志目录。**故意不是 config.yaml 里的 logs** —— 容器挂的是 ./logs，
# 两个进程写同一个文件会互相轮转掉（见文件头的说明）。
DEV_LOG_DIR="logs/dev"

print_only=0
if [ "${1:-}" = "-n" ]; then
  print_only=1
  shift
fi

run() {
  if [ "$print_only" = "1" ]; then
    printf '%s\n' "$*"
  else
    printf '\033[36m▸ %s\033[0m\n' "$*"
    "$@"
  fi
}

usage() {
  # 只打印文件头里的「用法：」那一段（把 # 去掉）—— 让 help 输出短而可复制
  sed -n '/^# 用法：/,/^#   \.\/scripts\/dev\.sh -n/p' "$0" | sed 's/^# \?//'
}

# ── 端口守卫：先拒绝、再解释，不把 Errno 98 丢给用户 ────────────────────────
port_in_use() {
  # 判据是「连不连得上」：bash 自带的 /dev/tcp，不需要 ss/lsof/python，也不需要 venv。
  # 已知盲区：只绑在非回环网卡上的服务会被判成空闲 —— 那时的兜底是 uvicorn 自己的 Errno 98。
  local port="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null || return 1
  return 0
}

port_holder() {
  # 只用来把话说清楚（"是谁占着"），不是判定依据：拿不到就返回空串。
  local port="$1"
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p "$" {print $6; exit}' || true
}

refuse_other_role() {
  # 静态守卫：不许把别的角色的端口当代码后端端口 —— 那不是"起不来"，是"起错了"。
  local port="$1"
  if [ "$port" = "$CONTAINER_PORT" ]; then
    cat >&2 <<EOF
✘ $port 是**容器后端**（API-only）的端口，不是代码后端的端口。
  想用容器后端：curl http://127.0.0.1:$CONTAINER_PORT/health（接口在 /api/v1，文档在 /docs）
  想起代码后端：./scripts/dev.sh backend（默认 $DEV_BACKEND_PORT），或 ./scripts/dev.sh backend <别的端口>
EOF
    return 2
  fi
  if [ "$port" = "$VITE_PORT" ]; then
    cat >&2 <<EOF
✘ $port 是 **Vite 前端**的端口，代码后端不用它。
  起前端：./scripts/dev.sh web（页面在 $VITE_PORT，默认连 $DEV_BACKEND_PORT；传 8080 连容器）
EOF
    return 2
  fi
  return 0
}

ensure_port_free() {
  local port="$1"
  port_in_use "$port" || return 0
  local holder
  holder="$(port_holder "$port")"
  cat >&2 <<EOF
✘ 端口 $port 已经被占用${holder:+（$holder）}，代码后端起不来（uvicorn 会抛 [Errno 98]）。
  看看是谁：./scripts/dev.sh ports
  换个端口：./scripts/dev.sh backend $(($port + 1))
EOF
  return 3
}

show_port_table() {
  # 角色名用 ASCII：中文按字节算宽度，printf 的 %-6s 对不齐（实测过一眼就歪）
  printf '%-10s %-6s %s\n' role port state
  while read -r role port; do
    if port_in_use "$port"; then
      local holder
      holder="$(port_holder "$port")"
      state="占用中${holder:+  $holder}"
    else
      state="空闲"
    fi
    printf '%-10s %-6s %s\n' "$role" "$port" "$state"
  done <<EOF
container $CONTAINER_PORT
dev $DEV_BACKEND_PORT
vite $VITE_PORT
EOF
  printf '\ncontainer = 容器后端（API-only；docker compose up -d app 之后就有，页面不在 8080）\n'
  printf 'dev       = 代码后端（./scripts/dev.sh backend / demo-serve，跑的是当前源码）\n'
  printf 'vite      = 前端开发服务器（./scripts/dev.sh web，只发页面）\n'
}

start_backend() {
  # 起**代码后端**：只跑源码，不构建前端、不用镜像。用法：start_backend [端口] [额外环境变量…]
  local port="${1:-$DEV_BACKEND_PORT}"
  shift || true
  refuse_other_role "$port" || exit 2
  ensure_port_free "$port" || exit 3
  printf '\033[36m▸ 代码后端：http://127.0.0.1:%s/（接口；兼容模式下也发 /static/app）\033[0m\n' "$port"
  printf '\033[36m▸ 前端开发：另开一个终端跑 ./scripts/dev.sh web %s\033[0m\n' "$port"
  printf '\033[36m▸ 日志：%s/app.log（与容器的 logs/app.log 分开写）\033[0m\n' "$DEV_LOG_DIR"
  if port_in_use "$CONTAINER_PORT"; then
    printf '\033[33m▸ 注意：容器后端也在跑（%s）。两边共用 MySQL/Milvus/Redis —— 数据与限流配额同源，\n  但模型显存各占一份（实测单进程约 5G），且改动是**同时对两边生效**的。\033[0m\n' "$CONTAINER_PORT"
  fi
  run env RAG_API__PORT="$port" RAG_LOG_DIR="$DEV_LOG_DIR" "$@" uv run project-rag-api
}

start_frontend() {
  # 起 Vite 前端。用法：start_frontend [后端端口，默认 18080]
  local api_port="${1:-$DEV_BACKEND_PORT}"
  if port_in_use "$VITE_PORT"; then
    cat >&2 <<EOF
✘ 端口 $VITE_PORT 已经被占用，Vite 起不来。
  看看是谁：./scripts/dev.sh ports $VITE_PORT
EOF
    exit 3
  fi
  printf '\033[36m▸ 前端：http://127.0.0.1:%s/（Vite；后端 http://127.0.0.1:%s）\033[0m\n' "$VITE_PORT" "$api_port"
  printf '\033[36m▸ 改前端只刷这个地址；改后端请另开终端跑 ./scripts/dev.sh backend\033[0m\n'
  run env VITE_DEFAULT_BACKEND="http://127.0.0.1:$api_port" bash -c 'cd web && npm run dev'
}

# ══ 「谁在发前端」这一组 ═════════════════════════════════════════════════════
# 为什么需要它们：同一个"启动"动作有**两个入口**，而它们起的服务集合不一样 ——
#   `./scripts/dev.sh up`        → 只起中间件（standalone / mysql / redis），**没有前端**
#   `docker compose up -d`       → 起 compose 里全部服务（含 app 与 attu），前端在 8080
# 于是"有时候起了前端、有时候没起"就有了第一种解释。第二种解释更要紧：**镜像里的前端产物是
# 构建那一刻的**，`npm run build` 只改工作区，容器照旧发旧产物 —— 前端"在"，但不是你改的那份。
# 这一组的职责就是把这两件事**当场读出来**（而不是让人靠记忆判断），并给出一条能走的路。

ARTIFACT_DIGEST="src/project_rag/api/static/app/source-digest.txt"
CONTAINER_DIGEST_PATH="/app/src/project_rag/api/static/app/source-digest.txt"

digest_short() {
  # 输入:（stdin）digest 文件内容。行为:取前 12 个字符。输出:短摘要。
  cut -c1-12
}

workspace_digest() {
  # 输入:无。行为:读工作区里前端产物的摘要。输出:12 位摘要;没构建过给一句人话。
  if [ -f "$ARTIFACT_DIGEST" ]; then
    digest_short <"$ARTIFACT_DIGEST"
  else
    printf '（工作区还没构建过前端）'
  fi
}

image_tag() {
  # 输入:无。行为:从 compose 里读 app 服务的镜像 tag（**不在这里再抄一份 tag**）。输出:tag。
  awk '/^  app:/{found=1} found && /^    image:/{print $2; exit}' docker-compose.yaml
}

running_services() {
  # 输入:无。行为:问 compose 现在哪些服务在跑。输出:服务名(每行一个);没有 docker 给空。
  command -v docker >/dev/null 2>&1 || return 0
  docker compose ps --services --status running 2>/dev/null || true
}

container_digest() {
  # 输入:无。行为:读**容器/镜像里**那份前端产物的摘要。输出:12 位摘要;读不到给空串。
  local tag
  if running_services | grep -qx app; then
    docker compose exec -T app cat "$CONTAINER_DIGEST_PATH" 2>/dev/null | digest_short
    return 0
  fi
  tag="$(image_tag)"
  if [ -n "$tag" ] && docker image inspect "$tag" >/dev/null 2>&1; then
    # 容器没起时也从**镜像**里读:它回答的是"如果现在 up,会看到哪一份前端"
    docker run --rm --entrypoint cat "$tag" "$CONTAINER_DIGEST_PATH" 2>/dev/null | digest_short
  fi
}

show_status() {
  # 输入:无。行为:把「三个角色 + 在跑的服务 + 两份产物摘要」一次说清。输出:无(只打印)。
  local http=""
  command -v curl >/dev/null 2>&1 && http=1
  printf '\033[36m▸ 三个角色（端口分层见文件头；谁在跑不等于谁在发你改的那份）\033[0m\n'
  local port role note code
  for pair in "$CONTAINER_PORT:容器后端（API-only；页面不在这一口）" \
              "$DEV_BACKEND_PORT:代码后端（工作区源码,兼容模式也发页面）" \
              "$VITE_PORT:Vite 前端（页面在这里，后端地址由脚本/页面配置）"; do
    port="${pair%%:*}"
    role="${pair#*:}"
    if port_in_use "$port"; then
      note="✔ 在跑"
      if [ -n "$http" ] && [ "$port" = "$CONTAINER_PORT" ]; then
        code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$port/health" || true)"
        note="✔ 在跑,API HTTP $code"
      fi
    else
      note="✘ 没在跑"
    fi
    printf '  %-6s %-42s %s\n' "$port" "$role" "$note"
  done

  local in_container in_workspace
  in_container="$(container_digest)"
  in_workspace="$(workspace_digest)"
  printf '\n\033[36m▸ 兼容前端产物（API-only 启动时**不发**；桌面壳/一体化模式才用）\033[0m\n'
  printf '  容器/镜像里  %s\n' "${in_container:-（读不到：镜像没构建过，或容器没起且镜像不在）}"
  printf '  工作区       %s\n' "$in_workspace"
  if [ -n "$in_container" ] && [ "$in_container" != "$in_workspace" ]; then
    printf '\033[33m  ⚠️ 两份不一致：镜像里的兼容产物是构建那一刻的前端；API-only 不受影响。\n     要一体化模式跟上工作区：./scripts/dev.sh frontend && ./scripts/dev.sh app --build\033[0m\n'
  elif [ -n "$in_container" ]; then
    printf '  ✔ 两份一致。\n'
  fi

  printf '\n\033[36m▸ compose 里在跑的服务\033[0m\n'
  local services
  services="$(running_services | tr '\n' ' ')"
  printf '  %s\n' "${services:-（一个都没在跑:./scripts/dev.sh up 起中间件,./scripts/dev.sh app 起容器后端）}"
  printf '\n  起中间件：./scripts/dev.sh up      起容器后端：./scripts/dev.sh app\n'
  printf '  起前端：./scripts/dev.sh web 8080   （连容器后端；连 18080 就不传参数）\n'
}

start_container_app() {
  # 起**容器后端**（API-only：镜像里的代码 + 兼容前端产物，但 compose 默认不发页面）。
  # 用法：start_container_app [--build]
  local build="${1:-}"
  run mkdir -p logs data/uploads
  if [ "$build" = "--build" ]; then
    # 容器默认只跑 API；前端由 ./scripts/dev.sh web 或独立静态服务器发。
    # 镜像里的兼容副本只服务“把 RAG_API__SERVE_FRONTEND 改回 true”的一体化模式。
    run docker compose build app
  fi
  run docker compose up -d app
  printf '\033[36m▸ 容器后端：http://127.0.0.1:%s/（API-only；健康检查 /health，文档 /docs）\033[0m\n' "$CONTAINER_PORT"
  printf '▸ 前端：./scripts/dev.sh web 8080（Vite 5173，连这个容器）\n'
  printf '▸ 现在谁在跑：./scripts/dev.sh status\n'
}

case "${1:-help}" in
  help | "")
    usage
    ;;
  setup)
    run uv sync --locked
    ;;
  up)
    # 先建两个**容器要写**的目录：`logs/` 与 `data/uploads/` 都在 .gitignore 里，新克隆的机器上
    # 不存在 → Docker 会替你建成 root 属主，容器里的 uid 1000 写不进去（日志静默失效、
    # 上传 EACCES）。自己建就没这问题。
    run mkdir -p logs data/uploads
    run docker compose up -d standalone mysql redis
    # **起完就说清"这一次没有前端"**：同一个"启动"动作有另一个入口（裸 docker compose up -d）
    # 会把 app 一起起起来 —— 两个入口的差别不写出来，用户只能靠"上次好像有页面"来判断。
    printf '\n\033[36m▸ 这次起的是**中间件 3 件**（standalone / mysql / redis）—— 这一步没有前端。\033[0m\n'
    printf '  要容器后端（%s，API-only）：./scripts/dev.sh app\n' "$CONTAINER_PORT"
    printf '  要当前源码后端（%s）：./scripts/dev.sh backend\n' "$DEV_BACKEND_PORT"
    printf '  要前端（5173）：./scripts/dev.sh web 8080（连容器）或 ./scripts/dev.sh web（连 18080）\n'
    printf '  现在谁在跑：./scripts/dev.sh status\n'
    ;;
  app)
    start_container_app "${2:-}"
    ;;
  status)
    show_status
    ;;
  down)
    run docker compose down
    printf '\033[36m▸ 已停掉 compose 里的全部服务（含 app 容器）。要回来：中间件 ./scripts/dev.sh up,'\
'容器后端 ./scripts/dev.sh app。\033[0m\n'
    ;;
  ci)
    run uv run pyright src scripts
    run uv run python scripts/acceptance/run_all.py --set ci
    ;;
  check)
    run uv run python scripts/acceptance/run_all.py --set ci --checks-only
    ;;
  reverse)
    run uv run python scripts/acceptance/run_all.py --set ci --reverse-only
    ;;
  services)
    run uv run python scripts/acceptance/run_all.py --set services
    ;;
  local)
    run uv run python scripts/acceptance/run_all.py --set local
    ;;
  all)
    run uv run python scripts/acceptance/run_all.py --set all
    ;;
  demo)
    # 演示模式：三个环境变量把它与生产配置分开（见 demo/README.md）——
    #   hashing  = 零模型的字节哈希嵌入（**非语义**，工厂会打 WARNING）
    #   rag_demo = 独立集合，不碰你真实的 rag_chunks
    #   rerank=false = 不加载 2.2G 重排模型
    # ⚠️ FAQ 那一步会往共享的 faq 表加 33 条演示问答；撤回命令在 demo/README.md
    #    （用 data/faq/qa_bagu_pdf_faq.csv --prune 拉回真实语料那 150 条，实测过）。
    run uv run python scripts/tools/load_faq_csv.py --csv demo/faq.csv
    run env RAG_EMBEDDING__BACKEND=hashing RAG_MILVUS__COLLECTION=rag_demo RAG_RAG__RERANK=false \
      uv run project-rag ingest --path demo/corpus
    printf '\033[36m▸ 接下来：./scripts/dev.sh demo-serve（浏览器打开 http://127.0.0.1:%s/）\033[0m\n' "$DEV_BACKEND_PORT"
    ;;
  demo-serve)
    start_backend "${2:-$DEV_BACKEND_PORT}" \
      RAG_EMBEDDING__BACKEND=hashing RAG_MILVUS__COLLECTION=rag_demo RAG_RAG__RERANK=false
    ;;
  frontend)
    run bash -c 'cd web && npm run typecheck && npm run build'
    ;;
  backend)
    start_backend "${2:-$DEV_BACKEND_PORT}"
    ;;
  web | frontend-dev)
    start_frontend "${2:-$DEV_BACKEND_PORT}"
    ;;
  typecheck)
    run uv run pyright src scripts
    ;;
  serve)
    start_backend "${2:-$DEV_BACKEND_PORT}"
    ;;
  ports)
    # 带端口参数时是**查询**（占用则退出 3，方便脚本里判），不带参数时是那张表。
    if [ -n "${2:-}" ]; then
      if port_in_use "$2"; then
        holder="$(port_holder "$2")"
        printf '✘ 端口 %s 被占用%s\n' "$2" "${holder:+（$holder）}" >&2
        exit 3
      fi
      printf '✔ 端口 %s 空闲\n' "$2"
      exit 0
    fi
    show_port_table
    ;;
  *)
    echo "不认识的命令：$1" >&2
    echo >&2
    usage >&2
    exit 2
    ;;
esac

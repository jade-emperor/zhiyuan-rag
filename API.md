# project-rag 接口文档

> 面向上层调用方的契约。**权威 schema 是运行时的 `/openapi.json`**（FastAPI 自动生成，
> 同时提供 `/docs` Swagger UI 与 `/redoc`）；本文档写 schema 表达不了的东西：
> 路由语义、WebSocket 帧协议、错误与降级、CLI 的 stdout / stderr 分工。
>
> 逐条契约（哪个行为被哪条检查钉住）在 `scripts/acceptance/check_m5_api.py` 与
> `check_m5_qa.py` 的 docstring 里；当前状态见 `docs/STATUS.md`。

---

## 一、起服务

```bash
uv run project-rag-api            # 默认 0.0.0.0:8080
```

绑定地址、CORS、启动预热都由配置决定（可被环境变量覆盖）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `api.host` / `api.port` | `0.0.0.0` / `8080` | 绑回环时 WSL 里的 Windows 浏览器连不上，故默认全网卡 |
| `api.cors_origins` | `["*"]` | 本机全开；**对外必须收紧成具体域名** |
| `api.warmup` | `true` | 启动时把嵌入 / 重排模型、语料索引、Milvus 连接先建好 |
| `api.warmup_query` | `装饰器有什么用` | 预热查询；**换语料领域时记得换这一句**，检索不到的预热等于没预热 |

启动顺序是「装日志 → 预热 → 起 uvicorn」，预热**失败不拦启动**（只记 warning，首个请求会自己补做）。
uvicorn **只起一个 worker**：嵌入 / 重排是进程内单例，多 worker 会各加载一份、内存翻倍。

其他相关配置：`conversation.history_rounds`（历史轮数，默认 5，读与写共用）、`faq.threshold`（默认 0.85）、
`rag.*`（检索旋钮）。配置优先级：**环境变量 > `.env` > `config/config.yaml`**，前缀 `RAG_`、嵌套用 `__`
（例如 `RAG_API__PORT=9000`）。

**鉴权（P0-3 起有了）**：业务接口要带 `Authorization: Bearer <令牌>`（`POST /api/v1/auth/login` 换），
WebSocket 走 `?token=<令牌>`（浏览器不能给 WS 设自定义头）；会话**绑在账号上**，读别人的会话是 `403`。
角色只有两种：`admin`（上传/删除文档、看报表）与 `employee`（问答）。详见 §3.4。

---

## 二、通用约定

- **Base URL**：`http://<host>:<port>`；请求与响应都是 JSON（UTF-8）。
- **路径**：业务接口**一律在 `/api/v1` 下**（2026-10-06 从 `/api/*` 统一迁完，**没有留别名** ——
  两套路径就是两个真值来源）。根上只有 `/health`（探活）、`/`（内置调试页）与 `/static`（它的资源）。
  这条有契约钉着（`check_m5_api` 的「命名空间」那条 + `reverse_m5_api.py`）。
- **无状态**：服务端不发 cookie；会话靠调用方保存 `session_id`，**令牌**靠调用方保存并每次带上
  （`Bearer`）。`session_id` 与令牌是两件事：前者指一段对话，后者证明你是谁。
- **`session_id` 不可猜**：`POST /api/v1/create_session` 发的是 `uuid4`，且**当场把归属绑到你账号上** ——
  光有 ID 也读不到别人的会话（`403`）。
- **错误码**：

| 码 | 什么时候 | 客户端该怎么处理 |
|---|---|---|
| `422` | 请求体不合法（缺 `query` / 空串 / `k < 1`），pydantic 拦在用例之前 | 改请求；这是编程错误，不是运行时故障 |
| `503` | 某个依赖工厂没装配（`{"detail": "<名字> 没装配:检查 build_real_deps"}`） | 服务端配置问题，重试无用 |
| `500` | 用例内部抛错（库连不上、模型加载失败…） | 读服务端日志；响应体不含堆栈 |

- **答不出来也是 200**：拒答时返回一句人话 + `refused: true`，**不是** `500`、**不是** `null`。
  前端因此永远拿到字符串，不用为「没有答案」写一支分支。

---

## 三、端点一览（13 条业务路由 + `/health` + 调试页）

「认证」一列说明**必须带什么**：`—` = 不需要；`Bearer` = 任意登录账号；`admin` = 管理员角色。

| 方法 | 路径 | 用途 | 认证 |
|---|---|---|---|
| `POST` | `/api/v1/query` | 非流式问答（**整条链路一次线程池调用**） | Bearer |
| `WS` | `/api/v1/stream` | 流式问答，逐帧推 `sources` / `token` / `done` | `?token=` |
| `POST` | `/api/v1/create_session` | 发一个新会话 ID（**当场绑归属**） | Bearer |
| `GET` | `/api/v1/history/{session_id}` | 读最近 N 轮历史 | Bearer + 归属 |
| `DELETE` | `/api/v1/history/{session_id}` | 清空一段会话 | Bearer + 归属 |
| `GET` | `/api/v1/sources` | 语料里真实有哪些来源 | Bearer |
| `POST` | `/api/v1/auth/login` | 账号密码换令牌 | — |
| `GET` | `/api/v1/auth/me` | 当前账号与角色（前端据此显示/隐藏按钮） | Bearer |
| `GET` | `/api/v1/capabilities` | 能吃什么后缀、单文件上限（**上传框的 `accept` 从这来**） | Bearer |
| `POST` | `/api/v1/documents?filename=…` | 上传一份文档（**原始 body 流式**，返回 202） | admin |
| `GET` | `/api/v1/documents` | 已登记文档清单（新→旧） | Bearer |
| `DELETE` | `/api/v1/documents/{document_id}` | 删一份文档（登记行 + 向量 + 原文件） | admin |
| `GET` | `/api/v1/admin/metrics` | 延迟报表（p50/p95/p99、冷热、最慢榜） | admin |
| `GET` | `/health` | 进程探活 | — |
| `GET` | `/` | 内置最小前端页（用 WebSocket 逐字显示） | — |
| `GET` | `/static/{path}` | 静态文件挂载（`api/static/`） | — |

### 3.1 `POST /api/v1/query`

请求（`QueryRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | ✅ | 用户问题，**长度至少 1** |
| `session_id` | string \| null | | 给了就带对话历史（读最近 N 轮进提示词，并把这一轮记下来） |
| `source_filter` | string \| null | | 按来源过滤，取值来自 `/api/v1/sources`（如 `bagu_ai`） |
| `k` | int \| null | | 每一路的召回条数；不给取配置 `rag.retrieval_k`，给就得 ≥ 1 |

```bash
curl -s http://127.0.0.1:8080/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "装饰器有什么用？"}'
```

响应（`QueryResponse`）：

```json
{
  "answer": "装饰器是给函数加行为的语法……[1]",
  "channel": "rag",
  "refused": false,
  "truncated": false,
  "citations": [
    {
      "index": 1,
      "source": "bagu_ai",
      "doc_id": "JBL常见AI面试题-潜艇.md",
      "preview": "……父块正文的前 120 字……",
      "score": 0.83,
      "rerank_score": 4.12,
      "page": 37
    }
  ],
  "faq_score": null,
  "session_id": null
}
```

| 字段 | 说明 |
|---|---|
| `answer` | 永远是非空字符串（拒答话术也在内） |
| `channel` | `faq`（命中标准答案）/ `rag`（知识库）/ `greeting`（寒暄拦截，比通道多这一种） |
| `refused` | 检索为空 → **直接拒答、不调模型**，此时 `citations` 为空 |
| `truncated` | 模型撞到 `max_tokens` 被截断：**半截答案照原样返回**，只用这个字段提示，不悄悄换成拒答话术 |
| `citations[].preview` | 父块正文前 120 字 —— 与**模型真正读到的那段**一致 |
| `citations[].score` | 融合分；`rerank_score` 是重排分（关掉重排或候选只有 1 条时为 `null`） |
| `citations[].page` | 这条引用在源文件的第几页（**从 1 开始**，P0-5）。`null` = 不知道：md/txt 没有页码概念，或者块是加 `page` 列之前入库的 —— **前端按「页码未知」显示，别拿 0 或 1 顶上** |
| `faq_score` | 仅 FAQ 命中时有值（`top1 原始分 ÷ 该条问自己的分`） |

**路由规则**（HTTP 与 WS 共用一份，实现在 `application/answer_question.py`）：

```text
整句只是寒暄 ──→ 直接回话术（不查 FAQ、不检索、不写历史）
   │ 否则
FAQ 匹配 ≥ faq.threshold(0.85) ──→ 标准答案直答（channel=faq）→ 记历史
   │ 否则
知识库检索 ──→ 有命中：检索 + 生成 + [n] 引用（channel=rag）→ 记历史
             └ 无命中：拒答话术（refused=true，不调模型）→ 记历史
```

> FAQ 那半边是**要用时才构造的零参函数**：FAQ 命中的问题连嵌入 / 重排模型都不会加载。

### 3.2 `WS /api/v1/stream`

连上后**客户端逐条发 JSON**（形状同 `QueryRequest`），服务端逐帧回 JSON。
**同一条连接可以连续问多轮**（FAQ 命中、问候、请求体畸形都不会关连接）。

| 帧 | 何时 | 字段 |
|---|---|---|
| `sources` | 知识库通道检索完成（0 或 1 帧，**在 token 之前**） | `citations: CitationOut[]` |
| `token` | 每一小段答案 | `text: string` |
| `done` | 恰好 1 帧收尾 | `answer` / `channel` / `refused` / `truncated` / `citations` / `session_id` |
| `error` | 请求体不是合法 JSON 或校验失败 | `detail: string`（连接**留着**，可以重发） |

**恒等式**：`done.answer == "".join(所有 token 帧的 text)`。答案**一律走 token 帧** ——
标准答案、拒答话术都在内，前端只要「打印 token」，不用为「这句话从哪来」写分支。

**非流式就是流式的收拢器**：`POST /api/v1/query` 与 `WS /api/v1/stream` 打印出来的答案逐字相同。

```javascript
const ws = new WebSocket("ws://127.0.0.1:8080/api/v1/stream");
ws.onopen = () => ws.send(JSON.stringify({ query: "装饰器有什么用？" }));
ws.onmessage = (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "sources") renderSources(frame.citations);   // 依据先到，先显示
  else if (frame.type === "token") appendText(frame.text);
  else if (frame.type === "done") finish(frame);
  else if (frame.type === "error") showError(frame.detail);
};
```

### 3.3 会话与来源

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/create_session
# {"session_id": "b7f0…"}          ← uuid4，同时是访问这段历史的凭证

curl -s 'http://127.0.0.1:8080/api/v1/history/b7f0…'
# {"session_id": "b7f0…", "rounds": [{"question": "…", "answer": "…"}]}
#   最近 N 轮（默认 5），按时间正序（旧 → 新），与喂给模型的是同一份

curl -s -X DELETE 'http://127.0.0.1:8080/api/v1/history/b7f0…'
# {"session_id": "b7f0…", "deleted": 3}

curl -s http://127.0.0.1:8080/api/v1/sources
# {"sources": ["bagu_ai", "bagu_java"]}   ← 读向量库里的**真实**来源，不是配置白名单
```

`GET /health` 只回答「服务在不在」（`{"status": "ok"}`）——**刻意不探容器**：
Milvus / MySQL / Redis 的连通性由 `scripts/acceptance/check_env_services.py` 负责。

### 3.4 认证与角色（P0-3a）

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "demo_admin", "password": "…"}' | python -c 'import json,sys;print(json.load(sys.stdin)["token"])')

curl -s http://127.0.0.1:8080/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
# {"username": "demo_admin", "role": "admin", "is_admin": true}
```

| | |
|---|---|
| 令牌 | `POST /api/v1/auth/login` 发，**有效期 12 小时**（`security.TOKEN_TTL_SECONDS`）；库里存的是它的 **sha256**，不是令牌本身 |
| 带法 | HTTP：`Authorization: Bearer <令牌>`；WebSocket：`?token=<令牌>`（浏览器不能给 WS 设自定义头）。代价是令牌会进服务器访问日志 |
| 角色 | `admin`：上传/删除文档、看报表；`employee`：问答与会话。**没有注册接口** —— 建号走 `project-rag user add` |
| 401 / 403 | 没带令牌或令牌无效 → `401`；令牌有效但**不是你的东西**（别人的会话、要管理员的操作）→ `403` |
| 登录失败 | 一律 `401` + **同一句话**（用户不存在与口令错误不分开说 —— 那是用户名枚举） |
| WS 被拒 | 先回一帧 `{"type":"error",…}`，再 `close(1008)` —— 光 close 的话前端不知道是令牌过期还是服务挂了 |
| 会话归属 | `create_session` 当场绑定；**没主人的会话也拒绝**（`owner_of` 的 `None` 既可能是老会话也可能是瞎编的 ID，分不清就别放行） |
| 无认证模式 | 只有**显式**不注入 `auth_store` 时才成立（`build_real_deps` 永远接上）：此时老接口照常 200，`/api/v1/auth/*` 给 `503` |

### 3.5 文档治理（P0-3b，前端上传/列表/删除用）

```bash
curl -s http://127.0.0.1:8080/api/v1/capabilities -H "Authorization: Bearer $TOKEN"
# {"suffixes": [".docx", ".md", ".pdf", ".pptx", ".txt"], "max_upload_mb": 50}
#   ← 上传框的 accept 与大小上限**从这个接口来**，别写死在前端

curl -s -X POST "http://127.0.0.1:8080/api/v1/documents?filename=lecture01.pdf" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/octet-stream' \
  --data-binary @lecture01.pdf
# 202 {"document_id": "lecture01.pdf", "source": "uploads", "status": "pending", …}

curl -s http://127.0.0.1:8080/api/v1/documents -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE http://127.0.0.1:8080/api/v1/documents/lecture01.pdf -H "Authorization: Bearer $TOKEN"
# {"document_id": "lecture01.pdf", "deleted_chunks": 42, "removed_file": true, "removed_record": true}
```

四条口径，前端照这个写：

1. **上传走原始 body**（`?filename=` 传名字，不要 `multipart/form-data`）—— 请求体是**边收边写**的，
   超限当场 `413`，不会先把 50MB 读进内存再拒绝；
2. **入库是异步的**：接口立刻 `202` + `status: "pending"`，交给后台任务切块嵌入
   （539 页 PDF 在 CPU 上要 8 分钟，同步 HTTP 必超时）。状态流转
   `pending → processing → processed`，失败进 `failed`（`error` 字段带原因）；
   **只有 `processed` 是可检索的**（`searchable` 字段就是它）；
3. **文件名会被 basename 化**：`../../逃逸.md` 存成 `逃逸.md`，写不到 uploads 目录之外；
   不认识的后缀在上传时就被 `capabilities.suffixes` 拦掉（`415`）；
4. **删除是幂等的**：文档不存在也 `200`，只是三个字段都是 0/false —— 重复点删除按钮不该报错。
   删除顺序是「先标记 `deleting` → 删向量 → 删原文件 → 删登记行」：**向量删失败就不动登记行**，
   宁可留一条看得见的记录，也不要「表里没了、库里还在」的孤儿。

### 3.6 报表（P0-2）

```bash
curl -s "http://127.0.0.1:8080/api/v1/admin/metrics?window=24h&top=5" -H "Authorization: Bearer $TOKEN"
```

`window` 只认 `1h / 6h / 24h / 7d / 30d`（别的给 `400`）；没接账本表给 `503`（**读不到 ≠ 是空的** ——
分不清这两件事就会把「MySQL 挂了」读成「系统没流量」）。分位数口径（最近秩不插值、
`p95` 要 n≥20、`-` 不折算成 0、冷行不进分段统计）见 `docs/STATUS.md` §7 P0-2。

---

## 四、CLI 接口

十一个子命令，与 HTTP 共用同一套用例与组合根。`uv run project-rag <子命令> --help` 看参数。

| 子命令 | 必填 | 可选 | 行为 |
|---|---|---|---|
| `chat` | `-p/--prompt` | — | 裸问模型，不检索（**唯一不依赖任何中间件**的命令） |
| `faq` | `-q/--question` | — | 只走 FAQ 通道：命中直答，未命中交给模型兜底 |
| `rag` | `-q/--question` | `--k` `--source` | 只走知识库通道：检索 + 生成 + `[1][2]` 引用 |
| `qa` | `-q/--question` | `--k` `--source` `--session` `--stream` | **合流**：FAQ 优先、知识库兜底（日常入口） |
| `search` | `-q/--question` | `--k` `--source` `--route {both,vector,bm25}` | 只检索、**不调模型**；`both` 打四段：向量 / BM25 / 融合 / 重排 |
| `ingest` | — | `--path` `--dry-run` `--purge-source` `--batch-size` | 切块入库；默认路径 `data/corpus/pdf_data` |
| `collections` | — | — | 列出向量集合（块数 / 有没有 `page` 列 / 当前配置指向哪个）**纯读** |
| `drop` | `--collection` | `--yes` | 删掉一个向量集合；**不给 `--yes` 只说不做**（退出码 1） |
| `collections` | — | — | 列出向量集合（块数 / 有没有 `page` 列 / 当前配置指向哪个）**纯读** |
| `drop` | `--collection` | `--yes` | 删掉一个向量集合；**不给 `--yes` 只说不做**（退出码 1） |

约定：

- **退出码**：`0` 成功、`1` 业务失败（错误短消息在 stderr + 详情进日志）、`2` 用法错误（argparse）。
- **stdout 只放结果**（答案、引用清单、统计），**提示一律走 stderr**（截断提醒、跳过清单）—— 便于管道消费。
  首行永远是答案；「这个答案的来历」排在后面。
- **`ingest --dry-run`**：只加载 + 切分 + 打印块长分布，**不加载模型、不写库**，调参时先用它。
  认识的后缀：`.md` / `.txt` / `.docx` / `.pptx` / `.pdf`；不认识的文件与「认了但没抽出文字」的文件
  （扫描件）会在 stderr 里各自报一行 —— **少入库一份文档必须让人当场看见**。
- **`ingest` 是幂等的**：主键 = `sha256(来源|文档|位置|内容)`，重灌同一份语料条数不变、块 id 不变；
  改了内容再灌会按 `doc_id` 清掉旧块（目录里删了文件用 `--purge-source` 整份下架）。
- **`search` 排障顺序**：答案不对时先 `search` 再 `rag` —— 分清是「没捞到」还是「答不好」。

---

## 五、观测：一次回答一行账

每次回答（HTTP / WS 的问答，以及 CLI 的 `qa`、`rag`）都会在 `logs/app.log` 写一行 INFO 的 latency 账，
**26 个字段**，拿不到的记 `-`（「没量到」与「量到 0」分得开）。前三个是 P0-2 加的观测维度：
`request_id`（一次请求一个，端到端串起来）、`cold` / `builds`（这一轮**真的构造过**重资源吗 —— 
它是「冷启动拖慢 p95」这件事唯一的判据，判据是构造动作，不是计时）。
`chat` 与 `faq` 单通道命令**不记**（账本挂在合流 / 知识库这两条帧流上，见 `docs/deferred.md` §1.3 第 34 条）：

```bash
grep -h " | latency " logs/app.log | tail -5
# label=rag channel=rag request_id=97269b3ba505 thinking=off cold=false builds=0 faq_ms=- sources_ms=6308.4 retrieve_ms=6308.4 embed_ms=176.4
#   vector_ms=112.8 bm25_ms=1095.5 fuse_ms=0.0 collapse_ms=0.0 rerank_ms=4923.6
#   llm_think_ms=- llm_ttft_ms=422.7 think_tokens=0 ttft_ms=6731.2 tokens=54 gen_ms=345.4
#   tps=156.4 total_ms=7077.9 refused=false truncated=false aborted=false
```

字段口径（检索六段之和 ≈ `retrieve_ms`、`thinking=off` 与「压根没调模型」怎么区分）见
`README.md` 的「慢在哪：一行日志」与 `docs/STATUS.md` §0.5。

---

## 六、版本与兼容

- 版本号在 `pyproject.toml`（当前 `0.1.0`）与 `FastAPI(version=…)` 里各写一次，改动要同时改。
- **公开 schema 的权威是 `/openapi.json`**：改 `api/schemas.py` 后它自动跟着变，
  本文档若与它冲突，以 `/openapi.json` 为准并回来改本文档。
- 兼容性承诺：**只增不改** —— 新增字段给默认值；删字段 / 改类型 / 改路由属于破坏性变更，要同步改
  `scripts/acceptance/check_m5_api.py`（那份脚本是端点契约的唯一权威）。
- **路由前缀**：业务接口一律 `/api/v1`。2026-10-06 从 `/api/*` 一次性迁完并**删掉了老路径**
  （那时还没有任何真实客户端，是唯一不用背兼容包袱的窗口）。映射：`/api/query`→`/api/v1/query`、
  `/api/stream`→`/api/v1/stream`、`/api/create_session`→`/api/v1/create_session`、
  `/api/history/{id}`→`/api/v1/history/{id}`、`/api/sources`→`/api/v1/sources`、
  `/api/metrics`→`/api/v1/admin/metrics`。有契约（命名空间）与反向测试盯着。

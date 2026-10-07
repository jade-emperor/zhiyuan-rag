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
uv run project-rag-api            # 默认 0.0.0.0:18080
```

绑定地址、CORS、启动预热都由配置决定（可被环境变量覆盖）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `api.host` / `api.port` | `0.0.0.0` / `18080` | 绑回环时 WSL 里的 Windows 浏览器连不上，故默认全网卡 |
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

浏览器打开 **`http://<host>:<port>/`** 就是前端 app（307 → `/static/app/`）：
左侧会话列表、中间流式回答与引用页码、右侧检索参数（知识库 / 检索方式 / 两路权重 / 重排 / 模型）。

前端的源码在 `web/`（Vite + Vue3 + TS），**构建产物入库**在 `src/project_rag/api/static/app/` ——
所以 Python 侧与 Docker 镜像都不需要 node；改了前端要记得重新构建：

```bash
cd web && npm install          # 只第一次
cd web && npm run build        # 产物 + source-digest.txt（产物"来自哪一版源码"的摘要）
cd web && npm run dev          # 开发态（5173），后端地址在页面左上角填
```

app **只需要知道一个后端地址**（默认 = 页面自己的 origin），换后端不用重新构建。
计划、里程碑与接口对齐表见 `docs/frontend.md`。

---

## 二、通用约定

- **Base URL**：`http://<host>:<port>`；请求与响应都是 JSON（UTF-8）。
- **路径**：业务接口**一律在 `/api/v1` 下**（2026-10-06 从 `/api/*` 统一迁完，**没有留别名** ——
  两套路径就是两个真值来源）。根上只有三个：`/health`（探活）、`/`（307 跳前端 app）、
  `/static`（静态资源：前端产物在 `/static/app/`、内置调试页在 `/static/index.html`）。
  这条有契约钉着（`check_m5_api` 的「命名空间」那条 + `reverse_m5_api.py`）。
- **无状态**：服务端不发 cookie；会话靠调用方保存 `session_id`，**令牌**靠调用方保存并每次带上
  （`Bearer`）。`session_id` 与令牌是两件事：前者指一段对话，后者证明你是谁。
- **`session_id` 不可猜**：`POST /api/v1/create_session` 发的是 `uuid4`，且**当场把归属绑到你账号上** ——
  光有 ID 也读不到别人的会话（`403`）。
- **错误码**：

| 码 | 什么时候 | 客户端该怎么处理 |
|---|---|---|
| `422` | 请求体不合法（缺 `query` / 空串 / `k < 1`），pydantic 拦在用例之前 | 改请求；这是编程错误，不是运行时故障 |
| `429` | 触发限流（问答 / 上传 / 登录各一档，见 §3.7） | **按 `Retry-After` 里的秒数等**，别立刻重试（重试必然又被拒） |
| `503` | 某个依赖工厂没装配（`{"detail": "<名字> 没装配:检查 build_real_deps"}`） | 服务端配置问题，重试无用 |
| `500` | 用例内部抛错（库连不上、模型加载失败…） | 读服务端日志；响应体不含堆栈 |

- **答不出来也是 200**：拒答时返回一句人话 + `refused: true`，**不是** `500`、**不是** `null`。
  前端因此永远拿到字符串，不用为「没有答案」写一支分支。

---

## 三、端点一览（16 条业务路由 + `/health` + 前端 app + 调试页）

「认证」一列说明**必须带什么**：`—` = 不需要；`Bearer` = 任意登录账号；`admin` = 管理员角色。

| 方法 | 路径 | 用途 | 认证 |
|---|---|---|---|
| `POST` | `/api/v1/query` | 非流式问答（**整条链路一次线程池调用**） | Bearer |
| `WS` | `/api/v1/stream` | 流式问答，逐帧推 `sources` / `token` / `done` | `?token=` |
| `POST` | `/api/v1/create_session` | 发一个新会话 ID（**当场绑归属**） | Bearer |
| `GET` | `/api/v1/history/{session_id}` | 读最近 N 轮历史 | Bearer + 归属 |
| `DELETE` | `/api/v1/history/{session_id}` | 清空一段会话 | Bearer + 归属 |
| `GET` | `/api/v1/sources` | 语料里真实有哪些来源 | Bearer |
| `GET` | `/api/v1/sessions` | **我名下的**会话目录（标题/轮数/最近活动，M7.1 加） | Bearer |
| `POST` | `/api/v1/auth/login` | 账号密码换令牌 | — |
| `POST` | `/api/v1/auth/logout` | **撤销当前令牌**(不只是前端删本地副本;幂等) | Bearer(可缺) |
| `GET` | `/api/v1/auth/me` | 当前账号与角色（前端据此显示/隐藏按钮） | Bearer |
| `GET` | `/api/v1/capabilities` | 后缀 / 上限 / 模型 / 检索选项与上限（**前端唯一的能力来源**） | — |
| `POST` | `/api/v1/documents?filename=…` | 上传一份文档（**原始 body 流式**，返回 202） | admin |
| `GET` | `/api/v1/documents` | 已登记文档清单（新→旧） | Bearer |
| `DELETE` | `/api/v1/documents/{document_id}` | 删一份文档（登记行 + 向量 + 原文件） | admin |
| `GET` | `/api/v1/documents/{document_id}/chunks` | 预览入库后的样子（折叠成**父块**、带页码，M7.2 加） | Bearer |
| `GET` | `/api/v1/admin/metrics` | 延迟报表（p50/p95/p99、冷热、最慢榜） | admin |
| `GET` | `/health` | 进程探活 | — |
| `GET` | `/` | **307 → `/static/app/`**（前端 app，M7.1 起） | — |
| `GET` | `/static/app/` | 前端产物（Vite+Vue3+TS 构建；hash 路由，无需服务端 rewrite） | — |
| `GET` | `/static/index.html` | 内置调试页（窄、无依赖；后端出问题时它比 SPA 更有用） | — |
| `GET` | `/static/{path}` | 静态文件挂载（`api/static/`） | — |

### 3.1 `POST /api/v1/query`

请求（`QueryRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | ✅ | 用户问题，**长度至少 1** |
| `session_id` | string \| null | | 给了就带对话历史（读最近 N 轮进提示词，并把这一轮记下来） |
| `source_filter` | string \| null | | 按来源过滤，取值来自 `/api/v1/sources`（如 `bagu_ai`） |
| `k` | int \| null | | 每一路的召回条数；不给取配置 `rag.retrieval_k`。范围 1..`capabilities.retrieval.limits.k_max` |
| `mode` | string \| null | | **检索方式**（M7.1）：`vector` / `keyword` / `hybrid`。不给 = 配置里的两路权重 |
| `fusion` | string \| null | | 融合方式：`rrf`（只用排名）/ `weighted`（各自 min-max 归一化后加权） |
| `bm25_weight` | float \| null | | 关键字那一路的权重，`0` = **关掉这一路**（连检索都不做），范围 0..1 |
| `vector_weight` | float \| null | | 向量那一路的权重，同上 |
| `rerank` | bool \| null | | 要不要重排；不给取配置 `rag.rerank`。**这个部署没装重排器时给 `true` → 400**（不静默降级） |
| `max_context` | int \| null | | 最多几条父块进上下文；不给取配置 `rag.candidate_m` |
| `rrf_k` | int \| null | | RRF 的平滑常数；不给取配置 `rag.rrf_k` |

**两条会 422 的组合**（都是"两个真值来源必然打架"）：`mode` 与 `bm25_weight`/`vector_weight`
**不能同时给**（模式已经决定了跑哪几路）；两个权重**不能同时为 0**（那等于什么都不检索，
而结果看起来只是"知识库里没有"）。取值范围与 `capabilities.retrieval.limits` 同源 ——
前端照它夹取，就不会夹到一个后端拒绝的值。

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
| `citations[].score` | 融合分；`rerank_score` 是重排分（关掉重排或候选只有 1 条时为 `null`）。**单路检索时它就是那一路的原始分**（向量余弦或 BM25 分），量纲与融合分不同，别拿来比 |
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
// 开了认证要带令牌:浏览器不能给 WS 设自定义头,所以只能走 query。
// 认证失败时服务端**先回一帧 error 说明原因再 close(1008)** —— 光看 close 分不清
// "令牌过期"和"服务挂了"。
const ws = new WebSocket("ws://127.0.0.1:8080/api/v1/stream?token=<令牌>");
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
# 我名下的会话目录(M7.1 加):标题 = 首问裁到 40 字(后端裁,前端不再截第二次)
curl -s -H "Authorization: Bearer <令牌>" 'http://127.0.0.1:8080/api/v1/sessions?limit=50'
# {"sessions": [{"session_id": "b7f0…", "title": "每周加班最多多长时间",
#                "rounds": 3, "created_at": "2026-10-06T09:00:00", "updated_at": "2026-10-06T09:30:00"}]}
#   ↑ **只列有轮次的会话**(前端"首次提问时才建会话",刚建没问过的不占地方);
#     按最近活动倒序;无认证模式给空表(没有账号时"这是谁的会话"没有答案);
#     认证开着却没装配会话目录 → 503(不拿空表冒充"你还没有会话")
```

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
| 无认证模式 | 只有**显式**不注入 `auth_store` 时才成立（`build_real_deps` 永远接上）：此时老接口照常 200，`/api/v1/auth/*` 给 `503`，`/api/v1/sessions` 给空表 |
| 前端只是"不给点" | 界面按角色隐藏管理入口（M7.1），但**权限在后端**：每个管理接口各自 `require_admin`。藏起来 ≠ 安全 |

### 3.5 文档治理（P0-3b，前端上传/列表/删除用）

```bash
curl -s http://127.0.0.1:8080/api/v1/capabilities -H "Authorization: Bearer $TOKEN"
# {"suffixes": [".docx", ".md", ".pdf", ".pptx", ".txt"], "max_upload_mb": 50,
#  "models": [{"id": "deepseek-flash", "label": "deepseek-flash", "default": true}],
#  "model_settings": {"temperature": 0.7, "thinking": "off"},
#  "retrieval": {"modes": ["vector", "keyword", "hybrid"], "fusions": ["rrf", "weighted"],
#                "defaults": {…配置里生效的那一份…}, "limits": {"k_max": 50, …},
#                "rerank_available": true},
#  "metrics": {"windows": ["1h", "6h", "24h", "7d", "30d"], "default_window": "24h",
#              "top_max": 50, "min_n": {"p50": 1, "p95": 20, "p99": 100}},
#  "roles": ["employee", "admin"]}
#   ← 上传框的 accept、参数面板的下拉项与上限、模型下拉**全从这来**，别写死在前端:
#     写两份的后果是"界面允许、后端拒绝"（或反过来:后端加了融合方式，前端下拉框里没有）。
#     这个接口**不需要登录**（不含任何数据），登录页也要用它 —— 后端地址填错时，
#     第一个报错应该发生在这一步（"连不上"），而不是"密码不对"。
#   ⚠️ `model_settings` 是**只读展示**:temperature / thinking 是服务端启动期配置，
#     按请求覆盖会绕过配置与延迟账的口径（触发条件见 docs/frontend.md §5）。

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
   不认识的后缀在上传时就被 `capabilities.suffixes` 拦掉（**`400`** —— 契约里钉的是 400，
   这里原来写着 415，是文档漂了；以 `check_p0_3_auth` 那条断言为准）；
4. **删除是幂等的**：文档不存在也 `200`，只是三个字段都是 0/false —— 重复点删除按钮不该报错。
   删除顺序是「先标记 `deleting` → 删向量 → 删原文件 → 删登记行」：**向量删失败就不动登记行**，
   宁可留一条看得见的记录，也不要「表里没了、库里还在」的孤儿。

### 3.5.1 文档预览（M7.2）

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8080/api/v1/documents/handbook.pdf/chunks?limit=20&chars=400'
# {"document_id": "handbook.pdf", "parent_blocks": 15, "child_chunks": 42, "returned": 15,
#  "truncated": false,
#  "blocks": [{"index": 1, "page": 7, "last_page": 8, "chars": 1180,
#              "text": "……这一段的前 400 字……"}]}
```

四条口径（每条都对应一种"看着对但其实错了"的读法）：

| | |
|---|---|
| **显示的是父块** | 向量库里写的是**子块**，父块正文挂在每个子块的 `parent_content` 上 —— 所以按 `parent_id` **折叠**回去。折叠出来的那段才是模型真正读到的东西 |
| **两个数都给** | `child_chunks`（入库写进去的）与 `parent_blocks`（折叠后的）差着切分比例；只给一个，界面上的"入库 42 块 / 预览 15 段"会被读成丢数据 |
| **`chars` 是截断前的字数** | 显示"1180 字"而正文只有 400 字，用户才知道后面还有 |
| **页码是首尾** | `page` / `last_page` 不同 = 这一段**跨页**（真语料 22.9% 的块跨页，口径见 P0-5）；都是 `null` = 不知道（md/txt），**不许拿 0 顶** |

`limit` ∈ [1, 100]、`chars` ∈ [50, 2000]；越界 `422`。
**权限与清单一致（Bearer，不是 admin）**：员工本来就能看清单（他要知道自己能检索什么），
预览是同一份数据的另一种看法；真正的管理动作（上传 / 删除）才是 admin。
**没登记的文档给 `404`**，而且话术点名 `project-rag doctor` 与 `--prune-orphans` ——
库里的孤儿块照样能被检索到，但"这份文档"在管理页上不存在。

**顺序只保证稳定，不保证与原文一致**：块里没有偏移列（`chunk_id` 是摘要），
只能按「先有页码的、页码升序」排。PDF 基本就是原文顺序，md/txt 只能保证两次看到的一样；
要做到严格原文顺序得给向量库加一列偏移（schema 变更 + 整库重灌，见 `docs/deferred.md`）。

### 3.6.1 限流、CSP 与 CORS（M7.4）

**限流**是**滑动窗口**,计数默认放在 **Redis**(`adapters/ratelimit_redis.py`),
每一档按**身份**计数:登录那一刻按来源 IP,登录之后按账号
(否则同一个办公室的同事会互相挤占额度)。

| 档 | 默认 | 打在哪 |
|---|---|---|
| `query` | 60 次/分钟 | `POST /api/v1/query` **与** `WS /api/v1/stream` 的每条消息(**两条入口一份额度**) |
| `upload` | 10 次/分钟 | `POST /api/v1/documents`(一次上传背后是几分钟 CPU 嵌入) |
| `login` | 10 次/分钟 | `POST /api/v1/auth/login`(防口令爆破);**登录成功会清掉本 IP 的计数** |

**为什么在 Redis**(而不是进程内存):重启不清零、多 worker / 多副本共享一份计数、
**时钟以 Redis 服务端为准**(多机时钟不同步也不会各算各的窗口)。
判定是**一段 Lua**:"裁剪过期成员 → 计数 → 判定 → 写入 → 设 TTL"一次往返原子完成 ——
分开两步(先查数量再写入)在并发下会多放行,而限流最不该错的就是这里
(契约里有一条:20 个并发抢 5 个名额,恰好 5 个通过)。

**存储故障 → 降级放行(fail-open)** + 一条 WARNING(同一进程每个窗口最多一条)+
`degraded` 计数。为什么不 fail-closed:限流是**保护**不是正确性,为一道闸把整个问答停掉,
是把"可能被刷"换成"一定不可用"(与缓存降级同一个判断)。代价照实说:**Redis 挂着的这段时间,
爆破与刷量是不设防的** —— 所以必须有日志痕,而不是悄悄过去。

**配置**(`api.rate_limit.*`,环境变量可覆盖):

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | `false` = 完全不接限流器(三档都放行) |
| `backend` | `redis` | `redis`(共享计数)/ `memory`(单进程内存,重启清零、多 worker 各算各的) |
| `key_prefix` | `ratelimit:v1:` | 键的**命名空间 + 版本号**,同一个 Redis 被别的应用共用时不互相踩 |
| `window_s` | `60` | 窗口秒数 |
| `query` / `upload` / `login` | `60` / `10` / `10` | 每档上限;**`<= 0` = 关掉这一档**(不是"限 0 次") |

**超限之后回什么,按路径分三种**(2026-10-06 作者定:**问答要兜底话术,不要错误码**):

| 路径 | 超限回什么 | 为什么 |
|---|---|---|
| `POST /api/v1/query` | **`200` + 兜底话术**:`channel: "throttled"`、`answer` 里带等待秒数与这一档的上限、`citations: []`;响应头仍带 `Retry-After` | 问答这条路上"被限流"对用户就是一句"稍后再试";做成错误码等于让界面上多一条红字 |
| `WS /api/v1/stream` | 同一个形制:`token` 帧 + `done` 帧(`channel: "throttled"`),**连接不断** | 与问候那条前置拦截一致(断连接会让前端以为服务挂了) |
| `POST /auth/login`、`POST /api/v1/documents` | **`429` + `Retry-After`** | 登录的兜底话术会看着像"登录成功"(安全上最糟的一种回法);上传要的是"为什么没传上去",不是一段"回答" |

被限流的那一轮**不写进会话历史**(那不是这一轮问答的内容 —— 与寒暄同一条口径),
也**不进延迟账本**(压根没进用例,没有 `total_ms` 那一行),只在日志里留一条 WARNING。

限流的键是 socket 对端地址,**不认 `X-Forwarded-For`** ——
没有可信反代时那个头谁都能伪造,认了等于送一个"改个头就绕过限流"的开关。

**CSP**：前端产物（`/static/app/**`）带 `Content-Security-Policy`：
`script-src 'self'`、`object-src 'none'`、`frame-ancestors 'none'`、`connect-src *`。
产物全是外链脚本，所以这一条不会误伤；`connect-src *` 是因为这个 app 的设计就是
"填一个后端地址就能连"。接口（`/api/**`）**不带** CSP。

**CORS**：默认 `["*"]`（本机开发），但**明确 `allow_credentials=False`** ——
这个服务只用 Bearer 令牌、不用 cookie，"通配来源 + 带凭证"是唯一一种
"任意站点都能带着你的身份打这个服务"的配置。`cors_origins` 全开**且**绑在非回环地址时，
启动会打一条 WARNING（不拦启动）。

### 3.6 报表（P0-2）

```bash
curl -s "http://127.0.0.1:8080/api/v1/admin/metrics?window=24h&top=5" -H "Authorization: Bearer $TOKEN"
```

`window` 只认 `1h / 6h / 24h / 7d / 30d`（别的给 `400`；这份白名单同时由
`GET /api/v1/capabilities` 的 `metrics.windows` 报出，界面不写死）；`top` ∈ [1, `metrics.top_max`]；没接账本表给 `503`（**读不到 ≠ 是空的** ——
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

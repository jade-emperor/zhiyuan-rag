# project-rag 接口文档

> **schema 的权威是运行时的 `/openapi.json`**（FastAPI 自动生成，同时提供 `/docs` Swagger 与 `/redoc`）。
> 本文只写 schema 表达不了的东西：路由语义、WebSocket 帧协议、错误与降级、限额口径。
> 端点契约（哪个行为被哪条断言钉住）的权威是 `scripts/acceptance/check_m5_api.py`。

## 概述

| 项 | 值 |
|---|---|
| 服务地址 | `http://<host>:18080`（容器里的 app 是 `:8080`） |
| 技术栈 | FastAPI + WebSocket + Milvus / MySQL / Redis |
| 版本 | `0.1.0`（见 `/openapi.json` 的 `info.version`） |
| 鉴权 | `Authorization: Bearer <令牌>`；WebSocket 用 `?token=<令牌>` |
| 编码 | 请求与响应都是 JSON（UTF-8）；上传例外，见 §9.1 |

```bash
uv run project-rag-api        # 或 ./scripts/dev.sh serve；端口取 api.host / api.port
```

浏览器打开 `http://<host>:<port>/` 就是前端 app（`307` → `/static/app/`）。

**三个口，别混**：`18080` = 宿主上的代码后端（跑当前源码）；`8080` = 容器里的 app（页面与接口同一个进程）；
`5173` = Vite 开发服务器（只发页面）。前两个可同时跑。

## 目录

1. [通用约定](#1-通用约定)　2. [端点一览](#2-端点一览)　3. [健康检查](#3-健康检查)　4. [能力自述](#4-能力自述)
5. [认证](#5-认证)　6. [问答](#6-问答)　7. [会话](#7-会话)　8. [来源](#8-来源)　9. [文档治理](#9-文档治理)
10. [报表](#10-报表)　11. [账号 / 组 / 可见范围](#11-账号--组--可见范围)
12. [限流、CSP 与 CORS](#12-限流csp-与-cors)　13. [CLI](#13-cli)　14. [版本与兼容](#14-版本与兼容)

---

## 1. 通用约定

- **Base URL**：`http://<host>:<port>`；业务接口**一律在 `/api/v1` 下**（从 `/api/*` 迁完，没有留别名）。
  根上只有三个东西：`/health`、`/`（307 跳前端）、`/static`（静态资源，前端产物在 `/static/app/`）。
- **请求头**：业务接口一律带 `Authorization: Bearer <令牌>`（`POST /api/v1/auth/login` 获取）；
  有请求体的再带 `Content-Type: application/json`。**WebSocket 用 `?token=<令牌>`**
  （浏览器不能给 WS 设自定义头）。
- **无状态**：服务端不发 cookie。`session_id` 指一段对话，令牌证明你是谁 —— 两件事，都由调用方保存。
  会话**绑在账号上**：读别人的会话是 `403`。
- **角色三种（闭集）**：`employee`（问答 + 看清单）/ `ops`（问答 + 报表）/ `admin`（全部）。没有注册接口。

**错误码**：

| 码 | 什么时候 | 客户端怎么办 |
|---|---|---|
| `400` | 参数值不被接受（未知后缀、未知窗口、重名、名单里的对象不存在…） | 改参数 |
| `401` | 没带令牌 / 令牌无效（登录失败也是这个，且**话术统一**） | 重新登录 |
| `403` | 令牌有效，但这不是你的东西（别人的会话、要管理员的操作） | 换账号，重试无用 |
| `404` | 资源不存在；**看不见的资源也回 404**（不告诉"它存在"） | — |
| `413` | 上传超过 `capabilities.max_upload_mb` | 换小文件 |
| `422` | 请求体不合法（缺 `query`、空串、越界），pydantic 拦在用例之前 | 改请求（这是编程错误） |
| `429` | 触发限流（问答 / 上传 / 登录各一档，见 §12） | **按 `Retry-After` 等**，别立刻重试 |
| `500` | 用例内部抛错（库连不上、模型加载失败） | 读服务端日志，响应体不含堆栈 |
| `503` | 依赖没装配 / 读不到数据（**读不到 ≠ 是空的**） | 服务端配置或依赖故障，重试无用 |

- **答不出来也是 `200`**：拒答时返回一句人话 + `refused: true`，不是 `500`、不是 `null`。
- 一次请求一行延迟账（`logs/app.log`，31 字段），消耗报表与延迟报表读的是同一批行。

**影响调用方的配置**（优先级：环境变量 > `.env` > `config/config.yaml`；前缀 `RAG_`、嵌套用 `__`）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `api.host` / `api.port` | `0.0.0.0` / `18080` | 绑定地址。18080 是**代码后端**；容器里由 `RAG_API__PORT=8080` 覆盖成发布口 |
| `api.cors_origins` | `["*"]` | 本机全开；对外必须收紧成具体域名（见 §12） |
| `api.rate_limit.*` | `60/10/10`（次/分） | 三档限流，见 §12 |
| `conversation.history_rounds` | `5` | 会话历史的**轮数**（读与写共用） |
| `conversation.history_max_chars` | `12000` | 进提示词的**字符预算**：从最新往回装，装不下的整轮丢掉、至少留最新一轮 |
| `faq.threshold` | `0.95` | FAQ 命中阈值（另有 `min_query_coverage` / `min_entry_coverage` 两道覆盖率门槛） |
| `rag.rerank` | `true` | 是否重排；`rerank: true` 依赖它，没装重排器时给 `true` 会 `400` |
| `rag.retrieval_k` / `rag.candidate_m` | `5` / `2` | 每路召回条数 / 进上下文的父块数（`k` / `max_context` 的默认值） |
| `embedding.use_f16` / `rerank_use_f16` | `false` | 两个模型各自的半精度开关，只在 CUDA 上生效 |

---

## 2. 端点一览

29 条业务路由 + `/health` + 前端。**认证**列：`—` 不需要；`Bearer` 任意登录账号；`Bearer+归属` 还得是这段会话的主人；
`admin` 管理员；`运维/admin` 报表那两条（运维不是管理员，所以不走 `/admin/` 前缀）。

| 方法 | 路径 | 用途 | 认证 |
|---|---|---|---|
| `POST` | `/api/v1/query` | 非流式问答 | Bearer |
| `WS` | `/api/v1/stream` | 流式问答（`sources` / `token` / `done` 帧） | `?token=` |
| `POST` | `/api/v1/create_session` | 发新会话 ID（当场绑归属） | Bearer |
| `GET` | `/api/v1/sessions` | 我名下的会话目录 | Bearer |
| `GET` | `/api/v1/history/{session_id}` | 读最近 N 轮 | Bearer+归属 |
| `DELETE` | `/api/v1/history/{session_id}` | 清空一段会话 | Bearer+归属 |
| `GET` | `/api/v1/sources` | 语料里真实有哪些来源 | Bearer |
| `POST` | `/api/v1/auth/login` | 账号密码换令牌 | — |
| `GET` | `/api/v1/auth/me` | 当前账号 + 三个能力布尔值 | Bearer |
| `POST` | `/api/v1/auth/logout` | 撤销当前令牌（幂等） | Bearer（可缺） |
| `GET` | `/api/v1/auth/captcha` | 图形验证码（PNG data URI + 票据） | — |
| `POST` | `/api/v1/auth/register` | 自助注册（验证码 + 口令策略），直接回令牌 | — |
| `POST` | `/api/v1/auth/password` | 改密：撤销全部旧令牌并发新令牌 | Bearer |
| `GET` | `/api/v1/capabilities` | 后缀/上限/模型/检索选项（前端能力的唯一来源） | — |
| `GET` | `/api/v1/folders` | 一级目录清单（含"未分类"与汇总） | Bearer |
| `POST` | `/api/v1/folders` | 建目录（`201`） | admin |
| `GET` | `/api/v1/folders/{folder_id}` | 一个目录 + 它里面的文档 | Bearer |
| `PATCH` | `/api/v1/folders/{folder_id}` | 改名 / 改说明（**不改 ID**） | admin |
| `DELETE` | `/api/v1/folders/{folder_id}` | 删**空**目录（非空 `409`） | admin |
| `POST` | `/api/v1/documents?filename=…&folder=…` | 上传文档（原始 body，`202`） | admin |
| `GET` | `/api/v1/documents?folder=…` | 知识库清单（登记表×磁盘×向量库三处对账） | Bearer |
| `PATCH` | `/api/v1/documents/{document_id}` | 把一份文档挪到另一个目录 | admin |
| `DELETE` | `/api/v1/documents/{document_id}` | 删文档（登记行 + 向量 + 上传的原件） | admin |
| `GET` | `/api/v1/documents/{document_id}/chunks` | 预览入库后的父块 | Bearer |
| `GET` | `/api/v1/reports/metrics` | 延迟报表 | 运维/admin |
| `GET` | `/api/v1/reports/cost` | 消耗报表 | 运维/admin |
| `GET` | `/api/v1/admin/users` | 账号列表 | admin |
| `POST` | `/api/v1/admin/users` | 建号（`201`） | admin |
| `DELETE` | `/api/v1/admin/users/{user_id}` | 删号 | admin |
| `POST` | `/api/v1/admin/users/{user_id}/password` | 改密码 | admin |
| `GET` | `/api/v1/admin/groups` | 组与成员 | admin |
| `POST` | `/api/v1/admin/groups` | 建组（`201`） | admin |
| `DELETE` | `/api/v1/admin/groups/{group_id}` | 删组 | admin |
| `PUT` | `/api/v1/admin/groups/{group_id}/members` | 整份设置成员 | admin |
| `GET` | `/api/v1/admin/groups/{group_id}/documents` | 这个组能看哪些文档 | admin |
| `PUT` | `/api/v1/admin/groups/{group_id}/documents` | 整份设置这个组能看的文档 | admin |
| `GET` | `/api/v1/admin/documents/{document_id}/acl` | 一份文档的可见范围 | admin |
| `PUT` | `/api/v1/admin/documents/{document_id}/acl` | 整份设置可见范围 | admin |
| `GET` | `/health` | 进程探活 | — |
| `GET` | `/` | `307` → `/static/app/` | — |

---

## 3. 健康检查

**请求**

```
GET /health
```

**响应**

```json
{ "status": "ok" }
```

只回答"进程在不在"，**刻意不探容器**：Milvus / MySQL / Redis 的连通性由运维侧探针负责。

---

## 4. 能力自述

**前端唯一的能力来源**：上传框的 `accept`、参数面板的下拉项与上限、模型下拉全从这来，别在前端写死。
**不需要登录**（不含任何数据），登录页也要用它 —— 后端地址填错时第一个报错应该发生在这里。

**请求**

```
GET /api/v1/capabilities
```

**响应**

```json
{
  "suffixes": [".docx", ".md", ".pdf", ".pptx", ".txt"],
  "max_upload_mb": 50,
  "models": [{ "id": "deepseek-flash", "label": "deepseek-flash", "default": true }],
  "model_settings": { "temperature": 0.7, "thinking": "off" },
  "retrieval": {
    "modes": ["vector", "keyword", "hybrid"],
    "fusions": ["rrf", "weighted"],
    "defaults": { "mode": "hybrid", "fusion": "rrf", "k": 5, "max_context": 2,
                  "bm25_weight": 0.5, "vector_weight": 0.5, "rrf_k": 60, "rerank": true },
    "limits": { "k_max": 50, "max_context_max": 10, "rrf_k_max": 100 },
    "rerank_available": true
  },
  "metrics": { "windows": ["1h", "6h", "24h", "7d", "30d"], "default_window": "24h",
               "top_max": 50, "min_n": { "p50": 1, "p95": 20, "p99": 100 } },
  "roles": ["employee", "ops", "admin"],
  "registration": { "enabled": true, "captcha_required": true,
                    "invite_required": false, "min_password_chars": 10 }
}
```

**字段说明**

| 字段 | 说明 |
|---|---|
| `retrieval.defaults` | 配置里**生效的那一份**（不是代码默认值） |
| `retrieval.limits` | `/query` 的取值范围；前端照它夹取，就不会夹到后端拒绝的值 |
| `metrics.windows` | 报表的窗口白名单；`min_n` 是分位数的最低样本量 |
| `model_settings` | **只读展示**：`temperature` / `thinking` 是启动期配置，不支持按请求覆盖 |

---

## 5. 认证

### 5.1 登录

**请求**

```
POST /api/v1/auth/login
```

**请求体**

```json
{ "username": "admin", "password": "你的口令" }
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | ✅ | 账号 |
| `password` | string | ✅ | 口令 |

**响应**

```json
{ "token": "…", "username": "admin", "role": "admin", "expires_in": 43200 }
```

| 字段 | 说明 |
|---|---|
| `token` | 后续请求放进 `Authorization: Bearer <token>`；库里存的是它的 sha256 |
| `expires_in` | 有效期（秒），默认 **12 小时** |

登录失败一律 `401` + 同一句话（用户不存在与口令错误不分开说 —— 那是用户名枚举）。

### 5.2 当前账号

**请求**

```
GET /api/v1/auth/me
```

**响应**

```json
{ "username": "admin", "role": "admin", "is_admin": true,
  "can_view_reports": true, "can_use_knowledge": true, "can_manage": true }
```

前端按**能力布尔值**显示页签，**不比较角色字符串** —— 后端加第四个角色时前端不用改。
但权限在后端：每条路由各自 `require_*`，藏起来 ≠ 安全。

### 5.3 登出

**请求**

```
POST /api/v1/auth/logout
```

吊销当前令牌（不只是前端删本地副本），幂等；没带令牌也给 `200`（`revoked: false`）。

**响应**

```json
{ "revoked": true }
```

| 场景 | 行为 |
|---|---|
| 没带令牌 / 无认证模式 | `503`（`/api/v1/auth/*` 都是） |
| WebSocket 认证失败 | 先回一帧 `error` 说明原因，再 `close(1008)` |

### 5.4 图形验证码

**请求**

```
GET /api/v1/auth/captcha
```

按 IP 限流（与登录同档，`api.rate_limit.login`）。`api.captcha.enabled=false` → `404`。

**响应**

```json
{ "ticket": "J0d…", "image": "data:image/png;base64,iVBORw0…", "expires_in": 120 }
```

| 字段 | 说明 |
|---|---|
| `ticket` | 不透明票据，随注册请求回传；**一次一票**，校验失败也作废 |
| `image` | PNG data URI，直接给 `<img src>`；答案不在响应里 |
| `expires_in` | 有效期（秒），`api.captcha.ttl_s` |

### 5.5 自助注册

**请求**

```
POST /api/v1/auth/register
```

```json
{ "username": "alice", "password": "Str0ngPass!",
  "captcha_ticket": "J0d…", "captcha_answer": "K7MQ", "invite_code": "" }
```

| 字段 | 说明 |
|---|---|
| `username` | 1..64 字符，不能含空格；重名报 `400` |
| `password` | 需满足 `api.registration.min_password_chars`，且含字母与数字、不含登录名、不在常见弱口令表里 |
| `captcha_ticket` / `captcha_answer` | 上一步那张图；大小写不敏感 |
| `invite_code` | 仅当 `api.registration.invite_code` 非空时必填 |

**响应**（`201`，与登录同形）

```json
{ "token": "…", "username": "alice", "role": "employee", "expires_in": 43200 }
```

| 场景 | 状态码 |
|---|---|
| 成功 | `201` + 令牌（角色固定 `employee`，请求体里没有 `role`） |
| 验证码过期 / 已用 / 不对 | `400` |
| 口令不合规 / 重名 | `400`（`detail` 逐条点名缺什么） |
| 邀请码不对 | `403` |
| 没开自助注册 | `403` |
| 同一 IP 超限 | `429` |

### 5.6 改密

**请求**

```
POST /api/v1/auth/password
```

```json
{ "old_password": "Str0ngPass!", "new_password": "Fresh0Pass!" }
```

**响应**

```json
{ "token": "…", "username": "alice", "role": "employee", "expires_in": 43200 }
```

成功后**该用户的全部旧令牌立刻作废**（其它设备下次请求 `401`），响应里的新令牌给当前设备继续用。
原密码不对 → `401`；新密码不合规或与原密码相同 → `400`。

---

## 6. 问答

### 6.1 非流式问答

**请求**

```
POST /api/v1/query
```

**请求体**

```json
{
  "query": "装饰器有什么用？",
  "session_id": "b7f0…",
  "source_filter": "bagu_ai",
  "k": 5,
  "mode": "hybrid",
  "fusion": "rrf",
  "bm25_weight": 0.5,
  "vector_weight": 0.5,
  "rerank": true,
  "max_context": 2,
  "rrf_k": 60
}
```

只填 `query` 就能跑；其余都是可选的覆盖值。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | ✅ | 用户问题，长度 ≥ 1 |
| `session_id` | string \| null | | 给了就带对话历史（读最近 N 轮进提示词，并记下这一轮） |
| `source_filter` | string \| null | | 按来源过滤，取值来自 `/api/v1/sources` |
| `k` | int \| null | | 每路召回条数，1..`limits.k_max`；不给取 `defaults.k` |
| `mode` | string \| null | | `vector` / `keyword` / `hybrid`；不给 = 配置里的两路权重 |
| `fusion` | string \| null | | `rrf`（只看排名）/ `weighted`（min-max 归一化后加权） |
| `bm25_weight` | float \| null | | 关键字那一路权重，`0` = **关掉这一路**（连检索都不做） |
| `vector_weight` | float \| null | | 向量那一路权重，同上 |
| `rerank` | bool \| null | | 不给取 `defaults.rerank`；**装了重排器才能给 `true`**，否则 `400`（不静默降级） |
| `max_context` | int \| null | | 最多几条父块进上下文 |
| `rrf_k` | int \| null | | RRF 平滑常数 |

**两种组合 `422`**（都是"两个真值来源必然打架"）：`mode` 与两个权重**不能同时给**；两个权重**不能同时为 0**。

**响应**

```json
{
  "answer": "装饰器是给函数加行为的语法……[1]",
  "channel": "rag",
  "refused": false,
  "truncated": false,
  "citations": [
    { "index": 1, "source": "bagu_ai", "doc_id": "JBL常见AI面试题-潜艇.md",
      "preview": "……父块正文的前 120 字……", "score": 0.83, "rerank_score": 4.12, "page": 37,
      "images": [
        { "key": "rag/images/ab/ab12…e6.png",
          "url": "https://bucket.oss-cn-guangzhou.aliyuncs.com/rag/images/ab/…?Expires=…&Signature=…" }
      ] }
  ],
  "faq_score": null,
  "session_id": null
}
```

| 字段 | 说明 |
|---|---|
| `answer` | 永远是非空字符串（拒答话术也在内） |
| `channel` | `faq` 标准答案直答 / `rag` 知识库 / `greeting` 寒暄拦截 / `throttled` 被限流（见 §12） |
| `refused` | 检索为空 → 直接拒答、**不调模型**，此时 `citations` 为空 |
| `truncated` | 撞到 `max_tokens`：**半截答案照原样返回**，只用这个字段提示 |
| `citations[].preview` | 父块正文前 120 字 —— 与模型真正读到的那段一致 |
| `citations[].score` | 融合分；`rerank_score` 为 `null` = 没重排或只有 1 条候选。**单路检索时 `score` 是那一路的原始分**，与融合分不同量纲，别互相比 |
| `citations[].page` | 源文件页码，**从 1 开始**；`null` = 不知道（md/txt 没有页码，或老块），**别拿 0 或 1 顶** |
| `citations[].images` | 这一条引用所属父块里的图：`key` 是稳定身份（图片字节 SHA-256），`url` 是**本次响应现签**的临时 URL（默认 300 秒）。`url` 为空 = 这次签不出来/图片功能没开；**不要把 `url` 存回库或历史** |
| `faq_score` | 仅 FAQ 命中时有值：`top1 原始分 ÷ 该条问自己的分`，夹到 0~1（1.000 = 近乎一字不差） |

**路由规则**（HTTP 与 WS 共用一份）：

```text
整句只是寒暄 → 回话术（不查 FAQ、不检索、不写历史）
FAQ 命中（≥ faq.threshold 且过两个覆盖率门槛）→ 标准答案直答 → 记历史
否则 → 知识库检索 → 有命中：检索 + 生成 + [n] 引用；无命中：拒答话术（不调模型）
```

### 6.2 流式问答（WebSocket）

#### 连接

```
WS /api/v1/stream?token=<令牌>
```

连上后客户端**逐条发 JSON**（形状同 `QueryRequest`），服务端逐帧回 JSON。
**同一条连接可以连续问多轮**（FAQ 命中、寒暄、请求体畸形都不会关连接）。

#### 发送消息

```json
{ "query": "装饰器有什么用？", "session_id": "b7f0…" }
```

#### 接收消息

| 帧 | 何时 | 字段 |
|---|---|---|
| `sources` | 知识库通道检索完成（0 或 1 帧，**在 token 之前**） | `citations: CitationOut[]` |
| `token` | 每一小段答案 | `text: string` |
| `done` | 恰好 1 帧收尾 | `answer` / `channel` / `refused` / `truncated` / `citations` / `session_id` |
| `error` | 请求体不是合法 JSON 或校验失败 | `detail: string`（连接留着，可重发） |

**帧示例**

```json
{ "type": "sources", "citations": [ { "index": 1, "source": "bagu_ai", "page": 37, "images": [ { "key": "…", "url": "…" } ] } ] }
{ "type": "token", "text": "装饰器是" }
{ "type": "done", "answer": "装饰器是……", "channel": "rag", "refused": false,
  "truncated": false, "citations": [ … ], "session_id": "b7f0…" }
```

**恒等式**：`done.answer == "".join(所有 token 帧的 text)`。答案**一律走 token 帧**（标准答案、拒答话术都在内），
前端只要"打印 token"，不用为"这句话从哪来"写分支。**非流式就是流式的收拢器**：两条入口的答案逐字相同。

```javascript
const ws = new WebSocket(`ws://${host}/api/v1/stream?token=${token}`);
ws.onopen = () => ws.send(JSON.stringify({ query: "装饰器有什么用？" }));
ws.onmessage = (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "sources") renderSources(frame.citations);
  else if (frame.type === "token") appendText(frame.text);
  else if (frame.type === "done") finish(frame);
  else if (frame.type === "error") showError(frame.detail);
};
```

---

## 7. 会话

### 7.1 建会话

**请求**

```
POST /api/v1/create_session
```

**响应**

```json
{ "session_id": "b7f0…" }
```

`uuid4`，**当场把归属绑到你账号上**：光有 ID 也读不到别人的会话（`403`）。

### 7.2 我名下的会话目录

**请求**

```
GET /api/v1/sessions?limit=50
```

| 查询参数 | 范围 | 默认 |
|---|---|---|
| `limit` | 1..200 | 50 |

**响应**

```json
{ "sessions": [
    { "session_id": "b7f0…", "title": "每周加班最多多长时间", "rounds": 3,
      "created_at": "<ISO 时间>", "updated_at": "<ISO 时间>" }] }
```

**只列有轮次的会话**（刚建没问过的不占地方），按最近活动倒序；标题 = 首问裁到 40 字。
无认证模式给空表；认证开着却没装配会话目录 → `503`（不拿空表冒充"你还没有会话"）。

### 7.3 读历史

**请求**

```
GET /api/v1/history/{session_id}
```

**响应**

```json
{ "session_id": "b7f0…",
  "rounds": [ { "question": "…", "answer": "…", "citations": [ … ] } ] }
```

最近 N 轮（`conversation.history_rounds`，默认 5），按时间正序，**与喂给模型的是同一份**。
每轮的 `citations[]` 是**当轮引用卡片的快照**，形状与 `/query` 出参里那份逐字相同；
更早的轮次给空列表（那一轮的出处当时没留下 —— 不编）。

### 7.4 清空会话

**请求**

```
DELETE /api/v1/history/{session_id}
```

**响应**

```json
{ "session_id": "b7f0…", "deleted": 3 }
```

---

## 8. 来源

**请求**

```
GET /api/v1/sources
```

**响应**

```json
{ "sources": ["bagu_ai", "bagu_java"] }
```

读向量库里的**真实**来源（不是配置白名单），用作 `source_filter` 的取值范围。

---

## 9. 文档治理

知识库是**两层**：一级是**目录**（`folder`），目录里才是文档。目录只是组织方式，**不承载可见范围**——
授权仍然按 `document_id` 逐份收窄（§11.3）。**未分类**（`folder` 为空串）是一个合法归属，
不是错误状态：磁盘上有、登记表没有的文件只能是它。目录的读写端点见 §9.5–§9.8。

**目录从哪来**：`data/corpus/` 下的每个一级子目录对应一格（进程启动时同步，幂等；
目录名去掉 `_data` 后缀），`corpus` 自己是"所有格的父亲"、不是一格。`project-rag ingest`
默认按文件在语料根下的一级目录落格，语料之外的路径落未分类；管理员也可以显式建目录
（§9.7），那一格不要求磁盘上有同名目录 —— 它是上传时的落点。

### 9.1 上传

**请求**

```
POST /api/v1/documents?filename=lecture01.pdf&folder=f_523f8bb13dc6
Content-Type: application/octet-stream
```

请求体是**文件原始字节**（不是 multipart）。响应 `202` + `DocumentOut`（见 §9.2 的字段表，此时 `status: "pending"`）。

| 情况 | 行为 |
|---|---|
| 后缀不在 `capabilities.suffixes` | `400` |
| 超过 `max_upload_mb` | `413`（**边收边写**，不会先读进内存再拒） |
| 文件名带路径 | basename 化（`../../逃逸.md` → `逃逸.md`） |
| `folder` | 目标目录 ID；省略或 `unfiled` = 未分类。**目录不存在 `404`（不自动新建，且不会先落盘）** |
| 同名文件已在**另一个目录** | `409` —— 文档 ID 就是文件名，两格不能各有一份（同一目录重传是覆盖） |
| 入库 | **异步**：立刻 `202`，后台切块嵌入；状态流转 `pending → processing → processed`，失败进 `failed`（带 `error`）；**只有 `processed` 可检索** |

### 9.2 知识库清单

**请求**

```
GET /api/v1/documents?folder=f_523f8bb13dc6
```

清单是**实时视图**：后端每次把"登记表 / 磁盘 / 向量库"三处摆到一起再出参。
`folder` 省略 = 全部；给目录 ID 或 `unfiled` = 只看那一格（二级页面的数据来源之一）。

**响应**

```json
{
  "documents": [
    { "document_id": "lecture01.pdf", "filename": "lecture01.pdf", "status": "processed", "state": "ok",
      "chunks": 42, "indexed_chunks": 42, "size_bytes": 20480, "disk_size_bytes": 20480,
      "on_disk": true, "searchable": true, "restricted": false, "folder": "f_523f8bb13dc6",
      "created_at": "…", "error": null },
    { "document_id": "handbook.docx", "filename": "handbook.docx", "status": "unregistered", "state": "unregistered",
      "chunks": 0, "indexed_chunks": 0, "size_bytes": 0, "disk_size_bytes": 38426,
      "on_disk": true, "searchable": false, "restricted": false, "folder": "",
      "created_at": "", "error": null }
  ],
  "summary": { "total": 2, "ok": 1, "changed": 0, "not_indexed": 0,
               "file_missing": 0, "unregistered": 1, "in_flight": 0 },
  "uploads_dir": "…/data/uploads", "corpus_dir": "…/data/corpus"
}
```

| 字段 | 说明 |
|---|---|
| `state` | `ok` 三处一致 / `changed` 磁盘文件变了 / `not_indexed` 登记说已入库但库里没有 / `file_missing` 磁盘找不到 / `unregistered` 磁盘上有、登记表没有（**只读**）。非 `processed` 的行 `state` 原样等于 `status` |
| `chunks` vs `indexed_chunks` | 登记表记的 vs 库里实际的，**两个数，不合成一个** |
| `searchable` | 按**库内实际块数**算 —— 登记说 `processed` 不算数 |
| `restricted` | 这份文档被收窄过（规则只用来收窄，见 §11.3）；**没授权给你的不出现** |
| `folder` | 归在哪一级目录下；空串 = 未分类（**未登记的文件必然是空串**：读接口不写库） |

### 9.3 删除

**请求**

```
DELETE /api/v1/documents/{document_id}
```

**响应**

```json
{ "document_id": "lecture01.pdf", "deleted_chunks": 42, "removed_file": true,
  "removed_record": true, "kept_source": false }
```

**幂等**：文档不存在也 `200`，只是几个字段都是 0/false。
删除顺序「标记 `deleting` → 删向量 → 删原文件 → 删登记行」——**向量删失败就不动登记行**。
`removed_file`（真删了原件）与 `kept_source`（`data/corpus/**` 是源材料，故意没动）是两件事。

### 9.4 预览

**请求**

```
GET /api/v1/documents/{document_id}/chunks?limit=20&chars=400
```

| 查询参数 | 范围 | 默认 |
|---|---|---|
| `limit` | 1..100 | 20 |
| `chars` | 50..2000 | 400 |

**响应**

```json
{ "document_id": "handbook.pdf", "parent_blocks": 15, "child_chunks": 42, "returned": 15,
  "truncated": false,
  "blocks": [ { "index": 1, "page": 7, "last_page": 8, "chars": 1180, "text": "……前 400 字……" } ] }
```

- **显示的是父块**（按 `parent_id` 折叠）：那才是模型真正读到的东西。`child_chunks` 与 `parent_blocks` 差着切分比例，两个都给。
- `chars` 是**截断前**的字数；`page` / `last_page` 不同 = 这一段跨页；都是 `null` = 不知道（md/txt）。
- 没登记的文档 `404`，话术会点名 `project-rag doctor` / `--prune-orphans`（库里的孤儿块照样能被检索到，
  但"这份文档"在管理页上不存在）；**看不见的文档也回同一句 404**。
- 顺序只保证稳定（按页码升序，没页码的在后），不保证与原文严格一致。


### 9.5 目录清单

**请求**

```
GET /api/v1/folders
```

一级页面用的就是这一条。每格的读数与清单**同一次对账**（份数 / 库内块数 / 占用 / 需要处理），
所以"目录卡上写 2 份"与"点进去两行"不会在两次点击之间对不上。

**响应**

```json
{
  "folders": [
    { "folder_id": "f_523f8bb13dc6", "name": "公司制度", "description": "员工手册与政策",
      "created_at": "…", "updated_at": "…", "documents": 2, "chunks": 8, "size_bytes": 75899,
      "issues": 0, "restricted": 1, "last_activity": "…", "unfiled": false }
  ],
  "unfiled": { "folder_id": "unfiled", "name": "未分类", "documents": 1, "chunks": 0, "…": "…",
               "unfiled": true },
  "summary": { "folders": 2, "used": 1, "unfiled": 1 }
}
```

| 字段 | 说明 |
|---|---|
| `unfiled` | **恒定给出**（哪怕 0 份）。`folder_id` 是保留值 `unfiled`，库里存的是空串 |
| `items.issues` | 需要处理的份数（三处不一致 + 在途/失败）—— **不是"错误数"** |
| `items.chunks` | **库内实际块数**（不是登记表说多少） |
| `items.last_activity` | 目录里最新一份文档的登记时间；空串 = 还没有文档（目录自身的 `updated_at` 是改名时间） |
| `items.restricted` | 里面有几份是收窄过的 |
| `summary` | `folders` 真实目录数 / `used` 其中有文档的 / `unfiled` 未分类份数 |

计数**只数看得见的**：看不见的文档不出现在 `documents` 里，也不计入份数。

### 9.6 目录详情

```
GET /api/v1/folders/{folder_id}
```

`folder_id` 可以是真实目录 ID，也可以是 `unfiled`。一次往返给齐二级页面要的三样东西：

```json
{ "folder": { "…": "同 §9.5 的一格" },
  "documents": [ { "…": "同 §9.2 的每一行" } ],
  "summary": { "total": 2, "ok": 1, "changed": 1, "not_indexed": 0,
               "file_missing": 0, "unregistered": 0, "in_flight": 0 } }
```

目录不存在 `404`；`detail` 会指一条能走的路（先看 `GET /api/v1/folders`）。

### 9.7 建目录 / 改名 / 删目录（admin）

```
POST   /api/v1/folders                {"name": "公司制度", "description": "员工手册与政策"}
PATCH  /api/v1/folders/{folder_id}    {"name": "…", "description": "…"}   # 两个字段都可选
DELETE /api/v1/folders/{folder_id}
```

| 情况 | 行为 |
|---|---|
| 名字为空 / 超过 40 字 / 含 `/` `\` 或控制字符 / 叫"未分类" | `400` |
| 名字已被占用（**名字唯一**，`--folder` 靠它解析） | `409` |
| 改名 | **ID 不变**（改 ID 等于把目录里的文档全脱钩）；`description` 给空串 = 清空 |
| 删非空目录 | `409`，`detail` 点出份数 —— **删目录不删文档** |
| 删不存在的目录 | `200` + `{"folder_id": "…", "deleted": 0}`（幂等） |
| 对 `unfiled` 做改名 / 删除 | `400`（它是虚拟目录） |

响应是 `FolderOut`（同 §9.5 的一格，含当前读数），删除是 `{"folder_id", "deleted"}`。

### 9.8 移动文档（admin）

```
PATCH /api/v1/documents/{document_id}   {"folder_id": "f_523f8bb13dc6"}
```

只改归属：**不重切、不重灌、块 id 一个都不变**，所以是毫秒级的，历史引用也不受影响。
`folder_id` 给空串 = 挪回未分类。目标目录不存在、或这份文档没有登记行（`unregistered`）都是 `404`。
响应 `{"document_id": "…", "folder": "…"}`。

---

## 10. 报表

两个报表**同权限、同窗口白名单、读同一批账本行**。`window` ∈ `1h / 6h / 24h / 7d / 30d`（别的 `400`）。

### 10.1 延迟报表

**请求**

```
GET /api/v1/reports/metrics?window=24h&top=5
```

| 查询参数 | 范围 | 默认 |
|---|---|---|
| `window` | 上面那五个 | `24h` |
| `top` | 1..`metrics.top_max` | 5 |

**响应**

```json
{
  "window": "24h", "rows": 37, "warm_rows": 34, "cold_rows": 3,
  "warm": { "rows": 34, "refused": 1, "truncated": 0, "aborted": 0, "faq_hits": 21, "rag_answers": 12 },
  "cold": { "rows": 3,  "refused": 0, "truncated": 0, "aborted": 0, "faq_hits": 1,  "rag_answers": 2 },
  "segments": [ { "name": "total_ms", "n": 34, "p50": 1991.3, "p95": null, "p99": null, "maximum": 5369.1 } ],
  "slowest": [ { "request_id": "5b7ad6acb0da", "channel": "rag", "cold": true, "total_ms": 5369.1 } ]
}
```

| 字段 | 说明 |
|---|---|
| `rows` | 这一窗收了**几次请求**（不是几条日志、不是两条 span） |
| `warm` / `cold` | 冷热**各一整套**计数，**故意没有合计栏**：两层分母不同，比率不可加 |
| `segments[]` | 分位数**只用热行**算（冷行仍进 `rows` 与最慢榜）；`null` = 样本量不够（`p95` 要 n≥20、`p99` 要 n≥100），**不折算成 0** |
| `segments[].name` | `total_ms` / `ttft_ms` / `retrieve_ms` / `llm_*` 等；`vector` 与 `bm25` 是并行两路，各自报时长，加起来不要求等于 `retrieve_ms` |
| `slowest[]` | **含冷启动**并标 `cold`（那是用户真实等到的） |

分位数口径：最近秩、不插值。没接账本表 → `503`。

### 10.2 消耗报表

**请求**

```
GET /api/v1/reports/cost?window=24h
```

**响应**

```json
{
  "window": "24h", "currency": "CNY",
  "metered_calls": 24, "unmetered_calls": 0,
  "period_unknown_calls": 0, "zone_unknown_calls": 0, "calendar_unknown_calls": 0, "cache_unknown_calls": 0,
  "input_tokens": 29412, "cached_input_tokens": 17638, "output_tokens": 16848,
  "total_tokens": 46260, "total_cost": 0.071582, "all_priced": true, "unpriced_models": [],
  "periods": [ { "period": "peak", "calls": 24, "input_tokens": 29412, "output_tokens": 16848, "total_cost": 0.071582 } ],
  "lines": [ { "model": "deepseek-flash", "period": "peak", "calls": 24, "input_tokens": 29412,
               "cached_input_tokens": 17638, "output_tokens": 16848, "total_cost": 0.071582 } ],
  "peak_weekdays": [1, 2, 3, 4, 5], "peak_windows": [["09:00", "12:00"], ["14:00", "18:00"]],
  "holiday_years": [2025, 2026]
}
```

**计价是三档 × 两时段**（元 / 每百万 token，数字来自 `config/config.yaml` 的 `cost.models`）：

| 档 | 空闲 | 高峰 |
|---|---|---|
| 输入 · 命中 prompt cache | 0.02 | 0.04 |
| 输入 · 未命中 | 1 | 2 |
| 输出 | 4 | 8 |

**高峰 = 北京时间工作日 ∩ 钟点窗口（09:00–12:00 / 14:00–18:00，左闭右开）∩ 不是法定节假日**，其余半价。
`peak_weekdays` / `peak_windows` / `holiday_years` 是口径本身（来自配置），界面拿它解释"这行为什么算空闲"。

**六个"不知道"的计数是故意的** —— 报表宁可说不知道，也不画成 0：

| 字段 | 含义 | 怎么计价 |
|---|---|---|
| `unmetered_calls` | 调了模型但没量到 token | **不算进金额** |
| `period_unknown_calls` | 连时间戳都没有 | 按**高峰**价 |
| `zone_unknown_calls` | 时间戳没带时区（价目表按北京时间分段） | 按**高峰**价 |
| `calendar_unknown_calls` | 那一年的节假日还没填进配置 | 按**高峰**价 |
| `cache_unknown_calls` | 没回报缓存命中数 | 整段输入按**未命中**价（上界） |
| `unpriced_models` + `all_priced=false` | 模型没配单价 | 金额 `null`，**不是 0 元** |

不花钱的路径（FAQ 直答、检索为空拒答、寒暄）不出现在消耗报表里。

---

## 11. 账号 / 组 / 可见范围

### 11.1 账号（admin）

**请求**

```
GET    /api/v1/admin/users
POST   /api/v1/admin/users
POST   /api/v1/admin/users/{user_id}/password
DELETE /api/v1/admin/users/{user_id}
```

| 请求 | 请求体 | 响应 |
|---|---|---|
| `GET /admin/users` | — | `{"users": [{user_id, username, role, is_admin, group_ids[]}]}`（**不含密码**） |
| `POST /admin/users` | `{"username":"zhangsan","password":"口令至少 8 位","role":"employee"}` | `201` + `{user_id, username, role}` |
| `POST /admin/users/{user_id}/password` | `{"password":"新口令"}` | `UserOut` |
| `DELETE /admin/users/{user_id}` | — | `{user_id, username, deleted: true}` |

**请求体**

```json
{ "username": "zhangsan", "password": "口令至少 8 位", "role": "employee" }
```

`role` ∈ `employee` / `ops` / `admin`。删号连带删令牌、组员行与授权行；
**不能删自己、不能删最后一个管理员**（`400`）。不存在的账号 `404`。

### 11.2 组（admin）

**请求**

```
GET    /api/v1/admin/groups
POST   /api/v1/admin/groups
PUT    /api/v1/admin/groups/{group_id}/members
DELETE /api/v1/admin/groups/{group_id}
GET    /api/v1/admin/groups/{group_id}/documents
PUT    /api/v1/admin/groups/{group_id}/documents
```

| 请求 | 请求体 | 响应 |
|---|---|---|
| `GET /admin/groups` | — | `{"groups": [{group_id, name, member_ids[]}]}` |
| `POST /admin/groups` | `{"name": "销售"}` | `201` + `{group_id, name, member_ids[]}`（重名 `400`） |
| `PUT /admin/groups/{group_id}/members` | `{"user_ids": ["U-1","U-2"]}` | `GroupOut`（**整份**替换，声明式） |
| `DELETE /admin/groups/{group_id}` | — | `{group_id, deleted: true}`（连带清掉授权给它的规则） |
| `GET /admin/groups/{group_id}/documents` | — | `GroupDocumentsOut` |
| `PUT /admin/groups/{group_id}/documents` | `{"allowed": ["内部报价单.md"]}` | `GroupDocumentsOut` + `granted` / `revoked` / `opened` + 一句 `notice` |

**请求体**

```json
{ "allowed": ["内部报价单.md"] }
```

**响应**

```json
{ "group_id": "G-1", "name": "销售",
  "documents": [ { "document_id": "内部报价单.md", "filename": "内部报价单.md",
                   "visibility": "open", "other_subjects": 0 } ],
  "granted": [], "revoked": [], "opened": [], "notice": "…" }
```

`visibility` 三态：`open` 没有规则 = 人人可见（**本组禁不掉**，要限制得去收窄这份文档）/ `allowed` 有规则且本组在名单里 / `denied` 有规则但本组不在。
`other_subjects` = 除本组外还授权给了几个对象 —— 取消本组后**名单空了 = 回到人人可见**，那是一次大得多的变化。

### 11.3 文档可见范围（admin）

**请求**

```
GET /api/v1/admin/documents/{document_id}/acl
PUT /api/v1/admin/documents/{document_id}/acl
```

**请求体**

```json
{ "user_ids": ["U-1"], "group_ids": ["G-1"] }
```

```json
{ "document_id": "内部报价单.md", "restricted": true, "user_ids": ["U-1"], "group_ids": ["G-1"] }
```

| 规则 | |
|---|---|
| 默认 | **没配规则 = 所有登录账号都能看**；规则**只用来收窄** |
| 并集 | 授权对象是人（`user_ids`）或组（`group_ids`），任一命中即可见 |
| 取消限制 | 两个名单都空 = `restricted: false` = 回到人人可见 |
| 管理员 | **永远可见**（他是唯一能改名单的人，被自己的规则挡住就再也改不回来） |
| 生效 | 检索（不进候选）、清单（不显示）、预览（`404`）三处同时生效；下一条请求就生效，不用重启 / 重灌 |
| 看不见 = 不存在 | 受限文档的预览回**与"文档不存在"逐字相同**的 404 —— 403 会把"这份文档存在"说出来，而名字往往就是内容 |
| 读不到规则 | `503`，**不放行**（一次数据库抖动不许变成一次全网可见） |

---

## 12. 限流、CSP 与 CORS

**滑动窗口**计数，默认在 **Redis**，每档**按身份**计数（登录前按来源 IP，登录后按账号）。

| 档 | 默认 | 打在哪 |
|---|---|---|
| `query` | 60 次 / 分钟 | `POST /api/v1/query` **与** WS 的每条消息（**两条入口一份额度**） |
| `upload` | 10 次 / 分钟 | `POST /api/v1/documents` |
| `login` | 10 次 / 分钟 | `POST /api/v1/auth/login`（登录成功清掉本 IP 计数） |

**超限回什么，按路径分三种**：

| 路径 | 超限回什么 |
|---|---|
| `POST /api/v1/query` | **`200` + 兜底话术**：`channel: "throttled"`、`answer` 带等待秒数与上限、`citations: []`；响应头带 `Retry-After` |
| `WS /api/v1/stream` | 同一形制：`token` + `done`（`channel: "throttled"`），**连接不断** |
| `login` / `upload` | **`429` + `Retry-After`**（登录给兜底话术会看着像登录成功） |

被限流的那一轮**不写历史、不进账本**，只留一条 WARNING。
限流键是 socket 对端地址，**不认 `X-Forwarded-For`**（没有可信反代时那个头谁都能伪造）。
Redis 故障 → **降级放行**（fail-open）+ WARNING：限流是保护不是正确性，不能为一道闸把问答停掉。

**CSP**：`/static/app/**` 带 `script-src 'self'`、`object-src 'none'`、`frame-ancestors 'none'`、`connect-src *`；接口不带 CSP。
**CORS**：默认 `["*"]`，但 `allow_credentials=False`（只用 Bearer 令牌，不用 cookie）。
`cors_origins` 全开**且**绑在非回环地址时，启动打一条 WARNING。

配置在 `api.rate_limit.*`：`enabled` / `backend`(`redis`|`memory`) / `key_prefix` / `window_s` / 三档上限（**`<= 0` = 关掉那一档**）。

---

## 13. CLI

13 个子命令，与 HTTP 共用同一套用例与组合根。`uv run project-rag <子命令> --help` 看参数。

| 子命令 | 必填 | 主要可选 | 行为 |
|---|---|---|---|
| `chat` | `-p/--prompt` | — | 裸问模型，不检索（**唯一不依赖任何中间件**的命令） |
| `faq` | `-q/--question` | — | 只走 FAQ 通道：命中直答，未命中模型兜底（**不写账本**） |
| `rag` | `-q/--question` | `--k` `--source` | 只走知识库：检索 + 生成 + `[1][2]` 引用 |
| `qa` | `-q/--question` | `--k` `--source` `--session` `--stream` | **合流**：FAQ 优先、知识库兜底（日常入口） |
| `rewrite` | `-q/--question` | — | 查询改写诊断：判成哪一类、改写成哪几句（不检索、不生成） |
| `search` | `-q/--question` | `--k` `--source` `--route {both,vector,bm25}` | 只检索、**不调模型** |
| `ingest` | — | `--path` `--dry-run` `--purge-source` `--batch-size` `--folder` | 切块入库；默认 `data/corpus/pdf_data`；`--folder 名字` 入到那一级目录（不存在就建，不给就按来源标签建） |
| `collections` | — | `--reindex` `--yes` | 列出集合（块数 / `page` 列 / 稀疏列 / 索引类型）；`--reindex` 按配置重建索引 |
| `sparse` | — | `--yes` `--batch-size` | 按当前语料重算全库 BM25 稀疏向量（`keyword.backend=milvus` 那一档） |
| `drop` | `--collection` | `--yes` | 删一个向量集合（不给 `--yes` 只说不做，退出码 1） |
| `metrics` | — | `--window` | 延迟报表（与 HTTP 同一份口径） |
| `user` | — | `add` / `passwd` / `list` | 账号管理 |
| `doctor` | — | — | 对账：登记表 vs 向量库（孤儿块 / 缺块），有问题退出码 1 |

约定：**退出码** `0` 成功 / `1` 业务失败（短消息在 stderr）/ `2` 用法错误；
**stdout 只放结果**（答案、引用、统计），提示一律走 stderr（便于管道消费）；
`ingest` 幂等（主键 = `sha256(来源|文档|位置|内容)`）；
答案不对时先 `search` 再 `rag` —— 分清是"没捞到"还是"答不好"。

---

## 14. 版本与兼容

- 版本号在 `pyproject.toml` 与 `FastAPI(version=…)` 各写一次，改动要同时改。
- **公开 schema 的权威是 `/openapi.json`**：本文档与它冲突时以它为准，并回来改本文档。
- **兼容性：只增不改** —— 新增字段给默认值；删字段 / 改类型 / 改路由属于破坏性变更，
  要同步改 `scripts/acceptance/check_m5_api.py`（端点契约的唯一权威）。
- 路由前缀一律 `/api/v1`，老路径已删除（那是唯一不用背兼容包袱的窗口）：`/api/query` → `/api/v1/query`、
  `/api/stream` → `/api/v1/stream`、`/api/create_session` → `/api/v1/create_session`、
  `/api/history/{id}` → `/api/v1/history/{id}`、`/api/sources` → `/api/v1/sources`、
  `/api/metrics` → `/api/v1/reports/metrics`。

# 变更记录

本文件记录所有**对外可见**的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 约定：`0.x` 期间次要版本可能有破坏性改动；每条变更都尽量带**证据指针**
>（契约脚本名），因为这个项目的规矩是"能验证的才算数"。

## [Unreleased]
### 新增（知识库两级目录）
- **一级目录**：`documents` 加 `folder` 列 + `document_folders` 表（名字唯一、ID 形如 `f_`+12 位十六进制、
  **改名不改 ID**）；空串 = **未分类**，是一个合法归属而不是错误状态（磁盘上还没登记的文件只能是它）。
- **接口**：`GET /api/v1/folders`（每格带对账后的份数 / 库内块数 / 占用 / 需要处理，含"未分类"与汇总）、
  `GET /api/v1/folders/{id}`（目录 + 里面的文档 + 汇总，一次往返）、`POST` / `PATCH` / `DELETE`
  （建 / 改名 / 删**空**目录；非空 `409` 并点出份数 —— 删目录不删文档）、
  `PATCH /api/v1/documents/{id}`（挪目录，只改归属）；清单与上传各多一个 `folder` 参数。
- **目录来自语料目录**：`data/corpus` 的每个一级子目录同步成一格（启动时同步、幂等；名字去掉
  `_data`），语料根本身不是一格；语料之外的来源（`uploads`、临时目录）**不再**凭空建目录。
- **入库落点**：默认 = 文件在语料根下的一级目录，`--folder 公司制度` 可显式指定；语料之外的路径落未分类。
  从语料根灌与从子目录灌给出同一批 `doc_id`/`source`（`registration_anchor`），两种入口互相幂等。
  同名文件**不能换目录**（`document_id` 就是文件名，两格会互相删块）：同一目录重传是覆盖，换目录 `409`。
- **老库一次性回填**：加 `folder` 列那一次按 `source` 把老行归进同名目录；**之后不再回填**（移回未分类不会被重启塞回去）。
- **前端两级**：一级是目录卡（`#/knowledge`）；二级 `#/knowledge/:folder` 是面包屑 + 统计条 +
  搜索 / 状态筛选 / 排序 + 文档表格（多选、批量删除、预览 / 权限 / 移动 / 两步确认删除）+ 右侧上传与目录信息 + 空状态。
  上传落点 = 当前目录。`DocumentCard` 换成 `DocumentTable`，"两步确认"这条口径不变。
- 验收：`check_m7_5_folders.py`（23 条，含真 MySQL 的迁移与唯一约束、同名换目录 409、语料目录同步与登记根归一化）；`reverse_m7_5_folders.py`（9 条全红）；
  `check_m7_2_kb` / `check_m7_2_live_kb` 改指两级页面后复跑通过。

### 新增（图片模态）
- **图片入库**：MinerU `content_list.json` → 正文 + 页码区间 + 图片锚点；图片按**字节 SHA-256**
  内容寻址上传阿里云 OSS（`x-oss-forbid-overwrite` 处理并发，同一张图不会产生第二个对象）；
  父块图片以 `image_keys` 随子块落 Milvus（新集合 `rag_chunks_img_v1`），**不把临时 URL 写进库**。
- **引用出参**：`CitationOut.images[]` 给 `key`（稳定身份）与 `url`（现签、默认 300s）；
  WebSocket `sources`/`done` 与历史接口同一形状。历史只存 key，回看时重新签。
- **多模态**：`ChatMessage.images`；RAG 只把前 3 张图的签名 URL 附在用户消息里，
  提示词给出 `[n]` 对应关系；纯文本路径的提示词逐字不变。实测 `deepseek-flash` 接受
  HTTPS URL 与 `data:image/...;base64` 两种形式。
- **CSP**：`img-src` 允许 `https:`（OSS/CDN 签名 URL），脚本仍只许 `'self'`。
- **MinerU 独立服务**：`services/mineru/`（任务队列、PDF 哈希缓存、zip 结果）+ 主 app 的
  `MinerUClient`；compose 里带 `profiles: ["images"]`，不开图片时不构建/不启动。
  zip 里附带 `full.md`/`input.md` 只做 sidecar（审计/人工排查），切块仍只用
  `content_list`（它才有 page_idx 与图片锚点）；图片按 `images.min/max_bytes` 与
  `max_per_doc` 过滤。MinerU 默认 `MINERU_MODEL_SOURCE=modelscope`（HuggingFace 在不少
  国内网络不通），首次解析下载约 1.9GB 模型；真机已用一份带图 PDF 跑通（CPU 约 14 分钟）。
- 验收：`check_image_modal.py`（7 条：内容哈希、OSS 幂等、content_list 解析、父块归属、引用签名、
  历史兼容、多模态前 3 张）；`check_m3_rag` / `check_p0_5_pages` / `check_m3_sparse` / `check_doc_formats` /
  `check_m7_frontend` / `check_m6_deploy` / `check_m7_4_security` 复跑通过。

### 变更（MinerU 默认用 GPU，超时 3600 秒）
- compose 的 `mineru` 服务默认申请 GPU：`gpus: all` + `MINERU_DEVICE_MODE: cuda`（两处必须成对），
  并用 `MINERU_VIRTUAL_VRAM_SIZE: "8"` 给同卡的 app 嵌入/重排留余量；没有 GPU 时两处一起改回 CPU。
- 服务端 `MINERU_TIMEOUT_S`、客户端 `RAG_MINERU__TIMEOUT_S`、`config` 默认值统一 **900 → 3600**；
  app 容器在 compose 里显式覆盖为 3600，避免 `env_file` 的旧值在重建后才生效。
- **改这些参数必须 `docker compose up -d` 重建容器，`restart` 不重读环境变量/设备**。
  实测 RTX 5080 Laptop：3 页带图 PDF 36.6s、3 张图、显存峰值 7479 MiB（含 app 的 4476 MiB）。
- 验收：`check_m6_deploy` 23/0/0；`reverse_m6_deploy` 34 例全红（RT-18 锚点改为 app 专属段落）。

### 新增（账号自服务与主题）
- **自助注册**：`POST /api/v1/auth/register`。图形验证码（`GET /api/v1/auth/captcha`，PNG data URI、
  一次一票、TTL 120s）+ 自助口令策略（长度下限、字母+数字、不含登录名、非弱口令）+ 可选邀请码；
  角色固定 `employee`，响应直接带令牌。`api.registration.enabled=false` 时 `403`，
  前端按 `capabilities.registration` 决定显示还是隐藏注册入口。契约 `check_p0_8_account.py`（13 条）。
- **改密**：`POST /api/v1/auth/password`。成功后**撤销该用户全部旧令牌**（其它设备下次请求 401）
  并下发新令牌；原密码错 401，新密码不合规 400（`Authenticator.change_password`）。
- **深浅色主题**：`stores/theme.ts` 三态（跟随系统 / 浅色 / 深色），写入 `<html data-theme>`，
  选择持久化；`tokens.css` 同名变量覆盖，组件不感知主题；首帧之前应用，无闪白。
- **前端工程化**：登录/注册两页共用 `ThemeToggle`；登录页密码显隐、**预填上次登录名**、**错误按字段定位**
  （用户名/密码/确认密码各有位置，服务端判定的错误仍显示在顶部）；改密面板在顶栏（改完提示其它设备失效）；
  **Esc 关闭顶栏浮层**（监听在卸载时摘掉）；**标题随路由变化**（`登录 · RAG Assistant` 这类，
  多标签页里能认出来）。

### 前端（全站版式与文案收敛）

- **统一骨架**：新增 `PageShell` / `Surface` / `DialogShell` / `AuthShell` / `EmptyState` 五个共享组件；
  知识库、报表、账号、登录、注册全部改用同一套页头/区块/弹层，桌面与 375px 走同一套断点。
- **超简约文案**：删除界面上的解释性长句（检索参数说明、对账说明、上传异步说明、权限"怎么读"等），
  只保留短标签、状态、错误与可执行命令；必要的口径移到 `title` 或数据行。
- **视觉**：页面改为浅灰画布 + 白色细描边区块、统一 22px 页标题/13px 区块标题、表格/指标卡/表单行统一；
  登录/注册改为左侧品牌 + 右侧单列表单，移动端收成单列；报表 KPI 在移动端两列。
- 契约同步：`browser_m7_2_kb.cjs` / `browser_m7_3_metrics.cjs` 的登录等待从已不存在的 `Configuration`
  文案改为 `.workspace`；`MessageList` 的重新生成提示断言同步为短文案。

### 前端（对话工作台重构）

- **三栏工作台**：左侧会话目录（按时间分组、搜索、本机重命名、行内删除确认）、中间消息流、
  右侧引用原文面板，顶栏「检索设置」抽屉收纳知识库/检索方式/权重/重排/模型；1440 三栏、
  ≤1180 两栏改浮层、≤768 侧栏抽屉 + 引用底部弹层。检索参数仍来自 `capabilities`，未新增任何能力真源。
- **消息流**：自研 Markdown 渲染（零新增依赖：交付环境 npm registry 不可达、CI 用 `npm ci`），
  支持标题/列表/引用/表格横向滚动/围栏代码高亮与复制；全部文本先转义，链接只放行 http(s)/mailto/站内路径；
  行内 `[n]` 是按钮（hover 摘要、点击定位右栏），未知序号保留灰色不猜。
- **引用溯源**：引用卡片显示序号/文件/来源/页码/相关度条；右侧面板给出元数据、命中片段与
  父块原文，命中块高亮并自动滚到可视区；来自表格的预览画成缩略表格。
- **置信度**：只认可解释量纲（FAQ 命中 / rerank 0~1 / 单路向量余弦），BM25/RRF 等不可跨查询比较的分数
  只画「相对强度」并明说；中置信度出「AI 建议」徽章，低置信度/拒答出「知识库中未找到足够信息」
  与「换个问法」「上传相关文档」两个动作，不写通用错误。
- **输入与流式**：Textarea 自适应 ≤200px、Enter/Shift+Enter（兼容中文输入法）、管理员附件上传
  （走知识库上传接口，消息里显示缩略图/状态）、拖拽、知识库单选、生成中「停止」；
  WebSocket 帧驱动、首字前先显示检索状态与已有依据、生成中禁用重新生成；点赞点踩/重命名仅本机。
- 验收：`npm run typecheck` 0 错误；`check_m7_frontend.py` / `check_m7_4_security.py` / `check_m5_api.py`
  全绿；浏览器脚本 `scripts/acceptance/browser_m7_frontend.cjs` 覆盖 1440/375、引用定位、低置信度、
  暗色与生成态；Markdown 安全用例（XSS/协议白名单/代码与表格）单独跑通。

### 性能（两路召回并行）

- **知识库检索的两路召回改成并行**：稠密向量（Milvus）与关键字（Milvus 稀疏 / 进程内 BM25）
  此前是串着跑的，现在关键字那一路在子线程里跑、汇齐后再融合。真栈 A/B（真语料、真关键字编码，
  各 5 次取中位）：串行 稠密 **398ms** + 关键字 **399ms = 797ms** → 并行墙钟 **400ms**；
  省下的正好是"较快那一路"的时间，所以随当次的一致性等待落在 **200–400ms**（真问答墙钟的 10–20%）。
  只有一个检索进行时不建线程；前端「只用向量 / 只用关键字」两档行为逐字不变。
- **分段口径跟着改**（`StreamTiming.span()`）：`vector_ms` / `bm25_ms` 由**各路自己报自己的时长**，
  不再用"相邻两个打点之差" —— 并行下那个算法会算出负数、或者把"等另一路"的时间算进融合段。
  因此「六段之和 ≈ `retrieve_ms`」改成「六段各自是自己的时长，两路重叠时和**可以大于**
  `retrieve_ms`、绝不会小于它」。顺带修掉一个假 `-`：只开关键字那一路时 `bm25_ms` 以前报不出来。
- 子线程里的异常**带回来由主线程重抛**（`Thread.join` 不传异常，否则会变成"关键字那一路
  静默消失"：少召回一半而账上一切正常）；两路都抛时抛先查的那一路，另一路记 WARNING。
- 验收：`check_m5_5_latency.py` **28 → 30 条**（并行墙钟 < 0.7×两段之和、融合段不吸等待、
  只开关键字时报自己的时长）；反向 `reverse_m5_5_latency.py` **2 → 4 例全红**。
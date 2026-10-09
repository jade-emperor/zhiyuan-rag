# 变更记录

本文件记录所有**对外可见**的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 约定：`0.x` 期间次要版本可能有破坏性改动；每条变更都尽量带**证据指针**
> （契约脚本名 / 维护者本地 `docs/STATUS.md` 的章节），因为这个项目的规矩是"能验证的才算数"。

## [Unreleased]
### 变更（数据与演示）

- **演示语料换成自写的面试八股**（Python / 数据库 / Linux / 提示词与 Agent / RAG /
  Transformer 与微调 / 部署，37 题 + 33 条 FAQ），结构与作者本机的真实语料一致；
  原来的虚构"示例学堂"语料（课程/退费/发票）已删除。演示流程不变：
  `./scripts/dev.sh demo` → `demo-serve`。
- 新增两个离线工具：`scripts/tools/build_qa_dataset.py`（八股确定性 + PDF 用 LLM 生成问答对，
  带溯源与逐条校验）与 `scripts/tools/reset_data.py`（数据换代：**先备份再删**、默认 dry-run、
  `users` 表受保护）。
- 验收新增 4 条契约（`check_m4_eval`：新评估集结构/溯源、问答对分桶、评估题不得与 faq 表近似、
  演示语料自写且两条演示问题各归其道）+ 新增反向 runner `reverse_m4_eval.py`（3 例）。


开源化第二、三批（进行中）：

### 新增
- **演示模式**（`demo/` + `./scripts/dev.sh demo`）：自写的虚构语料 + `HashingEmbedder`，
  不下载 4.4G 模型也能跑通 ingest → 检索 → 带引用的回答（预热 1405ms，真模型 CPU 上是 18240ms）。
  与生产配置只差三个环境变量；`embedding.backend` 默认值仍被契约钉在 `bge_m3`（+ 反向 RT-6）
- CI（GitHub Actions）：`pyright` + 零容器零网络的契约 + 全部反向 runner
- `CONTRIBUTING.md` / `SECURITY.md` / `CODE_OF_CONDUCT.md` / issue 与 PR 模板 / dependabot

### 变更
- 公开验收脚本 `scripts/acceptance/**` 与离线工具 `scripts/tools/**`；
  `docs/**`（维护者的决策与状态文档）、`data/**`、`models/**` **不公开**
- 新增聚合入口 `scripts/acceptance/run_all.py`（按外部依赖分 `ci`/`services`/`local` 三档）
  与短命令入口 `./scripts/dev.sh`

## [0.1.0] - 2026-10-07

**首个公开版本**（此前的 16 个提交是开发期；本条目按"能力"列，不按提交列）。

### 新增

**检索与回答**
- FAQ 标准答案通道：BM25 相似度匹配 + 阈值闸门（0.85，实测可用区间 `(0.303, 1.000]`）+
  Redis 旁路缓存 + LLM 兜底
- 知识库通道：BGE-M3 稠密向量（Milvus）+ 进程内 BM25 关键字，两路融合（RRF / weighted 都实现，
  默认 RRF），可选 CrossEncoder 重排；答案带 `[1][2]` 引用与页码
- 合流入口：FAQ 命中直答，否则知识库兜底；检索为空**直接拒答且不调 LLM**
- 多轮会话（MySQL 落库、5 轮裁剪）、逐字流式（WebSocket 帧协议 `sources`/`token`/`done`）

**接口与服务**
- FastAPI：`/api/v1/*` 统一命名空间（无别名）、能力自述 `capabilities`、Bearer 认证与角色、
  文档治理（上传 → 异步入库 → 预览 → 删除）、管理员延迟报表、Redis 滑动窗口限流 + 兜底话术
- 前端：Vite + Vue3 + TS（聊天页 / 知识库管理页 / 可观测看板），产物入库、hash 路由、桌面壳 `--serve`
- 部署：两阶段 `Dockerfile`（非 root、`/health` 探活）+ `docker-compose.yaml`（服务名互连、
  只读挂载、健康依赖）+ GPU 可选（`gpus: all` + `RAG_EMBEDDING__DEVICE=cuda`）

**工程与验收**
- 端口-适配器分层，**由 AST 扫描强制**（`domain/` 只用标准库、`adapters/` 是唯一碰第三方 SDK 的地方）
- 23 套验收契约（PASS/FAIL/**SKIP 点名 owner**）+ 20 份反向测试（把代码改坏一次，证明检查真的会红）
- 延迟观测：一行日志 26 个字段（TTFT / 检索六段 / 思考段 / 冷热 / 拒答截断），事件入 MySQL 账本
- 多格式入库：`.md` / `.txt` / `.docx` / `.pptx` / `.pdf`（父子分层切分 + 页码区间）

### 修复（开发期踩过、已钉进契约的代表性事故）
- 检索指纹每请求一次 Milvus 往返（397.6ms）→ 指纹摘要进 Redis，稳态 **~400ms → ~2ms**
  （`check_m3_bm25_cache` + 反向 4 条）
- 静态资源搬家后 `/static/app/` 是 404（`StaticFiles` 要显式 `html=True`）→ 根路径 307 到目录 URL
- `/api/v1/sources` 一直是裸的（业务数据不要令牌）→ 契约改成**全表扫描 + 白名单**
- 上传后删除文档会留下孤儿向量 → 删除改为三处一起清（登记行 / 向量 / 磁盘文件）
- 桌面壳按 `file://` 打开产物导致白屏（module 脚本被 CORS 挡）→ 壳自己起只绑回环的静态服务

### 安全
- 令牌：PBKDF2-SHA256 摘要入库、服务端撤销（登出真的失效）、12 小时 TTL
- 路由守卫默认拒绝 + 显式白名单；管理动作单独断言 401/403/放行
- 上传：后缀与大小白名单（来自 `capabilities`）、原始 body 传输、异步入库
- 静态产物带 CSP；服务端刻意不发 cookie（`allow_credentials=False`）
- 密钥只走 `.env`（从未入版本库），`.env.example` 三个必填键空值会 fail-fast

### 已知边界
- 未接：HTML / Excel / 扫描件 OCR；意图识别与检索策略选择；按天聚合看板与语义缓存
- 桌面壳的开窗与打包**未在真机验证**（这台开发机没有图形界面）
- 镜像 11G（CUDA 版 torch）；CPU-only 轮子的换法写在 `Dockerfile` 末尾但尚未验证
- 默认配置面向**本机开发**：对外部署前务必按 `SECURITY.md` 收紧 CORS、换掉默认口令、加反代限流

[Unreleased]: https://gitee.com/The_Jade_Emperor/project_rag/compare/v0.1.0...HEAD
[0.1.0]: https://gitee.com/The_Jade_Emperor/project_rag/releases/tag/v0.1.0

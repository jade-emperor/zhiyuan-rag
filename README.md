# project_rag

一个 RAG（检索增强生成）学习项目：**Python 3.13 + LangChain + Milvus + MySQL + Redis**，按企业级分层（端口-适配器）逐步搭建。

> 📌 **接手这个项目请先读 [`docs/STATUS.md`](docs/STATUS.md)** —— 里面有当前进度、架构硬约束、验收基线、环境版本和下一步契约。
> 不做但已想清楚的事记录在 [`docs/deferred.md`](docs/deferred.md)。

## 快速开始

```bash
uv sync
uv run project-rag chat -p "用一句话解释什么是向量数据库"
```

## 验收（回归门槛，必须全绿）

```bash
uv run pyright src
uv run python scripts/acceptance/check_m1_1_config.py     # 12 通过
uv run python scripts/acceptance/check_m1_2_logging.py    #  8 通过
uv run python scripts/acceptance/check_m1_3_cli.py        # 11 通过
uv run python scripts/acceptance/check_env_services.py    #  5 通过（需先起容器）
uv run python scripts/acceptance/check_m2_faq.py          # 23 项 M2 契约（实现前为红灯）
```

> M2 契约**先行**：`check_m2_faq.py` 的 docstring 是 M2 公开契约的权威来源，
> 当前 0 通过 / 23 失败是预期起点，失败清单即待实现清单。
> 契约从 M1 的 `llm_factory=` 换成 `Deps` 注入后，`check_m1_3_cli.py` 的 3 个注入检查
> 在 M2 实现完成前也会是红灯。

## 基础设施

```bash
docker compose up -d
docker compose ps
```

| 宿主端口 | 服务 |
|---|---|
| 10086 | Milvus gRPC |
| 10087 | Redis（密码 1234） |
| 10093 | MySQL（库/用户 `rag`，密码 1234） |
| 10092 | Attu（Milvus 图形界面） |

## 阶段

- [x] **M1 工程地基**：类型化配置、日志、LLM 端口/适配器、CLI、静态检查、验收体系
- [ ] **M2 FAQ 通道**：MySQL + 相似匹配 + Redis（契约已定稿，见 `docs/STATUS.md` 第 8 节；权威 = `scripts/acceptance/check_m2_faq.py`，当前红灯）
- [ ] M3 向量知识库与 RAG 问答
- [ ] M4 两通道融合路由
- [ ] M5 评估闭环
- [ ] M6 服务化与部署

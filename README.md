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
```

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
- [ ] **M2 FAQ 通道**：MySQL + 相似匹配 + Redis（契约草案见 `docs/STATUS.md` 第 8 节）
- [ ] M3 向量知识库与 RAG 问答
- [ ] M4 两通道融合路由
- [ ] M5 评估闭环
- [ ] M6 服务化与部署

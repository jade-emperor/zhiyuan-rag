# MinerU 独立解析服务

这是 `src/project_rag/adapters/mineru_client.py` 的服务端实现，跟主 app **分进程**：

- 主 app：上传 PDF → 调本服务 → 拿 zip → 图片上传 OSS → 切块入 Milvus
- 本服务：排队跑 MinerU → 产出 `content_list.json + images/` → 打成 zip

## 运行

```bash
# 本目录下
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# 另装 MinerU（本服务不钉它的版本，按官方文档安装到同一环境 / PATH）
export MINERU_TOKEN=change-me
export MINERU_OUTPUT_DIR=./data/mineru
uvicorn app:app --host 0.0.0.0 --port 8000
```

主 app 侧：

```dotenv
RAG_MINERU__BASE_URL=http://127.0.0.1:8000
RAG_MINERU__TOKEN=change-me
```

Docker compose 里 `BASE_URL` 用服务名 `http://mineru:8000`。

## 契约

```http
POST /v1/parse          multipart: file=@a.pdf   -> 202 {"job_id":"..."}
GET  /v1/jobs/{job_id}                            -> {"status":"queued|running|done|failed","result_url":"...","error":"..."}
GET  /v1/jobs/{job_id}/result.zip                 -> zip(content_list.json, images/, full.md 可选)
```

认证：设置 `MINERU_TOKEN` 后，请求必须带 `X-MinerU-Token`。

## 注意

- 默认单 worker；MinerU 很吃 CPU/GPU，别盲目加并发。
- 结果按 PDF 内容 SHA-256 缓存；同一份 PDF 重复提交不会重跑。
- zip 里的 `full.md` 只是 sidecar（人类排查用），主 app 的切块仍只用 content_list（页码/图片锚点）。
- `MINERU_BIN` 默认 `mineru`，必须是 PATH 里可执行的 MinerU CLI。
- 任务状态在内存里，服务重启会丢“正在跑”的任务；已完成的 zip 还在磁盘上，可复用。

## GPU

compose 的 `mineru` 服务默认已申请 GPU，下面两处**必须成对**：

```yaml
gpus: all
environment:
  MINERU_DEVICE_MODE: cuda
  MINERU_VIRTUAL_VRAM_SIZE: "8"
```

app 的嵌入/重排也在同一张 16GB 卡上（约 4~5GB），所以 `MINERU_VIRTUAL_VRAM_SIZE` 按 8GB
估算 batch，不把整卡都算进去。本机实测：3 页带图 PDF（模型已缓存）约 **37 秒**，显存峰值
约 **7.5GB**（含 app 的 4.5GB）。没有 GPU 的机器把 `gpus: all` 与 `MINERU_DEVICE_MODE`
**一起**改回 CPU；只改一处时容器里可能根本看不见卡。

## 版本

镜像里钉的是 `mineru[core]==2.6.4`（与适配器验证过的 `content_list` / CLI 形状一致）。
换 4.x 前先跑一份带图 PDF，确认输出目录与 `mineru -p ... -o ...` 没变。

## 首次运行

默认 `MINERU_MODEL_SOURCE=modelscope`（HuggingFace 在不少国内网络不通）。第一次解析会
下载模型（本机实测模型缓存约 **1.9 GB**；CPU 上两页 PDF 连下载约 **14 分钟**），之后走缓存。
超时默认已是 **3600 秒**（GPU 上大 PDF 也够用）；第一次部署可先拿一份小 PDF 预热模型缓存，
确认 `/health` 之外真的能产出一份 `content_list.json`。

```bash
# 只想临时放宽（不改 compose）时：
RAG_MINERU__TIMEOUT_S=3600
```

模型缓存在 compose 的 `mineru_models` 卷（`/root/.cache`）里，重建容器不丢。

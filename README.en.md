# project_rag

A retrieval-augmented Q&A service over a curated FAQ table plus a document corpus: **curated FAQ
answers first, hybrid retrieval as the fallback** — every answer carries `[1][2]` citations, streams
token by token, and every latency segment is accounted for in one log line.

**Stack**: Python 3.13 · FastAPI · LangChain · Milvus 2.4 · MySQL 8 · Redis 7 · Vue 3 + Vite.
**Architecture**: ports & adapters — `ports/` only declares capabilities, `adapters/` is the single
place that touches third-party SDKs, `domain/` uses the standard library only. Swapping the model
vendor, the vector store or the cache means changing one adapter file.

> 中文说明（更详细）见 [`README.md`](README.md)。维护者的决策与状态文档（`docs/`）**不随本仓库发布**；
> 本仓库里可验证的东西是 `scripts/acceptance/` 的 23 份契约 + 15 份反向测试。

## Try it in 5 minutes (no model download)

```bash
./scripts/dev.sh up            # start Milvus / MySQL / Redis (skip if already running)
./scripts/dev.sh demo          # load the demo corpus (self-written bagu-style interview Q&A: 37 items + 33 FAQ rows)
./scripts/dev.sh demo-serve    # serve on http://127.0.0.1:18080/
```

The demo swaps in a **byte-hash embedder** (zero model, 256-dim) instead of BGE-M3, uses a separate
Milvus collection (`rag_demo`) and turns reranking off — the product code has no "demo branch",
just one explicit config switch whose default is pinned by a contract. Details, and how to undo
everything it writes: [`demo/README.md`](demo/README.md).

Ask two questions to see both channels:

| Question | Path | Expect |
|---|---|---|
| `深拷贝与浅拷贝的区别?` | FAQ (curated answer, **no LLM call**) | instant, verbatim answer |
| `混合检索与 RRF 融合是怎么回事` | knowledge base (vector + BM25 → LLM) | answer with a `[1]` citation |

## Production-ish quickstart

```bash
uv sync                                                   # Python 3.13, pinned by uv.lock
cp .env.example .env                                      # fill in the 3 required secrets
mkdir -p logs data/uploads                                # the two dirs the container writes to
uv run project-rag user add -u <name> --role admin         # there is no sign-up endpoint
docker compose up -d standalone mysql redis                # Milvus + MySQL + Redis
uv run python scripts/tools/fetch_models.py --with-reranker   # 4.4 GB (BGE-M3 + reranker)
uv run project-rag ingest --path data/corpus/<your_corpus>    # md / txt / docx / pptx / pdf
uv run project-rag-api                                     # http://127.0.0.1:18080/ (config.yaml default)
# Same thing as a short command (adds a port guard): ./scripts/dev.sh serve
```

Ports have three roles, don't mix them: **18080** = the code backend on the host
(`uv run project-rag-api` / `./scripts/dev.sh serve`), **8080** = the app in the container
(`docker compose up -d`; the image's `RAG_API__PORT=8080` overrides the yaml), **5173** = the vite
dev server (`cd web && npm run dev`). Both backends can run at once; see what is currently taken
with `./scripts/dev.sh ports`.

Or the whole thing in containers: `docker compose up -d --build` (host port 8080; GPU optional via
`gpus: all` + `RAG_EMBEDDING__DEVICE=cuda`, see `docker-compose.yaml`).

Open `http://127.0.0.1:18080/` (307 → `/static/app/`) for the SPA: **chat** for any signed-in user,
**knowledge base** and **metrics** for admins. The page's only configuration is *one backend URL*
(editable in the header, remembered) — the desktop shell relies on exactly that.

## What's inside

| Area | Highlights |
|---|---|
| Retrieval | BGE-M3 dense vectors (Milvus) + in-process BM25, fused with RRF (or weighted), optional CrossEncoder rerank; FAQ channel gated by a calibrated similarity threshold (code default `0.85`, `config.yaml` ships `0.95` — the value must stay inside a measured interval, asserted by a contract) |
| Answers | Streaming over WebSocket (`sources` / `token` / `done` frames), citations with page numbers, explicit refusal when retrieval is empty (**no LLM call at all**) |
| API | 16 business endpoints under `/api/v1/*`, capability self-description (`/capabilities` — the frontend hard-codes nothing), Bearer auth + roles, document governance (upload → async ingest → preview → delete), admin latency reports, Redis sliding-window rate limiting with a friendly fallback message |
| Frontend | Vue 3 + TS SPA (chat / knowledge base / metrics views), hash router, artifact committed so the Python side and the image need no Node |
| Desktop | `project-rag-desktop`: a native window (pywebview) around the same artifact — local mode serves it over loopback HTTP, `--remote URL` opens the backend-hosted copy |
| Ops | One log line with 26 fields per request (TTFT, six retrieval segments, cold/warm, refusal/truncation), ledger in MySQL, healthcheck, non-root container |
| Engineering | 23 executable contracts + 15 reverse-test runners (each contract is proven to fail when the code is broken), AST-enforced layering, `pyright` clean |

## Repository layout

```text
src/project_rag/     domain / ports / adapters / application / observability / cli / api
web/                 Vue 3 + Vite frontend (build output goes into src/project_rag/api/static/app/)
scripts/acceptance/  23 contracts + 15 reverse runners + fake ports (published: it's how we verify)
scripts/tools/       offline tools: fetch models, load FAQ CSV, build eval sets, run RAGAS
demo/                self-written bagu-style interview Q&A for the 5-minute demo (no third-party content)
config/config.yaml   every knob (retrieval, models, device, thresholds, rate limits)
docker-compose.yaml  Milvus (etcd+minio) / MySQL / Redis / Attu + the app service
Dockerfile           two-stage build for the app image
API.md               HTTP / WebSocket / CLI reference
CHANGELOG.md         Keep a Changelog + semver
```

## Verification

```bash
./scripts/dev.sh ci        # pyright + 16 contracts + 15 reverse runners, no containers needed (~10 min)
./scripts/dev.sh all       # + the checks that need real middleware / corpus / a real provider key
```

Contracts print `PASS` / `FAIL` / `SKIP`; a `SKIP` always names its owner (missing model, missing
corpus, no GPU in the sandbox…). Reverse runners deliberately break the code and assert the matching
contract turns red — a contract that has never gone red does not count as a contract here.

## License

MIT — see [`LICENSE`](LICENSE). Model weights have their own licenses and are **not** distributed
here; `data/` (course corpus) and `docs/` (maintainer notes) are not part of this repository.

# project_rag

A retrieval-augmented Q&A service over a curated FAQ table plus a document corpus: **curated FAQ
answers first, hybrid retrieval as the fallback** — every answer carries `[1][2]` citations, streams
token by token, and every latency segment is accounted for in one log line.

**Stack**: Python 3.13 · FastAPI · LangChain · Milvus 2.4 · MySQL 8 · Redis 7 · Vue 3 + Vite.
**Architecture**: ports & adapters — `ports/` only declares capabilities, `adapters/` is the single
place that touches third-party SDKs, `domain/` uses the standard library only. Swapping the model
vendor, the vector store or the cache means changing one adapter file.

> 中文说明（更详细）见 [`README.md`](README.md)。维护者的决策与状态文档（`docs/`）**不随本仓库发布**；
> 本仓库里可验证的东西是 `scripts/acceptance/` 的 32 份契约 + 24 份反向测试。

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
uv run project-rag user add -u <name> --role admin         # no sign-up endpoint; --role employee / ops
docker compose up -d standalone mysql redis                # Milvus + MySQL + Redis
uv run python -c "from modelscope import snapshot_download as d; \
  d('BAAI/bge-m3', local_dir='models/bge-m3'); \
  d('BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3')"   # 4.4 GB (BGE-M3 + reranker; `modelscope` is a declared dependency)
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
**knowledge base**, and **reports** (latency + token cost) for the ops and admin roles; admins also get
**accounts & permissions** (create/delete accounts, groups, per-document visibility). The page's only configuration is *one backend URL*
(editable in the header, remembered) — the desktop shell relies on exactly that.

## What's inside

| Area | Highlights |
|---|---|
| Retrieval | BGE-M3 dense vectors (Milvus) + BM25 keywords — either in-process or as **sparse vectors inside Milvus** (`keyword.backend`), the two recall legs run **in parallel** (they don't depend on each other), fused with RRF (or weighted), optional CrossEncoder rerank; FAQ channel gated by a calibrated similarity threshold (code default `0.85`, `config.yaml` ships `0.95` — the value must stay inside a measured interval, asserted by a contract) |
| Answers | Streaming over WebSocket (`sources` / `token` / `done` frames), citations with page numbers (**old sessions get the same citation cards back** — each turn stores a snapshot, not a pointer), explicit refusal when retrieval is empty (**no LLM call at all**) |
| API | 29 business endpoints under `/api/v1/*`, capability self-description (`/capabilities` — the frontend hard-codes nothing), Bearer auth + three roles (employee / ops / admin), document governance (upload → async ingest → preview → delete → per-document **and** per-group visibility), latency + token-cost reports (three price tiers × two periods; peak = Beijing-time weekdays 09:00–12:00 / 14:00–18:00 excluding Chinese statutory holidays — the ledger clock carries its UTC offset so container-UTC and host-CST rows both classify correctly, and anything the report cannot know is counted instead of shown as 0), account & group management, Redis sliding-window rate limiting with a friendly fallback message |
| Frontend | Vue 3 + TS SPA (chat / knowledge base / reports / accounts views; tabs rendered from the capabilities reported by `/auth/me`), hash router, artifact committed so the Python side and the image need no Node |
| Desktop | `project-rag-desktop`: a native window (pywebview) around the same artifact — local mode serves it over loopback HTTP, `--remote URL` opens the backend-hosted copy |
| Index knobs | `milvus.index.type` = `FLAT` (exact, default) or `IVF_FLAT` (+`nlist`/`nprobe`); switching rebuilds the index in place (`project-rag collections --reindex --yes`). Measured: at 3.9k chunks both are 5.5 ms with recall 1.000; at 20k synthetic vectors IVF_FLAT is 3.8× faster. `keyword.backend=milvus` moves BM25 into the collection as sparse vectors (schema-level: needs a re-ingest) |
| Query rewriting | Optional step between an FAQ miss and vector retrieval: a fine-tuned 4-way classifier (`chinese-roberta-wwm-ext` + LoRA, `tools/lora/`) picks *direct / hypothetical answer (HyDE) / subquery decomposition / denoise*, then one LLM call turns the user's wording into the sentence(s) actually sent to retrieval. **Off by default** (`rewrite.enabled`); it rewrites retrieval only, never the answer, and any failure falls back to the original query. Measured on a 120-query ablation (paired, judge `deepseek-flash`, local BGE-M3): **context recall +0.0621** (95% CI [+0.017, +0.114]) and four-metric mean +0.0440 against a **+0.0141 noise floor** measured on the rows where rewriting changed nothing — at **+1.45 s** and **+0.294 CNY per 1000 queries** |
| Ops | One log line with **30 fields** per request (TTFT, six retrieval segments, the rewrite segment, cold/warm, refusal/truncation), ledger in MySQL, healthcheck, non-root container |
| Engineering | **32 executable contracts + 24 reverse-test runners** (each contract is proven to fail when the code is broken), AST-enforced layering, `pyright` clean |

## Repository layout

```text
src/project_rag/     domain / ports / adapters / application / observability / cli / api
web/                 Vue 3 + Vite frontend (build output goes into src/project_rag/api/static/app/)
scripts/acceptance/  32 contracts + 24 reverse runners + fake ports (published: it's how we verify)
                     (not shipped: scripts/tools/ — the author's offline tools, they run on data/ and local tables)
demo/                self-written bagu-style interview Q&A for the 5-minute demo (no third-party content)
config/config.yaml   every knob (retrieval, models, device, thresholds, rate limits)
docker-compose.yaml  Milvus (etcd+minio) / MySQL / Redis / Attu + the app service
Dockerfile           two-stage build for the app image
API.md               HTTP / WebSocket / CLI reference
CHANGELOG.md         Keep a Changelog + semver
```

## Verification

```bash
./scripts/dev.sh ci        # pyright + 23 contracts + 24 reverse runners (= 47 scripts), no containers needed (~20 min)
./scripts/dev.sh all       # + the checks that need real middleware / corpus / a real provider key
```

Contracts print `PASS` / `FAIL` / `SKIP`; a `SKIP` always names its owner (missing model, missing
corpus, no GPU in the sandbox…). Reverse runners deliberately break the code and assert the matching
contract turns red — a contract that has never gone red does not count as a contract here.

## License

MIT — see [`LICENSE`](LICENSE). Model weights have their own licenses and are **not** distributed
here; `data/` (course corpus) and `docs/` (maintainer notes) are not part of this repository.

"""
MinerU 独立异步解析服务(最小实现)。

**为什么单独一个服务**:MinerU 吃 CPU/GPU、单份 PDF 要几分钟,模型依赖与 app 的
Web 栈不是一套;把它塞进 app 进程会让"上传一份 PDF"和"回答一句问题"互相拖死。
app 只提交任务、轮询、下载 zip;服务自己排队、跑 MinerU、缓存结果。

启动(示例):

    MINERU_TOKEN=change-me MINERU_OUTPUT_DIR=/data/mineru \
      uvicorn app:app --host 0.0.0.0 --port 8000

依赖:``fastapi uvicorn python-multipart`` + 一个能执行的 ``mineru`` 命令
(装法见 MinerU 官方文档;本服务不把它钉进 requirements)。

契约见仓库 ``src/project_rag/adapters/mineru_client.py``:
``POST /v1/parse`` → ``{"job_id"}``;
``GET /v1/jobs/{id}`` → ``{"status","result_url","error"}``;
``GET /v1/jobs/{id}/result.zip`` → content_list.json + images/。
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

logging.basicConfig(level=os.getenv("MINERU_LOG_LEVEL", "INFO"))
log = logging.getLogger("mineru-service")

OUTPUT_DIR = Path(os.getenv("MINERU_OUTPUT_DIR", "/data/mineru")).resolve()
MINERU_BIN = os.getenv("MINERU_BIN", "mineru")
MINERU_TOKEN = os.getenv("MINERU_TOKEN", "")
MINERU_TIMEOUT_S = int(os.getenv("MINERU_TIMEOUT_S", "3600"))
MINERU_MAX_MB = int(os.getenv("MINERU_MAX_MB", "50"))
# MinerU 很吃资源:默认单 worker,别让两份 PDF 同时抢显存/CPU。
MINERU_WORKERS = max(int(os.getenv("MINERU_WORKERS", "1")), 1)

Status = Literal["queued", "running", "done", "failed"]


@dataclass
class Job:
    """一个解析任务的内存状态。服务重启会丢 —— 结果 zip 还在磁盘上,按 PDF 哈希可复用。"""

    job_id: str
    pdf_sha256: str
    status: Status = "queued"
    error: str = ""
    result_zip: Path | None = None
    file_name: str = ""
    created_at: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
QUEUE: queue.Queue[str] = queue.Queue()
APP = FastAPI(title="MinerU parser", version="0.1.0")


def _auth(x_mineru_token: str = Header(default="")) -> None:
    """输入:请求头。行为:配了 token 就要求一致。输出:无。"""
    if MINERU_TOKEN and x_mineru_token != MINERU_TOKEN:
        raise HTTPException(status_code=401, detail="MinerU token 不对")


def _safe_name(name: str) -> str:
    """输入:上传文件名。行为:只留 basename,防路径穿越。输出:文件名。"""
    return Path(name).name or "upload.pdf"


def _cache_zip(pdf_sha256: str) -> Path:
    """输入:PDF 内容哈希。行为:结果 zip 的缓存路径。输出:路径。"""
    return OUTPUT_DIR / "cache" / pdf_sha256 / "result.zip"


def _find_content_list(root: Path) -> Path | None:
    """输入:输出目录。行为:找 content_list.json。输出:路径;没有给 None。"""
    for candidate in sorted(root.rglob("*content_list.json")):
        return candidate
    return None


def _zip_content_list(content: Path, target: Path, *, full_md: Path | None = None) -> None:
    """输入:content_list 路径 + zip 目标 + 可选的 full.md。行为:把 content_list 所在目录压成 zip。输出:无。

    只压 ``content_list`` 的同级内容(含 ``images/``),客户端的 ``output_dir``
    就取 content_list 的父目录 —— 两边口径一致,不靠猜层级。

    ``full.md`` 是**旁路产物**:放在 zip 根目录供人排查,不参与切块。
    切块仍只认 content_list(它才有 page_idx 与图片锚点)。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    root = content.parent
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
        if full_md is not None and full_md.is_file():
            try:
                full_md.relative_to(root)
            except ValueError:
                archive.write(full_md, "full.md")


def _run_mineru(job: Job, pdf: Path) -> None:
    """输入:任务 + PDF。行为:跑 MinerU、找 content_list、压 zip。输出:无(状态写进 job)。"""
    work = OUTPUT_DIR / "jobs" / job.job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [MINERU_BIN, "-p", str(pdf), "-o", str(work)],
            capture_output=True,
            text=True,
            timeout=MINERU_TIMEOUT_S,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"mineru 退出码 {completed.returncode}:"
                f"{(completed.stderr or completed.stdout or '')[-800:]}"
            )
        content = _find_content_list(work)
        if content is None:
            detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            raise RuntimeError(
                f"MinerU 没有产出 content_list.json(工作目录 {work});"
                f"CLI 输出:{detail or '（空）'}"
            )
        target = _cache_zip(job.pdf_sha256)
        # full.md 只是 sidecar:人类可读/审计用,不参与切块。
        full_md = next(iter(sorted(work.rglob("full.md"))), None)
        _zip_content_list(content, target, full_md=full_md)
        job.result_zip = target
        job.status = "done"
        log.info("任务完成 job=%s pdf=%s", job.job_id, job.file_name)
    except Exception as exc:  # noqa: BLE001 - 任务失败要写进状态,不能把 worker 打死
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        log.exception("任务失败 job=%s", job.job_id)
    finally:
        # 原始上传文件不再需要;工作目录保留 content_list/images 便于排查。
        pdf.unlink(missing_ok=True)


def _worker() -> None:
    """输入:无。行为:串行消费队列。输出:无。"""
    while True:
        job_id = QUEUE.get()
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None or job.status == "done":
            QUEUE.task_done()
            continue
        with JOBS_LOCK:
            job.status = "running"
        pdf = OUTPUT_DIR / "jobs" / job.job_id / "input.pdf"
        _run_mineru(job, pdf)
        QUEUE.task_done()


for _ in range(MINERU_WORKERS):
    threading.Thread(target=_worker, name="mineru-worker", daemon=True).start()


@APP.get("/health")
def health() -> dict[str, str]:
    """输入:无。行为:探活 + 报 MinerU 命令是否存在。输出:状态。"""
    return {
        "status": "ok",
        "mineru": shutil.which(MINERU_BIN) or f"not-found:{MINERU_BIN}",
    }


@APP.post("/v1/parse", dependencies=[Depends(_auth)], status_code=202)
async def parse(file: UploadFile = File(...)) -> dict[str, str]:
    """输入:multipart PDF。行为:落盘、按哈希查缓存、没缓存就入队。输出:job_id。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    safe = _safe_name(file.filename)
    if not safe.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只接受 .pdf")

    data = await file.read()
    if len(data) > MINERU_MAX_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {MINERU_MAX_MB}MB")
    digest = hashlib.sha256(data).hexdigest()

    job = Job(job_id=uuid.uuid4().hex, pdf_sha256=digest, file_name=safe)
    cached = _cache_zip(digest)
    if cached.is_file():
        job.status = "done"
        job.result_zip = cached
        with JOBS_LOCK:
            JOBS[job.job_id] = job
    else:
        folder = OUTPUT_DIR / "jobs" / job.job_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "input.pdf").write_bytes(data)
        # **先登记再入队**:worker 可能立刻取走,晚一步就会查不到 job。
        with JOBS_LOCK:
            JOBS[job.job_id] = job
        QUEUE.put(job.job_id)
    return {"job_id": job.job_id}


@APP.get("/v1/jobs/{job_id}", dependencies=[Depends(_auth)])
def job_status(job_id: str) -> dict[str, str]:
    """输入:job_id。行为:查状态。输出:status / result_url / error。"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="没有这个任务")
    payload = {"status": job.status, "error": job.error}
    if job.status == "done":
        payload["result_url"] = f"/v1/jobs/{job_id}/result.zip"
    return payload


@APP.get("/v1/jobs/{job_id}/result.zip", dependencies=[Depends(_auth)])
def result_zip(job_id: str) -> FileResponse:
    """输入:job_id。行为:回结果 zip。输出:FileResponse。"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or job.result_zip is None or not job.result_zip.is_file():
        raise HTTPException(status_code=404, detail="结果还没准备好")
    return FileResponse(job.result_zip, media_type="application/zip", filename="mineru.zip")

# uvicorn 的入口约定是 ``module:app``;模块里叫 ``APP`` 只是风格,
# 这里显式给一个别名,别让 ``uvicorn app:app`` 报 "Attribute app not found"。
app = APP

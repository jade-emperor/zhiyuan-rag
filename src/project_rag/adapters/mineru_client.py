"""
MinerU 独立解析服务的 HTTP 客户端。

服务契约定死这三条(服务端实现见 ``services/mineru/``):

1. ``POST /v1/parse``(multipart,字段名 ``file``)→ ``{"job_id": "..."}``;
2. ``GET /v1/jobs/{job_id}`` → ``{"status": "queued|running|done|failed", "result_url": "...", "error": "..."}``;
3. ``GET result_url`` → 一个 zip,里面至少有 ``content_list.json`` 与 ``images/``。

**为什么结果用 zip 而不是共享卷**:app 与 MinerU 可能不在同一台机器。共享卷在
本机 compose 里最省事,但它把"部署形态"焊进了协议;zip 走 HTTP,放哪都能跑。
代价是一次下载,相对几分钟的解析可以忽略。

客户端**不传 OSS、不切块**:它只把一份 PDF 变成 ``ParsedDocument``(本地图片路径)。
上传是 ``ImageStore`` 的职责,归属父块是 ``chunk_documents`` 的职责。
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

from project_rag.config import MinerUConfig, project_path
from project_rag.observability import get_logger
from project_rag.ports import ParsedDocument

from .mineru_parser import parse_content_list_file

_logger = get_logger("adapters.mineru_client")


class MinerUClient:
    """提交→轮询→下载 zip→解析。同一份 PDF 按内容哈希缓存。"""

    def __init__(self, config: MinerUConfig) -> None:
        """输入:MinerU 服务配置。行为:保存配置,不连网。输出:无。"""
        self._config = config
        self._cache_root = project_path(config.cache_dir)

    def parse(self, path: Path) -> ParsedDocument:
        """输入:一份 PDF。行为:命中缓存直接解析,否则提交服务并等待结果。输出:ParsedDocument。"""
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cache_dir = self._cache_root / digest
        cached = self._find_content_list(cache_dir)
        if cached is not None:
            _logger.info("MinerU 命中缓存:%s", path.name)
            return parse_content_list_file(cached, output_dir=cached.parent)

        job_id = self._submit(path)
        result_url = self._wait(job_id)
        self._download_and_extract(result_url, cache_dir)

        found = self._find_content_list(cache_dir)
        if found is None:
            raise RuntimeError(
                f"MinerU 结果里没有 content_list.json(job={job_id},目录={cache_dir})"
            )
        return parse_content_list_file(found, output_dir=found.parent)

    # ── HTTP ────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        """输入:无。行为:构造认证头(没配 token 就不发)。输出:请求头。"""
        token = self._config.token.get_secret_value()
        return {"X-MinerU-Token": token} if token else {}

    def _submit(self, path: Path) -> str:
        """输入:PDF。行为:POST multipart 提交任务。输出:job_id。"""
        url = f"{self._config.base_url}/v1/parse"
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    url,
                    headers=self._headers(),
                    files={"file": (path.name, handle, "application/pdf")},
                    timeout=60,
                )
        except requests.RequestException as exc:
            raise RuntimeError(f"提交 MinerU 任务失败({url}):{exc}") from exc
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"MinerU 拒绝了任务({response.status_code}):{response.text[:300]}"
            )
        try:
            payload = response.json()
            job_id = str(payload["job_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"MinerU 返回里没有 job_id:{response.text[:300]}") from exc
        if not job_id:
            raise RuntimeError("MinerU 返回了空 job_id")
        return job_id

    def _wait(self, job_id: str) -> str:
        """输入:job_id。行为:轮询到 done。输出:result_url。"""
        url = f"{self._config.base_url}/v1/jobs/{job_id}"
        deadline = time.monotonic() + self._config.timeout_s
        while True:
            try:
                response = requests.get(url, headers=self._headers(), timeout=30)
            except requests.RequestException as exc:
                raise RuntimeError(f"轮询 MinerU 任务失败({url}):{exc}") from exc
            if response.status_code != 200:
                raise RuntimeError(
                    f"MinerU 任务查询失败({response.status_code}):{response.text[:300]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(f"MinerU 任务状态不是 JSON:{response.text[:300]}") from exc
            status = str(payload.get("status") or "")
            if status == "done":
                result_url = str(payload.get("result_url") or "")
                if not result_url:
                    raise RuntimeError("MinerU 任务 done 了却没有 result_url")
                return urljoin(f"{self._config.base_url}/", result_url)
            if status == "failed":
                raise RuntimeError(f"MinerU 解析失败(job={job_id}):{payload.get('error') or '未知原因'}")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"MinerU 解析超时(job={job_id},{self._config.timeout_s}s)"
                )
            time.sleep(self._config.poll_interval_s)

    def _download_and_extract(self, result_url: str, cache_dir: Path) -> None:
        """输入:结果 URL + 缓存目录。行为:下载 zip 并安全解压。输出:无。"""
        try:
            response = requests.get(result_url, headers=self._headers(), timeout=120)
        except requests.RequestException as exc:
            raise RuntimeError(f"下载 MinerU 结果失败:{exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"下载 MinerU 结果失败({response.status_code}):{response.text[:300]}"
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                root = cache_dir.resolve()
                for member in archive.infolist():
                    target = (root / member.filename).resolve()
                    try:
                        target.relative_to(root)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"MinerU zip 里有越出目录的路径:{member.filename!r}"
                        ) from exc
                archive.extractall(root)
        except zipfile.BadZipFile as exc:
            raise RuntimeError("MinerU 返回的不是合法 zip") from exc

    @staticmethod
    def _find_content_list(cache_dir: Path) -> Path | None:
        """输入:缓存目录。行为:找 ``*content_list.json``。输出:路径;没有给 None。"""
        if not cache_dir.is_dir():
            return None
        # 本地/云端 MinerU 的目录层级不完全一样(auto/ 子目录或根目录),用 glob 不写死。
        for candidate in sorted(cache_dir.rglob("*content_list.json")):
            return candidate
        return None

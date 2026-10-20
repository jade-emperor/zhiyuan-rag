"""
阿里云 OSS 图片存储(内容寻址 + 幂等上传 + 短时签名 URL)。

**幂等的全部依据只有一条:对象 key 由图片字节的 SHA-256 决定。**
文件名、文档名、上传时间、页码都不进 key —— 同一张图在 A 文档和 B 文档里、
第 1 次和第 10 次上传,得到的 key 必须一样。

写入流程刻意是「HEAD → PUT(禁止覆盖)」:

1. ``object_exists`` 命中 → 直接返回 key,不传字节;
2. 未命中 → ``PUT`` 带 ``x-oss-forbid-overwrite: true``;
3. 并发时另一个 worker 刚写完 → OSS 回 409,按成功处理,仍然返回同一个 key。

为什么不是「先 DELETE 再 PUT」:那会把别的文档正在引用的对象删掉。
为什么不是「PUT 覆盖」:内容虽然相同,但覆盖会掩盖"两个不同内容撞了同一个 key"
(理论上 SHA-256 不会,但工程上要能看见),也会让 OSS 产生多余版本/事件。
"""

from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from typing import Any

from project_rag.config import OssConfig
from project_rag.observability import get_logger
from project_rag.ports import ImageStore

_logger = get_logger("adapters.oss_images")

# Pillow 认得的格式 → (扩展名, Content-Type)。扩展名按**字节**判断,
# 不按 MinerU 给的文件名 —— 否则同一份字节可能被存成 .jpg 和 .png 两个 key。
_IMAGE_TYPES: dict[str, tuple[str, str]] = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
    "BMP": ("bmp", "image/bmp"),
    "TIFF": ("tiff", "image/tiff"),
}

# 只允许这些 Content-Type 进桶。MinerU 正常情况下只会给图片;
# 如果它给出奇怪格式,宁可在这里拒绝,也不要往桶里塞 application/octet-stream。
_ALLOWED_MIME = frozenset(mime for _ext, mime in _IMAGE_TYPES.values())


def _permission_hint(exc: Exception) -> RuntimeError:
    """输入:OSS 异常。行为:把 403 翻成能照着改的 RAM 权限提示。输出:RuntimeError。

    **不打印 AccessKey/签名**:403 的响应里可能带 RequestId,但访问凭证一个字都不能进日志。
    """
    return RuntimeError(
        "OSS 拒绝访问(403)。当前 AccessKey 至少需要 "
        "oss:PutObject(上传)、oss:GetObject(HEAD/签名 URL 读取);"
        "资源限制到 `<bucket>/<prefix>/*` 即可。"
        f"原始错误:{type(exc).__name__}: {exc}"
    )


def image_key_for(data: bytes, *, prefix: str) -> tuple[str, str]:
    """输入:图片字节 + 对象前缀。行为:按内容哈希生成 key,并按 magic bytes 定扩展名。输出:``(key, content_type)``。

    这是幂等的**唯一**入口:测试直接调它,不必连 OSS。哈希取 SHA-256:
    key 的十六进制是 64 字符,与项目里其它内容哈希口径一致。
    """
    digest = hashlib.sha256(data).hexdigest()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
    except Exception as exc:  # noqa: BLE001 - 不是图片就拒绝,报错里带原因
        raise ValueError(f"不是可识别的图片(无法读取 magic bytes):{exc}") from exc

    try:
        ext, content_type = _IMAGE_TYPES[fmt]
    except KeyError as exc:
        raise ValueError(
            f"不支持的图片格式 {fmt!r};支持 {sorted(_IMAGE_TYPES)}"
        ) from exc

    cleaned = prefix.strip().strip("/")
    if not cleaned:
        raise ValueError("图片对象前缀不能为空")
    # 前两位分桶:一个目录下不要堆几十万个对象;也保留按哈希检索的能力。
    key = f"{cleaned}/{digest[:2]}/{digest}.{ext}"
    return key, content_type


class OssImageStore(ImageStore):
    """OSS 图片存储。``oss2`` 在第一次真正用到时才 import。"""

    def __init__(
        self,
        config: OssConfig,
        *,
        allowed_types: frozenset[str] | None = None,
    ) -> None:
        """输入:OSS 配置 + 可选 MIME 白名单。行为:保存配置并校验当前档位。输出:无。

        **构造期不连网**:图片功能关掉时不会创建这个对象;即使创建了,也不该在
        构造时因为网络抖动拦住整个服务启动。真正的连接在第一次 ``put/sign``。
        """
        if config.url_mode != "signed":
            raise RuntimeError(
                f"oss.url_mode={config.url_mode!r} 尚未实现;当前只支持 signed。"
                "proxy 档需要额外的图片代理路由(按文档 ACL 校验后流式返回),不要静默退回。"
            )
        self._config = config
        self._allowed_types = allowed_types
        self._buckets: dict[str, Any] = {}

    @property
    def config(self) -> OssConfig:
        """输入:无。行为:暴露配置给组合根/测试。输出:配置对象。"""
        return self._config

    def put(self, path: Path) -> str:
        """输入:本地图片文件。行为:内容寻址 + 幂等上传。输出:``image_key``。"""
        data = path.read_bytes()
        if not data:
            raise ValueError(f"图片是空文件:{path}")
        key, content_type = image_key_for(data, prefix=self._config.prefix)
        if content_type not in _ALLOWED_MIME:  # 理论上 image_key_for 已经拦过
            raise ValueError(f"不支持的 Content-Type {content_type}")
        if self._allowed_types is not None and content_type not in self._allowed_types:
            raise ValueError(
                f"图片类型 {content_type} 不在 images.allowed_types 白名单里 "
                f"({sorted(self._allowed_types)});调配置或跳过这张图"
            )

        bucket = self._bucket()
        if self._exists(bucket, key):
            return key

        headers = {
            "Content-Type": content_type,
            # 内容寻址:同一 key 的字节永远不变,可以放心长缓存。
            "Cache-Control": "public, max-age=31536000, immutable",
            # 并发写同一个 key 时只允许一个成功;另一个会收到 409。
            "x-oss-forbid-overwrite": "true",
        }
        self._put_with_retry(bucket, key, data, headers)
        return key

    def sign(self, key: str) -> str:
        """输入:``image_key``。行为:生成短时签名 URL。输出:URL。

        **每次调用都重新签**:签名里带时间戳/过期时间,缓存它等于制造死链。
        """
        cleaned = key.strip().lstrip("/")
        if not cleaned:
            raise ValueError("image_key 不能为空")
        bucket = self._sign_bucket()
        return str(
            bucket.sign_url(
                "GET",
                cleaned,
                self._config.sign_expires_s,
                # key 里有 ``/``;不保留的话签名 URL 会把斜杠编码成 %2F,
                # OSS 校验会失败(或对象名对不上)。
                slash_safe=True,
            )
        )

    def delete(self, key: str) -> None:
        """输入:``image_key``。行为:删除对象;不存在按成功。输出:无。

        **调用方必须先确认引用计数**:内容寻址的 key 可能被多份文档共用。
        """
        cleaned = key.strip().lstrip("/")
        if not cleaned:
            return
        try:
            self._bucket().delete_object(cleaned)
        except Exception as exc:  # noqa: BLE001 - 删除是清理路径,不存在不该炸
            status = getattr(exc, "status", None)
            if status != 404:
                raise

    # ── 内部 ────────────────────────────────────────────────────────────
    def _bucket(self) -> Any:
        """输入:无。行为:构造/复用上传用的 Bucket。输出:oss2.Bucket。"""
        return self._bucket_for("upload", self._config.endpoint)

    def _sign_bucket(self) -> Any:
        """输入:无。行为:构造/复用签名用的 Bucket(优先 public_endpoint)。输出:oss2.Bucket。"""
        endpoint = self._config.public_endpoint or self._config.endpoint
        return self._bucket_for("sign", endpoint)

    def _bucket_for(self, role: str, endpoint: str) -> Any:
        """输入:角色名 + endpoint。行为:按端点缓存 Bucket。输出:oss2.Bucket。"""
        cached = self._buckets.get(role)
        if cached is not None:
            return cached
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - 依赖没装时才会走到
            raise RuntimeError(
                "图片功能需要 oss2:先 `uv add oss2`(或 `uv sync`)再启动;"
                "不想接图片就把 RAG_IMAGES__ENABLED 设成 false"
            ) from exc

        auth = oss2.Auth(
            self._config.accesskey_id.get_secret_value(),
            self._config.accesskey_secret.get_secret_value(),
        )
        bucket = oss2.Bucket(
            auth,
            endpoint,
            self._config.bucket,
            connect_timeout=self._config.connect_timeout_s,
        )
        self._buckets[role] = bucket
        return bucket

    @staticmethod
    def _exists(bucket: Any, key: str) -> bool:
        """输入:Bucket + key。行为:HEAD 判断对象是否存在。输出:布尔。"""
        try:
            return bool(bucket.object_exists(key))
        except Exception as exc:  # noqa: BLE001 - 404 是"不存在",其余照抛
            status = getattr(exc, "status", None)
            if status == 404:
                return False
            if status == 403:
                raise _permission_hint(exc) from exc
            raise

    def _put_with_retry(self, bucket: Any, key: str, data: bytes, headers: dict[str, str]) -> None:
        """输入:Bucket + key + 字节 + 请求头。行为:PUT,409 视为成功,5xx/超时重试。输出:无。"""
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            try:
                bucket.put_object(key, data, headers=headers)
                return
            except Exception as exc:  # noqa: BLE001 - 按 OSS 的 status 分类处理
                status = getattr(exc, "status", None)
                if status == 409:
                    # 另一个 worker 刚写完同一份内容;key 仍然是同一个,按成功处理。
                    _logger.info("图片已存在(并发写入),复用 key=%s", key)
                    return
                if status == 403:
                    raise _permission_hint(exc) from exc
                transient = status is None or status >= 500
                if not transient or attempt + 1 >= attempts:
                    raise
                delay = min(0.5 * (2**attempt), 4.0)
                _logger.warning(
                    "上传图片失败(status=%s),%.1fs 后重试(%d/%d):%s",
                    status,
                    delay,
                    attempt + 1,
                    attempts,
                    exc,
                )
                time.sleep(delay)

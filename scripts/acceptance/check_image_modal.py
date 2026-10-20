#!/usr/bin/env python
"""契约:图片 key 从解析产物一路挂到**父块**,再以临时 URL 出现在引用/模型消息里。

这一层不连 OSS、不跑 MinerU:
* 幂等性只测「同一份字节 → 同一个 key」(真正的 PUT/409 由网关/集成测试覆盖);
* 父块归属用现造切分器,不加载 langchain;
* 签名器用假的,只验证「key 不落库、URL 每次现签」。

跑法:``uv run python scripts/acceptance/check_image_modal.py``
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from _harness import check, report


def _png(color: tuple[int, int, int]) -> bytes:
    """输入:RGB 颜色。行为:造一张 2×2 PNG。输出:PNG 字节。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


@check("图片 key：同一份字节永远同一个 key,不同字节不同 key,扩展名按 magic bytes 定")
def _() -> str:
    from project_rag.adapters.oss_images import image_key_for

    first, content_type = image_key_for(_png((255, 0, 0)), prefix="rag/images")
    again, again_type = image_key_for(_png((255, 0, 0)), prefix="rag/images")
    other, _ = image_key_for(_png((0, 0, 255)), prefix="rag/images")

    if first != again or content_type != again_type:
        raise AssertionError("同一份字节两次得到不同 key —— 幂等的根基没了")
    if first == other:
        raise AssertionError("不同图片得到同一个 key —— 会把两张图合成一张")
    if not first.startswith("rag/images/") or not first.endswith(".png"):
        raise AssertionError(f"key 形状不对:{first!r}")
    if content_type != "image/png":
        raise AssertionError(f"Content-Type 应由字节判断,实际 {content_type!r}")
    # 哈希必须是 64 位十六进制(sha256),且 key 里不出现文件名/时间戳
    digest = Path(first).stem
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise AssertionError(f"key 里的摘要不是 sha256:{digest!r}")
    return f"{first[:24]}… / {content_type}"


@check("OSS 幂等：HEAD 命中不重传；并发 409 视为成功；同一字节始终同一 key")
def _() -> str:
    import tempfile

    from project_rag.adapters.oss_images import OssImageStore
    from project_rag.config import OssConfig

    class _Conflict(Exception):
        status = 409

    class Bucket:
        def __init__(self, *, race: bool = False) -> None:
            self.objects: set[str] = set()
            self.puts = 0
            self.race = race

        def object_exists(self, key: str) -> bool:
            return key in self.objects

        def put_object(self, key, data, headers=None) -> None:  # noqa: ANN001
            if self.race:
                raise _Conflict()
            self.puts += 1
            self.objects.add(key)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "a.png"
        path.write_bytes(_png((1, 2, 3)))
        from pydantic import SecretStr

        store = OssImageStore(
            OssConfig(
                # 字段类型是 SecretStr(见 config.OssConfig):传裸 str 会在装配期就被 pydantic 收下,
                # 但类型检查会红 —— 验收脚本自己也得按契约的类型写。
                accesskey_id=SecretStr("test"),
                accesskey_secret=SecretStr("test"),
                bucket="test-bucket",
                endpoint="https://oss-cn-guangzhou.aliyuncs.com",
            )
        )
        bucket = Bucket()
        store._buckets["upload"] = bucket
        first = store.put(path)
        second = store.put(path)
        if first != second or bucket.puts != 1:
            raise AssertionError(
                f"重复上传应命中 HEAD 且只写一次,实际 puts={bucket.puts}, key 相同={first == second}"
            )
        racing = Bucket(race=True)
        store._buckets["upload"] = racing
        if store.put(path) != first:
            raise AssertionError("并发 409 时应复用同一个 key,而不是报错/换 key")
    return "HEAD 命中 → 0 次 PUT；409 → 视为成功；key 不变"


@check("MinerU 解析：content_list 的图注变成正文、图片锚点落在图注上、页码 1 起")
def _() -> str:
    import tempfile

    from project_rag.adapters.mineru_parser import parse_content_list

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "images").mkdir()
        image_path = root / "images" / "a.png"
        image_path.write_bytes(_png((10, 20, 30)))
        parsed = parse_content_list(
            [
                {"type": "text", "text": "标题", "text_level": 1, "page_idx": 0},
                {
                    "type": "image",
                    "img_path": "images/a.png",
                    "image_caption": ["图一"],
                    "page_idx": 0,
                },
            ],
            output_dir=root,
        )
    if parsed.text != "# 标题\n\n图一":
        raise AssertionError(f"正文拼装不对:{parsed.text!r}")
    if parsed.pages != ((0, 4, 1), (6, 8, 1)):
        raise AssertionError(f"页码区间不对:{parsed.pages}")
    if len(parsed.images) != 1:
        raise AssertionError(f"图片数量不对:{parsed.images}")
    image = parsed.images[0]
    if image.offset != 6 or parsed.text[image.offset : image.offset + 2] != "图一":
        raise AssertionError("图片锚点没有落在图注上")
    if image.page != 1 or image.alt != "图一":
        raise AssertionError(f"图片页码/alt 不对:{image}")
    return "正文含图注；锚点=图注起点；page_idx 0→1"


@check("父块归属：图落在 [parent_start, 下一个 parent_start) 里；不报位置的切分器不挂图")
def _() -> str:
    from project_rag.application.ingest_documents import chunk_documents
    from project_rag.domain import ChunkGroup, Document, DocumentImage

    text = "AAAA\n\nBBBB\n\nCCCC"
    document = Document(
        "d",
        text,
        "s",
        images=(
            DocumentImage("k-a", 0, 1),
            DocumentImage("k-b", 6, 2),
            DocumentImage("k-c", 12, 2),
        ),
    )

    class Chunker:
        # 参数名与端口一致(``text``):协议匹配会检查参数名,``value`` 会让 pyright 报
        # "Parameter name mismatch" —— 运行时无所谓,但类型检查是这一层的契约。
        def split(self, text: str):
            return (
                ChunkGroup("AAAA", ("AAAA",), (0,), parent_start=0),
                ChunkGroup("BBBB", ("BBBB",), (0,), parent_start=6),
                ChunkGroup("CCCC", ("CCCC",), (0,), parent_start=12),
            )

    chunks = chunk_documents([document], Chunker())
    got = {chunk.parent_content: chunk.image_keys for chunk in chunks}
    if got != {"AAAA": ("k-a",), "BBBB": ("k-b",), "CCCC": ("k-c",)}:
        raise AssertionError(f"图片挂错父块:{got}")

    class NoPositionChunker:
        def split(self, text: str):
            return (
                ChunkGroup("AAAA", ("AAAA",), (0,)),
                ChunkGroup("BBBB", ("BBBB",), (0,)),
                ChunkGroup("CCCC", ("CCCC",), (0,)),
            )

    blind = chunk_documents([document], NoPositionChunker())
    if any(chunk.image_keys for chunk in blind):
        raise AssertionError("切分器没报 parent_start 却挂上了图 —— 那是在猜位置")
    return "三张图各归自己的父块；不报位置 → 全部不挂（不猜）"


@check("引用快照：image_keys 随快照走；API 出参把 key 签成 URL，key 本身仍保留")
def _() -> str:
    from project_rag.api.app import _citations
    from project_rag.domain import Chunk, Citation, snapshot_citations

    citation = Citation(
        index=1,
        chunk=Chunk("c1", "正文", "s", "d", image_keys=("k1", "k2")),
        score=0.9,
    )
    snapshot = snapshot_citations([citation])[0]
    if snapshot.image_keys != ("k1", "k2"):
        raise AssertionError(f"快照丢了 image_keys:{snapshot.image_keys}")

    signed = _citations([snapshot], lambda key: f"https://oss.test/{key}?sig=x")
    if [image.key for image in signed[0].images] != ["k1", "k2"]:
        raise AssertionError("出参没带 image key")
    if not all(image.url.startswith("https://oss.test/") for image in signed[0].images):
        raise AssertionError("出参没带签名 URL")

    unsigned = _citations([snapshot], None)
    if [image.url for image in unsigned[0].images] != ["", ""]:
        raise AssertionError("没装配图片功能时 URL 应为空,而不是编一个")
    return "key 进快照、URL 只出现在出参；未装配时 URL 为空"


@check("历史：老行没有 image_keys 读成空元组；坏形状当场报错，不静默丢图")
def _() -> str:
    from project_rag.adapters.conversation_mysql import _load_citations

    old = _load_citations(
        '[{"index":1,"source":"s","doc_id":"d","preview":"p","score":0.5}]'
    )
    if old[0].image_keys != ():
        raise AssertionError("旧历史没有 image_keys 时应该读成空元组")

    bad = '[{"index":1,"source":"s","doc_id":"d","preview":"p","score":0.5,"image_keys":"k1"}]'
    try:
        _load_citations(bad)
    except RuntimeError as exc:
        if "image_keys" not in str(exc):
            raise AssertionError(f"坏数据的报错没点名 image_keys:{exc}") from exc
    else:
        raise AssertionError("image_keys 不是数组时应该报错,不能把字符串拆成 key")
    return "旧行 → 空；坏形状 → RuntimeError 且点名字段"


@check("多模态：只给前 3 张图，提示词里的编号与引用一致；没有图时提示词一字不改")
def _() -> str:
    from project_rag.application.answer_rag import _llm_images
    from project_rag.domain import Chunk, Citation

    citations = [
        Citation(1, Chunk("c1", "t", "s", "d", image_keys=("a", "b")), 0.9),
        Citation(2, Chunk("c2", "t", "s", "d", image_keys=("c", "d")), 0.8),
    ]
    urls, note = _llm_images(citations, lambda key: f"https://oss.test/{key}")
    if urls != ("https://oss.test/a", "https://oss.test/b", "https://oss.test/c"):
        raise AssertionError(f"附给模型的图不是前 3 张:{urls}")
    if note != "[1] 2 张；[2] 1 张":
        raise AssertionError(f"图片说明与引用编号对不上:{note!r}")

    empty_urls, empty_note = _llm_images(citations, None)
    if empty_urls or empty_note:
        raise AssertionError("没有签名器时不该附任何图")
    return "最多 3 张；说明按 [1]/[2] 编号；无签名器 → 纯文本"


def main() -> int:
    return report(
        "图片模态：内容哈希 key / 父块归属 / 引用签名 / 多模态前 3 张",
        hint="真 OSS 上传与 MinerU 解析由集成测试覆盖；这里零网络。",
    )


if __name__ == "__main__":
    sys.exit(main())

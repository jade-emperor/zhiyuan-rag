"""
MinerU ``content_list.json`` → ``ParsedDocument``(正文 + 页码 + 本地图片)。

**为什么不用 MinerU 生成的 ``full.md``**:markdown 里没有可靠的页码/图片偏移,
拿图注回正文 ``find`` 会在重复句子上给出看着合法的错位置。``content_list.json``
是阅读顺序的扁平块,每块带 ``page_idx`` 与 ``img_path`` —— 图片锚点和页码都能在
拼正文时**当场记下来**,不用猜。

本模块不跑 MinerU、不传 OSS、不读配置:输入是 JSON 块 + 输出目录,输出是纯数据。
这样解析规则可以用一份小夹具直接验收,不必装几个 G 的模型。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from project_rag.observability import get_logger
from project_rag.ports import ParsedDocument, SourceImage

_logger = get_logger("adapters.mineru_parser")


def _page_of(block: Mapping[str, Any]) -> int:
    """输入:一个内容块。行为:把 0 起的 page_idx 转成 1 起。输出:页码(至少 1)。"""
    try:
        return max(int(block.get("page_idx", 0)) + 1, 1)
    except (TypeError, ValueError):
        return 1


def _caption(block: Mapping[str, Any], field: str) -> str:
    """输入:内容块 + 字段名。行为:把 MinerU 的字符串/字符串数组图注压成一行。输出:图注文本。"""
    raw = block.get(field)
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, Sequence):
        return " ".join(str(item).strip() for item in raw if str(item).strip()).strip()
    return ""


def _text_block(block: Mapping[str, Any]) -> str:
    """输入:一个 text/equation 块。行为:取正文并按标题层级加 ``#``。输出:文本。

    ``text_level`` 在正文里**通常没有这个键**(不是 0);用 ``.get()`` 判断,
    写成 ``block["text_level"] >= 1`` 会 KeyError。
    """
    text = str(block.get("text") or "").strip()
    if not text:
        return ""
    level = block.get("text_level")
    if level is not None:
        try:
            depth = min(max(int(level), 1), 6)
            text = f"{'#' * depth} {text}"
        except (TypeError, ValueError):
            pass
    return text


def _table_block(block: Mapping[str, Any]) -> str:
    """输入:一个 table 块。行为:图注 + HTML 表格正文。输出:文本。

    HTML 表格不进 markdown 解析器,但它仍然是可检索的正文(数字/列名在里头);
    前端的表格缩略图只认 markdown 管道表,这里不强行转换 —— 转换错了比不转更糟。
    """
    body = str(block.get("table_body") or "").strip()
    if not body:
        return ""
    caption = _caption(block, "table_caption")
    return f"{caption}\n{body}".strip() if caption else body


def _image_block(
    block: Mapping[str, Any], *, output_dir: Path, offset: int
) -> tuple[str, SourceImage | None]:
    """输入:image 块 + 输出目录 + 当前正文偏移。行为:解析本地图片并给出锚点。输出:(正文, 图片或 None)。

    图片**不写 URL 进正文**:正文要进 embedding,URL 是纯噪声;而且签名 URL 会过期。
    正文里只留图注(没有图注就留 ``【图】``),保证纯图片页也能切出父块。
    """
    raw_path = str(block.get("img_path") or "").strip()
    if not raw_path:
        return _caption(block, "image_caption") or "【图】", None

    root = output_dir.resolve()
    local: Path | None = None
    # 不同 MinerU 版本的目录层级不完全一样:有的把 images/ 放在 content_list 同级,
    # 有的放在它的父目录(云端 zip 常见)。两处都找,但都必须落在对应根之内。
    for base in (root, root.parent):
        candidate = (base / raw_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            continue
        if candidate.is_file():
            local = candidate
            break
    if local is None:
        _logger.warning("MinerU 图片文件不存在或越出输出目录,忽略:%s", raw_path)
        return _caption(block, "image_caption") or "【图】", None

    alt = _caption(block, "image_caption") or _caption(block, "image_footnote")
    image = SourceImage(
        path=local,
        offset=offset,
        page=_page_of(block),
        alt=alt,
    )
    return alt or "【图】", image


def parse_content_list(
    blocks: Sequence[Mapping[str, Any]], *, output_dir: Path
) -> ParsedDocument:
    """输入:MinerU content_list 块序列 + 图片所在目录。行为:按阅读顺序拼正文并记录图片锚点。输出:ParsedDocument。

    图片的 ``offset`` 是"它本应出现在正文里的位置"(图片本身被摘掉,不占字符);
    切块时用父块区间判断归属。把图片 markdown 留在正文里会让 URL 进入 embedding,
    也会让签名 URL 过期后正文跟着废掉。
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    images: list[SourceImage] = []
    cursor = 0

    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        kind = str(block.get("type") or "")
        page = _page_of(block)

        if kind == "image":
            # 图片的锚点要落在**它自己的图注/占位符**上,而不是上一段末尾的
            # 分隔符里;否则父块边界正好在这里时,图会被算到前一个父块。
            separator = 2 if parts else 0
            text, image = _image_block(
                block, output_dir=output_dir, offset=cursor + separator
            )
            if image is not None:
                images.append(image)
        elif kind == "table":
            text = _table_block(block)
        elif kind in {"text", "equation", "code", "list"}:
            text = _text_block(block)
        else:
            # 未知类型不猜:宁可少一段,也不要把一个字段名当成正文。
            continue

        text = text.strip()
        if not text:
            continue
        if parts:
            parts.append("\n\n")
            cursor += 2
        spans.append((cursor, cursor + len(text), page))
        parts.append(text)
        cursor += len(text)

    return ParsedDocument(
        text="".join(parts),
        pages=tuple(spans),
        images=tuple(images),
    )


def parse_content_list_file(path: Path, *, output_dir: Path) -> ParsedDocument:
    """输入:``*_content_list.json`` 路径 + 输出目录。行为:读 JSON 并解析。输出:ParsedDocument。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} 不是 content_list 数组(顶层是 {type(raw).__name__})")
    return parse_content_list(raw, output_dir=output_dir)

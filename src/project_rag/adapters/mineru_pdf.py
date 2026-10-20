"""
PDF 源(MinerU 版):``.pdf`` → ``ParsedDocument``(正文 + 页码 + 本地图片)。

**它和 ``pdf_files.PdfFileSource`` 是同一档的两个实现**,不是叠加:
图片模态开启时用它替换掉 PyMuPDF 那一份,否则 MinerU 的图与页码根本拿不到。
关闭图片时仍走 PyMuPDF —— 那条路径更轻,且不依赖常驻服务。

本类只做格式分派,真正的解析/上传在:
* ``adapters/mineru_client.py`` —— HTTP 提交与结果下载;
* ``adapters/mineru_parser.py`` —— content_list → ParsedDocument;
* ``adapters/oss_images.py`` —— 图片上传成 key。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from project_rag.ports import ParsedDocument


class MinerUFileSource:
    """用注入的 ``parse`` 函数把 PDF 变成 ParsedDocument。"""

    suffixes: ClassVar[frozenset[str]] = frozenset({".pdf"})

    def __init__(self, parse: Callable[[Path], ParsedDocument]) -> None:
        """输入:一个 ``Path -> ParsedDocument`` 的解析函数(通常是 MinerUClient.parse)。输出:无。"""
        self._parse = parse

    def read(self, path: Path) -> str:
        """输入:一份 .pdf。行为:解析并只取正文。输出:markdown 文本。"""
        return self.read_with_assets(path).text

    def read_with_assets(self, path: Path) -> ParsedDocument:
        """输入:一份 .pdf。行为:提交给 MinerU 并取回正文/页码/图片。输出:ParsedDocument。"""
        return self._parse(path)

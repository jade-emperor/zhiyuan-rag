"""
端口:一种文件格式 → 带结构的正文。

只依赖 domain + 标准库 —— 加一种格式(HTML / Excel / 扫描件 OCR)只加一个适配器。

**为什么 ``read`` 回的是「正文文本」而不是 ``Document``**:命名是**语料级**的规则,不是格式级的。
``doc_id`` 取相对路径、``source`` 取目录名去掉 ``_data``、按路径排序 —— 这三条对每种格式都一样,
所以只在 ``adapters/document_loader.py`` 里写一处。让每个适配器各自拼 ``Document`` 的话,
「source 去掉 _data」会在三个文件里漂成三个版本(改一处忘两处,而且很难发现)。

**为什么回的是 markdown 而不是纯文本**:父块是按标题层级切的,标题丢了父块就会在任意位置断开
(见 ``adapters/chunker_recursive.py``)。每种格式都有自己的「标题」概念(Word 的 Heading 样式、
PDF 的书签、PPT 的一页),适配器的活儿就是把它翻成 ``#`` / ``##`` 标出来 —— 这件事只有格式层知道。

约定:``read`` 只负责「读懂一份文件」;扩展名过滤、目录遍历、跳过不支持的文件由调用方
(``load_documents``)负责,所以适配器不必再写一遍路径处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Protocol


class DocumentSource(Protocol):
    """把一份文件读成 markdown 正文。"""

    # 这个适配器认识的扩展名(带点、小写)。调用方按它分派 —— 别在适配器内部再判一次,
    # 两处判断迟早不一致(表现是「有的文件谁都不认」,静默漏掉一份文档)。
    suffixes: ClassVar[frozenset[str]]

    def read(self, path: Path) -> str:
        """输入:一份文件的路径(扩展名保证在 ``suffixes`` 里)。行为:抽出正文并标出标题层级。输出:markdown 文本。

        抽不出文字(扫描件 PDF / 空文档)时返回空串 —— 由调用方决定怎么报告,
        适配器不抛「空文档」这种异常:那不是格式错误,是这份文件本来就没有可检索的文字。
        """
        ...

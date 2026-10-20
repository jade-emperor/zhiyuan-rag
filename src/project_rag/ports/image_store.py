"""
端口:图片对象存储(上传 + 临时 URL 签名)。

**为什么 key 由存储层生成,而不是由调用方给**:key 必须由图片**字节**决定,
调用方只要拿到 key 就行。让 MinerU/切块层自己拼 key,迟早会出现"同一张图两个 key"
(文件名不同、大小写不同、重复上传)—— 那正是幂等要防的事。

**为什么返回值是 key 而不是 URL**:URL 会过期(签名)或随 CDN 域名变化,
它不是事实,只是某一次访问的凭证。真正落库、进提示词、进历史的是 key;
URL 在响应/调模型前现签。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ImageStore(Protocol):
    """图片对象存储。"""

    def put(self, path: Path) -> str:
        """输入:本地图片文件。行为:按内容哈希幂等上传。输出:稳定的 ``image_key``。

        约定:

        * 同一份字节无论上传多少次、来自哪份文档,都返回**同一个 key**;
        * key 里只含哈希与前缀,不含文档名/时间戳/随机数;
        * 已存在时不重复写对象;并发写同一个 key 也按成功处理。
        """
        ...

    def sign(self, key: str) -> str:
        """输入:``image_key``。行为:生成一个短时有效的 URL。输出:URL。

        **每次调用都可以不同**:签名带时间戳。调用方不许把它存进库或历史里。
        """
        ...

    def delete(self, key: str) -> None:
        """输入:``image_key``。行为:删除对象(不存在按成功)。输出:无。

        只给清理任务用。内容寻址意味着一个 key 可能被多份文档引用 ——
        删之前必须先确认没有别的引用,否则会把别人的图删掉。
        """
        ...

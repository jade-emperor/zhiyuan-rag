"""
文档治理的领域类型:一份**已入库文档**的登记信息。

**为什么需要它**:向量库里只有 chunk 级的 ``doc_id``(内容哈希),回答不了
"库里有哪些文件、各多少块、什么时候传的、处理成功没有" —— 而那正是前端要显示的东西
(参考图里的卡片:文件名 / 15 chunks / Processed / &lt;时间&gt;)。

**为什么时间用字符串**:``created_at`` 是给人看的登记信息,不是用来算间隔的
(算间隔的地方用 ``time.perf_counter()``,见 observability/latency.py)。
存 ISO-8601 文本省掉一堆时区问题,也让 JSON 序列化零转换。

**状态是闭集**:``pending``(已登记未处理) / ``processing``(正在切块入库) /
``processed``(可检索) / ``failed``(处理失败,带错误原因) / ``deleting``(正在删)。
``deleting`` 是**中间态**:它让"删到一半崩了"留下痕迹(而不是看起来一切正常)。

**``DocState`` 是另一根轴**:上面那个 ``DocStatus`` 是**登记表说它怎么样**,
而 ``DocState`` 是**它现在到底怎么样** —— 清单接口把登记表、磁盘、向量库三处摆在一起后
给的那一个字段(见 ``application/documents.py`` 的 ``document_views``)。
两者只在 ``processed`` 上分岔:非 ``processed`` 的行 ``state`` 原样等于 ``status``
(在途/失败的行,登记表就是真相);``processed`` 的行才可能被现实推翻成下面四种之一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

DocStatus = Literal["pending", "processing", "processed", "failed", "deleting"]

DOC_STATUSES: tuple[DocStatus, ...] = ("pending", "processing", "processed", "failed", "deleting")

# 前端只该看到"能检索"或"在处理/失败"两类;`deleting` 归到不可检索那一类
SEARCHABLE_STATUSES: tuple[DocStatus, ...] = ("processed",)

# 「这份文档现在到底怎么样」—— 清单要显示的那一个字段。**闭集,而且只有这一处定义**:
# 前端的话术表、验收的断言、接口的枚举全跟着它走。
#
# 四个"不一致态"的名字是刻意选的,每个都直接说清**哪一处不对**:
#   not_indexed  登记说 processed,向量库里一块都没有(集合被清过 / 那次入库其实没成);
#   file_missing 磁盘上找不到这个文件(被手工删了 / 上传的原件被清了);
#   unregistered 磁盘上有、登记表没有(后端直接放进去的语料,还没入库);
#   changed      磁盘上的文件变了(与登记时记下的大小不符)—— 库里的内容是旧的。
# 剩下四个就是那四种在途/失败状态**原样**搬过来(pending/processing/failed/deleting):
# 那种行的真相就是登记表 —— 拿"库里没有块"去报 pending,等于自己造一批假问题。
DocState = Literal[
    "ok",
    "changed",
    "not_indexed",
    "file_missing",
    "unregistered",
    "pending",
    "processing",
    "failed",
    "deleting",
]

DOC_STATES: tuple[DocState, ...] = (
    "ok",
    "changed",
    "not_indexed",
    "file_missing",
    "unregistered",
    "pending",
    "processing",
    "failed",
    "deleting",
)

# 「三处一致」的那一个值。别处要判"这份文档没问题"就比它,不要自己再数一遍条件。
STATE_OK: DocState = "ok"

# 「不一致」的那四个 —— 清单的汇总、前端的话术都按这一组走。**在途/失败不算不一致**:
# 它们是"还没干完"或"干砸了",不是"三处对不上"。
STATE_MISMATCHES: tuple[DocState, ...] = ("changed", "not_indexed", "file_missing", "unregistered")

# 「在途与失败」那一组(汇总里合成一档:它们的处理方式都是"等一下 / 看错误")。
STATE_IN_FLIGHT: tuple[DocState, ...] = ("pending", "processing", "failed", "deleting")


@dataclass(frozen=True, slots=True)
class DocRecord:
    """一份文档的登记信息。``chunks`` 是入库成功后的子块数(未处理时是 0)。"""

    document_id: str
    source: str
    filename: str
    status: DocStatus
    chunks: int
    size_bytes: int
    created_at: str
    error: str | None = None
    # 这一份归在哪一级目录下(见 ``Folder``)。空串 = 未分类 —— 它**是**一个合法归属,
    # 不是"缺字段":磁盘上刚放进来、还没登记的文件也只能是未分类(没有行可写)。
    folder: str = ""

    @property
    def searchable(self) -> bool:
        """输入:无。行为:判断**登记表**说这份文档能不能被检索。输出:布尔。

        **这是登记表那一侧的说法,不是现实**:登记表会撒谎(集合被清过、入库其实没成),
        而"现在搜不搜得到"的唯一判据是**向量库里有没有它的块**。清单接口回的就是后者
        (见 ``application/documents.py`` 的 ``document_views``)—— 这里保留原义,
        是因为对账(``reconcile``)要的正是"登记表怎么说"。
        """
        return self.status in SEARCHABLE_STATUSES


# ══ 一级目录 ════════════════════════════════════════════════════════════════
UNFILED: str = ""
"""未分类的目录 ID。空串在接口路径里不好看,所以对外用 ``UNFILED_ID`` 这一个别名。"""

UNFILED_ID: str = "unfiled"
"""未分类在**接口路径**与前端路由里的那一节(``GET /api/v1/folders/unfiled``)。

真实的目录 ID 永远是 ``f_`` 开头的十六进制,不会撞上这个词 —— 这条由 ``new_folder_id``
的形状保证,不是靠调用方小心。
"""

UNFILED_NAME: str = "未分类"

FOLDER_NAME_MAX = 40
"""目录名上限。**只在这一处**:数据库列宽(64)、接口校验、前端 `maxlength` 都跟着它。

留出余量是因为列宽要装得下"改名时把名字塞进 URL"这类临时形态;真正给人看的上限
是 40 —— 再长在卡片上就要折行了,而折行的目录名读起来像两行正文。
"""

FOLDER_DESCRIPTION_MAX = 120


def new_folder_id() -> str:
    """输入:无。行为:生成一个目录 ID。输出:``f_`` + 12 位十六进制。

    前缀不是装饰:生成出来的 ID 恒满足 ``^f_[0-9a-f]{12}$``,于是**在形状上**就不可能
    等于保留 ID ``unfiled`` —— 这条不靠调用方小心,由 ``check_m7_5_folders`` 钉住。
    """
    return f"f_{uuid4().hex[:12]}"


def check_folder_name(raw: str) -> str:
    """输入:用户给的目录名。行为:去掉首尾空白并校验。输出:可入库的名字;不合法则抛 ValueError。

    **为什么名字也要有规矩**:目录名是这个系统里唯一一个"人给、人看、还要当命令行参数"
    的标识 —— ``project-rag ingest --folder 产品资料`` 就是按名字找目录的。
    带首尾空格的名字会让同一次命令在下一次找不到它;带 ``/`` 或换行的名字在任何路径式
    展示里都会错位。所以这些一律当场驳回,而不是"存进去再说"。

    ``未分类`` 是保留名:它与"没有目录"在界面上长得一样,允许存在就会有两件事共用一个名字。
    """
    name = raw.strip()
    if not name:
        raise ValueError("目录名不能为空")
    if len(name) > FOLDER_NAME_MAX:
        raise ValueError(f"目录名最多 {FOLDER_NAME_MAX} 个字符,收到 {len(name)} 个")
    if name == UNFILED_NAME:
        raise ValueError(f"{UNFILED_NAME!r} 是保留名(它表示「没有目录」),换一个")
    bad = [ch for ch in name if ch in "/\\" or ord(ch) < 32]
    if bad:
        raise ValueError(f"目录名里不能有 / \\ 或控制字符,收到 {bad!r}")
    return name


def check_folder_description(raw: str) -> str:
    """输入:用户给的目录描述。行为:去空白、限长。输出:可入库的描述(空串 = 不写)。"""
    text = " ".join(raw.split())
    if len(text) > FOLDER_DESCRIPTION_MAX:
        raise ValueError(f"目录说明最多 {FOLDER_DESCRIPTION_MAX} 个字符,收到 {len(text)} 个")
    return text


@dataclass(frozen=True, slots=True)
class Folder:
    """一级目录:**只有两层**,所以它没有 ``parent_id``。

    ``folder_id`` 一旦生成就不再变(改名不改 ID):它是前端路由 ``#/knowledge/<id>``
    与 ``documents.folder`` 那一列的键,变了就等于把目录里的文档全丢了。
    """

    folder_id: str
    name: str
    description: str
    created_at: str
    updated_at: str

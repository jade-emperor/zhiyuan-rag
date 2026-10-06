"""
各文档适配器共用的文本整理(私有模块,不是端口也不是公共 API)。

PDF 与 Word 都面对同样两件事,只该有一份实现:

* **折断的行要接回去** —— 中文之间不能补空格(「语言 模型」会把词切开),拉丁文之间必须留
  (「the\\nmodel」粘成 themodel 就再也搜不到了),所以按相邻两个字符判;
* **字体垃圾要去掉** —— PDF 里从嵌入字体映射出来的 ``\\x00`` / ``\\x01`` /
  私用区字符(``\\uf8ee`` 这种,本来是公式里的括号)对检索毫无用处,留着只会占块长。
"""

from __future__ import annotations

# markdown 只到 ######,再深的层级按 6 级算(总比把 ####### 当正文强)
MAX_HEADING_LEVEL = 6

_CJK_RANGES = (
    ("\u3000", "\u303f"),  # 中文标点
    ("\u4e00", "\u9fff"),  # 汉字
    ("\uff00", "\uffef"),  # 全角字符
)

# 控制字符(除 \t\n\r)与私用区:PDF 的嵌入字体常把它们当符号位吐出来
_JUNK = frozenset(
    [chr(code) for code in range(0x20) if chr(code) not in "\t\n\r"]
    + [chr(0x7F)]
    + [chr(code) for code in range(0xE000, 0xF900)]
)


def is_cjk(char: str) -> bool:
    """输入:一个字符。行为:判断它是不是中日韩文字或全角标点。输出:是/否。"""
    return any(low <= char <= high for low, high in _CJK_RANGES)


def join_wrapped(text: str) -> str:
    """输入:一段被换行折断的文本。行为:去掉字体垃圾,按语言把折断的行接回去。输出:一行(可能为空)。

    行内多余的空白折成一个空格:PDF 的文字块按坐标切割,常在词中间留下连续空格。
    """
    cleaned = "".join(char for char in text if char not in _JUNK)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    merged = lines[0]
    for line in lines[1:]:
        separator = "" if is_cjk(merged[-1]) and is_cjk(line[0]) else " "
        merged += separator + line
    return " ".join(merged.split())

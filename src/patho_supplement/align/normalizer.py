"""蜡块号与部位描述归一化。

**为什么需要这一层**：病理报告基线（病理系统 ``GET`` 回来）与加做结果回报
（``ORU^R01`` 落盘）里，同一块蜡的写法常常不一样 —— 基线写 ``B-3``，回报写
``B3``；基线写 ``b03``，回报写 ``B3``；部位写「子宫体」还是「子宫 体」也不
统一。字面不等就判「对不上」，结果是**明明是同一块，却挂起等人补**；更糟的
是字面碰巧相等就绑上去 —— 绑错块写进已签发的补充报告，在病理科是事故。

所以对齐之前先把两边收拾成同一把尺子：

- **蜡块号**：去分隔符、全角转半角、去空白、统一大写、去掉编号里的前导零，
  于是 ``B3`` ≡ ``B-3`` ≡ ``B03`` ≡ ``b3`` ≡ ``Ｂ３``；
- **部位描述**：去空白与标点、全角转半角、去掉看不见的格式字符（零宽空格等）、
  方位词归并（「左侧」→「左」、「右侧」→「右」），于是「子宫体（内侧）」≡
  「子宫体内侧」。

**三条硬约束**：

- **幂等**：``normalize(normalize(x)) == normalize(x)``。归一化会被反复调用
  （入站归一化一次、对齐时再归一化一次），不幂等就会出现「第一次对得上、
  第二次对不上」的诡异现象 —— 这类问题在报告里极难排查；
- **不猜**：输入里既没有字母也没有数字时返回 ``None``（这个字段等于没填），
  而不是把标点当成值；
- **空值不相等**：两侧都缺蜡块号**不代表**它们是同一块。缺字段必须走挂起
  分支，不能参与相等比较，否则「两个都没有」会被当成「两个相同」。

对应源产品：源项目的检索后端是 embedding 相似度检索
（``github_ref/haystack/haystack/components/retrievers/text_embedding_retriever.py``、
``.../retrievers/in_memory/embedding_retriever.py``），相似度给不出「这条就是
B3 块」这种确定性绑定 —— 0.87 分既可能是 B3 也可能是 B5。本模块是替换掉向量
后端的第一层：先把写法收敛成可比的键，再由词法打分与四级匹配判定归属。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "normalize_block_no",
    "normalize_region_desc",
    "same_block_no",
    "same_region_desc",
]

#: 全角 ASCII 可打印区（！～ 与 ０～９ 的全角写法）起止码位。
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
#: 全角 → 半角的固定偏移量。
_FULLWIDTH_OFFSET = 0xFEE0
#: 表意空格（全角空格）码位。
_IDEOGRAPHIC_SPACE = 0x3000

#: 空白（含表意空格与换行）。
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
#: 归一化后仍算「有值」的字符：ASCII 字母或数字。
_ALNUM_RE = re.compile(r"[0-9A-Z]")
#: 编号里的数字段（前缀 / 数字 / 其余）。
_DIGITS_LEAD_RE = re.compile(r"^([^0-9]*)([0-9]+)(.*)$")
#: 蜡块号里属于符号类、不是标点类的分隔符。
_BLOCK_EXTRA_SEP_RE = re.compile(r"[~*+=#№]")

#: 蜡块号尾部可能带的单位词 —— **长的先剥**，否则「B3蜡块」会被剥成「B3蜡」。
_BLOCK_SUFFIXES: tuple[str, ...] = ("蜡块", "块", "号")

#: 方位词归并表：长写法收敛到短写法。
_DIRECTION_WORDS: tuple[tuple[str, str], ...] = (("左侧", "左"), ("右侧", "右"))


def _to_half_width(text: str) -> str:
    """全角转半角（含表意空格）。

    只做 **ASCII 可打印区** 的转换：全角字母数字与全角标点各减一个固定偏移
    量即可落回半角。这里不用 ``unicodedata.normalize("NFKC", ...)`` —— NFKC 会
    顺手把「㍿」「①」一并改写，而那些字符在病理报告原文中可能是有意义的内容，
    改写掉就再也回不去了。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == _IDEOGRAPHIC_SPACE:
            out.append(" ")
        elif _FULLWIDTH_START <= code <= _FULLWIDTH_END:
            out.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def _strip_whitespace(text: str) -> str:
    """去掉所有空白（含内部空白与表意空格）。"""
    return _WHITESPACE_RE.sub("", text)


def _strip_invisible(text: str) -> str:
    """去掉**看不见的字符**（Unicode 类别 ``Cf``）。

    从 Word 里复制粘贴的部位描述常带零宽空格（U+200B）、零宽不连字（U+200C）、
    字节序标记（U+FEFF）、软连字符（U+00AD）—— 它们在屏幕上什么也看不见，
    却让「子宫体」和「子宫体」变成两个不同的键，部位因此对不上。
    肉眼看不见的东西不该影响归属判定，所以这一类字符直接去掉。
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _strip_punctuation(text: str) -> str:
    """去掉所有标点（Unicode 标点类 ``P*``）。

    连字符、斜杠、句点、括号、顿号、书名号、引号都在这一类里 —— 蜡块号里的
    「B-3」与部位描述里的「子宫体（内侧）」都要靠这一步收敛成同一个键。
    """
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))


def _strip_leading_zeros(text: str) -> str:
    """去掉编号里的前导零（``B03`` → ``B3``、``003`` → ``3``）。

    只处理**第一段**数字：``B03A02`` → ``B3A02``。全零的数字段保留一位
    （``B00`` → ``B0``），免得把编号本身抹成空串。
    """
    matched = _DIGITS_LEAD_RE.match(text)
    if matched is None:
        return text
    prefix, digits, rest = matched.groups()
    return prefix + (digits.lstrip("0") or "0") + rest


def _strip_block_suffix(text: str) -> str:
    """剥掉蜡块号尾部的单位词（``B3号`` → ``B3``）。

    只有一个字的尾缀不算（``号`` 本身就是没填），否则「号」会被剥成空串。
    """
    for suffix in _BLOCK_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _merge_direction(text: str) -> str:
    """方位词归并：``左侧`` → ``左``、``右侧`` → ``右``。

    只并**左/右**这两个方位词。``左叶``、``左附件``里的「左」是部位名称的一部分，
    不能动；``左侧壁`` → ``左壁`` 则是同一块的不同写法，要并。
    """
    for source, target in _DIRECTION_WORDS:
        text = text.replace(source, target)
    return text


def normalize_block_no(value: str | None) -> str | None:
    """归一化蜡块号。

    ``B3`` / ``B-3`` / ``B03`` / ``b3`` / ``Ｂ３`` / `` B - 3 `` 全部落到同一个值
    ``"B3"``。

    返回 ``None`` 的两种情况：输入为空；或去掉分隔符后**既没有字母也没有数字**
    （纯标点、纯符号）。这两种都表示「这个字段等于没填」—— 空值必须走挂起
    分支，不能参与相等比较，否则两个空值会被当成同一块。

    本函数不抛异常：任何输入都返回一个确定结果（字符串或 ``None``）。
    """
    if value is None:
        return None
    text = _to_half_width(str(value))
    text = _strip_invisible(text)
    text = _strip_whitespace(text)
    text = _strip_punctuation(text)
    text = _BLOCK_EXTRA_SEP_RE.sub("", text)
    text = _strip_block_suffix(text)
    text = text.upper()
    if not text or not _ALNUM_RE.search(text):
        return None
    return _strip_leading_zeros(text)


def normalize_region_desc(value: str | None) -> str | None:
    """归一化取材部位描述。

    去掉空白与标点、全角转半角，方位词归并。于是「子宫体（内侧）」、
    「子宫 体 内侧」、「子宫体内侧」归到同一个值。

    返回 ``None`` 表示这个字段等于没填（空串或只有标点）。本函数不抛异常。

    **超长描述不截断**：院里有人把整段镜下描述粘进部位栏（几百字），本函数
    原样保留 —— 截断可能把关键部位名切掉，反而更糟；打分器那边用长度归一
    （BM25 的 ``b`` 参数）消化超长文本。
    """
    if value is None:
        return None
    text = _to_half_width(str(value))
    text = _strip_invisible(text)
    text = _strip_whitespace(text)
    text = _strip_punctuation(text)
    text = _merge_direction(text)
    return text or None


def same_block_no(left: str | None, right: str | None) -> bool:
    """两个蜡块号归一化后是否指向同一块。

    任一侧归一化后为空即判 **不等** —— 两边都缺蜡块号不代表它们是同一块，
    更不代表可以互相替代。宁可判「对不上」走挂起，也不要把两个空值当成对上。
    """
    left_key = normalize_block_no(left)
    right_key = normalize_block_no(right)
    if left_key is None or right_key is None:
        return False
    return left_key == right_key


def same_region_desc(left: str | None, right: str | None) -> bool:
    """两个部位描述归一化后是否指向同一部位。

    与 :func:`same_block_no` 同一口径：空值不相等。
    """
    left_key = normalize_region_desc(left)
    right_key = normalize_region_desc(right)
    if left_key is None or right_key is None:
        return False
    return left_key == right_key

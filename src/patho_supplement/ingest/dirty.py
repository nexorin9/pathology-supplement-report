"""真实数据形态加固：混编解码、脏行识别。

院内回报文件不是干净的 UTF-8 文本，也不是格式严整的 HL7 报文。联调时把人卡住的
通常是下面两件事，本模块就把它们变成**确定的行为**而不是随机崩溃：

**一、编码混编。** 同一批文件里 UTF-8 与 GBK 都有；更麻烦的是同一个文件里两种
都有 —— 不同系统各写各的，导出时被拼到一起。整文件按一种编码解码，另一部分就
成了乱码；乱码进草稿 = 部位写错 = 一份签出去却写错部位的补充报告。

做法是**逐行解码**：每行先试 UTF-8，再试 GBK，哪条解得开就用哪条。纯 ASCII 行
两种编码结果一致，不影响判断。两种都解不开才算坏文件，报错时带上**行号**。

**二、脏行（多了一个字段分隔符）。** 某一行多写了一个 ``|``，后面的字段整体
右移一位：部位序号那一列变成了「子宫体」，蜡块号变成了空。这种行不是「看不懂」，
是「看懂了但错位」—— 静默放过它，结果就会绑到错的块上。

识别办法很朴素但有效：**某个字段校验不通过，而把它右移一位就能通过** —— 那就
是多了一个分隔符。这类行单独报一个错误码（``dirty_extra_separator``），并指明
是哪个字段、哪一行，让技术人员去改源系统，而不是让本产品去猜。

**不猜，是这一层唯一的原则**：看不懂就报错并说清是哪一行哪个字段；看得懂但
错位 likewise。绝不让「半懂」的字段悄悄进草稿。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .er7 import Er7ParseError, Segment

__all__ = [
    "DecodedPayload",
    "DIRTY_ERROR_CODE",
    "decode_line",
    "decode_payload",
    "looks_shifted",
    "shifted_field",
]

#: 脏行（多了一个字段分隔符）的错误码。
DIRTY_ERROR_CODE = "dirty_extra_separator"

#: 逐行解码时，单行最多允许多少字节 —— 超过即视为坏行，不无限吃内存。
_MAX_LINE_BYTES = 1 << 20


@dataclass(frozen=True)
class DecodedPayload:
    """一份报文文件的解码结果。

    - ``text``：解码后的文本；
    - ``encoding``：本份文件的解码口径 —— ``utf-8``（全程 UTF-8）、``gbk``
      （含中文的行都要靠 GBK 回退才解得开）、``mixed``（两种编码的行混在一起）；
    - ``fallback``：是否用过 GBK 回退；
    - ``line_encodings``：逐行用了哪种编码（行号从 1 起），混编时一眼看得出
      哪几行不是同一种编码。
    """

    text: str
    encoding: str
    fallback: bool = False
    byte_length: int = 0
    line_encodings: tuple[tuple[int, str], ...] = ()

    def summary(self) -> dict[str, object]:
        """可打印的解码摘要（写进运行摘要与审计明细）。"""
        return {
            "encoding": self.encoding,
            "fallback": self.fallback,
            "bytes": self.byte_length,
            "lines": len(self.line_encodings),
        }


def _split_lines(raw: bytes) -> list[bytes]:
    """按行切开字节流（``\\r\\n`` 与 ``\\r`` 都算行尾）。"""
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")


def decode_line(raw_line: bytes, line_no: int) -> tuple[str, str]:
    """解码一行，返回 ``(文本, 编码)``。

    先试 UTF-8，再试 GBK；两种都解不开就报结构化错误，并带上行号 ——
    「第几行坏了」是运维要的第一条信息。
    """
    if len(raw_line) > _MAX_LINE_BYTES:
        raise Er7ParseError(
            f"单行长度 {len(raw_line)} 字节超过上限 {_MAX_LINE_BYTES}，疑似文件损坏",
            line_no=line_no,
            code="bad_encoding",
        )
    try:
        return raw_line.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw_line.decode("gbk"), "gbk"
    except UnicodeDecodeError as exc:
        raise Er7ParseError(
            f"该行既不是 UTF-8 也不是 GBK 编码：{exc}",
            line_no=line_no,
            code="bad_encoding",
        ) from exc


def decode_payload(raw: bytes) -> DecodedPayload:
    """逐行解码一份报文文件。

    整份文件先试一次 UTF-8 —— 绝大多数文件都是，解得开就不必逐行折腾；解不开
    才逐行走「UTF-8 → GBK」的回退，顺带把每一行用的编码记下来。

    返回的 ``encoding``：

    - ``utf-8``：整份按 UTF-8 解开；
    - ``gbk``：含中文的行都要靠 GBK 才解得开（纯 ASCII 行两种编码一致，不计）；
    - ``mixed``：文件里两种编码的行混在一起（导出自不同系统的典型形态）。
    """
    raw_lines = _split_lines(raw)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None:
        return DecodedPayload(
            text=text,
            encoding="utf-8",
            fallback=False,
            byte_length=len(raw),
            line_encodings=tuple(
                (line_no, "utf-8") for line_no in range(1, len(raw_lines) + 1)
            ),
        )

    lines: list[str] = []
    line_encodings: list[tuple[int, str]] = []
    chinese_line_encodings: set[str] = set()
    for line_no, raw_line in enumerate(raw_lines, start=1):
        decoded, encoding = decode_line(raw_line, line_no)
        lines.append(decoded)
        line_encodings.append((line_no, encoding))
        # 只有含中文（多字节）的行才参与「这份文件到底是哪种编码」的判断：
        # 纯 ASCII 行两种编码解出来一模一样，不该把文件说成混编。
        if encoding == "gbk" or any(ord(ch) > 127 for ch in decoded):
            chinese_line_encodings.add(encoding)

    if not chinese_line_encodings:
        encoding_name = "utf-8"
    elif chinese_line_encodings == {"gbk"}:
        encoding_name = "gbk"
    elif chinese_line_encodings == {"utf-8"}:
        encoding_name = "utf-8"
    else:
        encoding_name = "mixed"

    return DecodedPayload(
        text="\n".join(lines),
        encoding=encoding_name,
        fallback="gbk" in chinese_line_encodings,
        byte_length=len(raw),
        line_encodings=tuple(line_encodings),
    )


def shifted_field(segment: Segment, index: int) -> str:
    """取「右移一位」之后的字段值。

    多了一个分隔符时，本该在第 ``index`` 位的值会跑到第 ``index + 1`` 位。
    """
    return segment.field(index + 1)


def looks_shifted(
    segment: Segment, index: int, validator: Callable[[str], bool]
) -> bool:
    """判断这一行是不是多写了一个字段分隔符。

    判据：**把第 ``index`` 字段右移一位后，``validator`` 就成立** —— 那说明
    发送方多写了一个分隔符，后面的字段整体错位了。

    ``validator`` 收到的是右移后的字段值。校验不通过时返回 ``False``（不是
    脏行，只是这一位本身就不合法）。
    """
    shifted = shifted_field(segment, index).strip()
    if not shifted:
        return False
    try:
        return bool(validator(shifted))
    except Exception:  # noqa: BLE001 —— 校验器自身出错不算脏行
        return False


def is_integer_text(value: str) -> bool:
    """文本是否是整数（带正负号也算）。"""
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def describe_shift(segment: Segment, index: int) -> str:
    """描述「右移一位后读到了什么」，用于错误信息。"""
    shifted = shifted_field(segment, index)
    return f"第 {index + 1} 位读到的值是 {shifted!r}"

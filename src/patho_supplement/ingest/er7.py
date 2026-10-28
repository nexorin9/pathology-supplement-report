"""ER7 报文切分与段解析。

HL7 v2 的 ER7 编码把一条报文写成若干行段，每行段用**字段分隔符**（默认 ``|``）
隔成字段，字段内再用**组件分隔符**（默认 ``^``）拆成组件。分隔符由报文头的
``MSH-2`` 声明 —— 不同医院的 LIS 可能用别的符号，因此这里一律从报文里读，不写死。

解析原则：**看不懂就报错，不静默跳过**。任何一处段结构不对，抛
:class:`Er7ParseError`，错误里带段名、字段名与行号，运维照着就能定位到是哪个
文件、哪一行、哪个字段出了问题。静默跳过等于把坏数据悄悄塞进补充报告，
那比报错糟糕得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Er7ParseError",
    "Message",
    "PARSE_ERROR_REASONS",
    "Segment",
    "parse_message",
    "reason_text",
    "split_unescaped",
]

#: 段名：三个字符，首位大写字母，后两位大写字母或数字（ZBR 之类自定义段也算）。
_SEGMENT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]{2}$")

#: 找不到 ``MSH-2`` 时使用的默认编码字符（^~\&）。
_DEFAULT_ENCODING_CHARS = "^~\\&"

#: 报文结构错误码 → 中文原因。拒收清单与审计流水共用这一份口径：
#: 同一个错码在任何一处都读得出同一句话，不必各写各的。
PARSE_ERROR_REASONS: dict[str, str] = {
    "empty_message": "报文为空",
    "not_msh": "报文首段不是 MSH",
    "missing_field_sep": "MSH 段缺少字段分隔符",
    "invalid_segment_name": "段名不合法",
    "missing_message_type": "缺 MSH-9 报文类型，无法分流",
    "missing_version": "缺 MSH-12 版本号",
    "bad_encoding": "报文既不是 UTF-8 也不是 GBK 编码",
    "missing_accession": "缺 OBR-2 病理号，无法归属报告",
    "missing_item_code": "缺 OBX-3 结果项目码",
    "illegal_result_status": "OBX-11 结果状态取值非法",
    "orphan_obx": "OBX 段没有对应的 OBR 段",
    "orphan_zbr": "ZBR 段没有对应的 OBR 段",
    "invalid_region_seq": "ZBR-3 部位序号不是整数",
    "dirty_extra_separator": "该行多了一个字段分隔符，后面的字段整体错位",
    "not_oru": "不是 ORU^R01 报文",
    "not_orm": "不是 ORM^O01 报文",
}


def reason_text(code: str) -> str:
    """按错误码取中文原因；未知错误码原样返回，不猜。"""
    return PARSE_ERROR_REASONS.get(code, code)


class Er7ParseError(ValueError):
    """报文不可解析。

    带上**段名、字段名与行号**，运维能直接定位到文件里的哪一行、哪个字段出了
    事；``to_dict`` 便于落审计。``code`` 是机器可读的错误码 —— 统计「几种坏法」
    靠它，不靠读中文。
    """

    def __init__(
        self,
        detail: str,
        *,
        segment: str | None = None,
        line_no: int | None = None,
        source: str | None = None,
        code: str = "malformed",
        field: str | None = None,
    ) -> None:
        self.detail = detail
        self.segment = segment
        self.line_no = line_no
        self.source = source
        self.code = code
        self.field = field
        where = " / ".join(
            part
            for part in (
                source,
                f"第 {line_no} 行" if line_no is not None else None,
                f"段 {segment}" if segment else None,
                f"字段 {field}" if field else None,
            )
            if part
        )
        super().__init__(f"{where}：{detail}" if where else detail)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "reason": reason_text(self.code),
            "detail": self.detail,
            "segment": self.segment,
            "field": self.field,
            "line_no": self.line_no,
            "source": self.source,
        }


def split_unescaped(text: str, separator: str, escape: str) -> list[str]:
    """按分隔符切分，被转义的分隔符不参与切分。

    HL7 的转义符（默认 ``\\``）可以让分隔符出现在字段内容里；不处理转义就会把
    一个字段错切成两个。
    """
    if not separator:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if escape and char == escape and index + 1 < length:
            buf.append(char)
            buf.append(text[index + 1])
            index += 2
            continue
        if char == separator:
            parts.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return parts


@dataclass(frozen=True)
class Segment:
    """一个行段。

    ``parts`` 是按字段分隔符切开的原始结果：``parts[0]`` 是段名。对 ``MSH``
    段有特殊约定 —— ``MSH-1`` 就是字段分隔符本身，``MSH-2`` 是编码字符，
    所以 ``MSH`` 的字段号与数组下标错开一位；:meth:`field` 已经处理了这件事。
    """

    name: str
    parts: tuple[str, ...]
    line_no: int
    field_sep: str = "|"
    encoding_chars: str = _DEFAULT_ENCODING_CHARS

    # ------------------------------------------------------------ 编码字符

    @property
    def component_sep(self) -> str:
        return self.encoding_chars[0] if len(self.encoding_chars) > 0 else "^"

    @property
    def repetition_sep(self) -> str:
        return self.encoding_chars[1] if len(self.encoding_chars) > 1 else "~"

    @property
    def escape_char(self) -> str:
        return self.encoding_chars[2] if len(self.encoding_chars) > 2 else "\\"

    @property
    def subcomponent_sep(self) -> str:
        return self.encoding_chars[3] if len(self.encoding_chars) > 3 else "&"

    # ------------------------------------------------------------ 取值

    def field(self, index: int) -> str:
        """取第 ``index`` 个字段（从 1 起）。越界返回空串。"""
        if index < 1:
            raise ValueError("字段序号从 1 起")
        if self.name == "MSH":
            if index == 1:
                return self.field_sep
            if index == 2:
                return self.parts[1] if len(self.parts) > 1 else ""
            offset = index - 1
        else:
            offset = index
        if offset < len(self.parts):
            return self.parts[offset]
        return ""

    def component(self, index: int, comp: int = 1) -> str:
        """取字段的某个组件（从 1 起）。"""
        raw = self.field(index)
        parts = split_unescaped(raw, self.component_sep, self.escape_char)
        if comp < 1 or comp > len(parts):
            return ""
        return parts[comp - 1]


@dataclass(frozen=True)
class Message:
    """一条解析完成的报文。

    ``encoding`` 记着这份报文是按哪种编码解出来的（``utf-8`` / ``gbk`` /
    ``mixed``）—— 同一批文件里编码不一致是常事，事后复盘时必须知道这份报文
    当初是怎么读进来的。
    """

    segments: tuple[Segment, ...]
    field_sep: str
    encoding_chars: str
    source: str | None = None
    encoding: str = "utf-8"

    @property
    def msh(self) -> Segment:
        return self.segments[0]

    def field(self, index: int) -> str:
        """取报文头字段（等价于 ``MSH-n``）。"""
        return self.msh.field(index)

    @property
    def message_type(self) -> str:
        """``MSH-9`` 原值，例如 ``ORU^R01``。"""
        return self.field(9)

    @property
    def message_code(self) -> str:
        """``MSH-9`` 的消息码，例如 ``ORU``。"""
        return self.msh.component(9, 1)

    @property
    def message_structure(self) -> str:
        """``MSH-9`` 的消息结构，例如 ``R01``。

        HL7 v2 里 ``MSH-9`` 是「消息码 ^ 触发事件 ^ 消息结构」。结果类报文只写
        两段（``ORU^R01``），此时触发事件即消息结构 —— 这里做兜底，免得把
        正常的报文判成未知类型。
        """
        structure = self.msh.component(9, 3)
        if structure:
            return structure
        return self.msh.component(9, 2)

    @property
    def trigger_event(self) -> str:
        """``MSH-9`` 的触发事件，例如 ``R01``。"""
        return self.msh.component(9, 2)

    @property
    def version(self) -> str:
        """``MSH-12`` 版本号。"""
        return self.field(12)

    @property
    def sending_application(self) -> str:
        """``MSH-3`` 发送方应用，作为结果来源系统的默认值。"""
        return self.field(3)

    def segments_named(self, name: str) -> tuple[Segment, ...]:
        return tuple(seg for seg in self.segments if seg.name == name)


def parse_message(
    text: str, *, source: str | None = None, encoding: str = "utf-8"
) -> Message:
    """把一段 ER7 文本解析成 :class:`Message`。

    报文的段分隔符、字段分隔符、编码字符一律从报文自身读取；
    缺 ``MSH-9``（无法分流）或缺 ``MSH-12``（无版本）视为报文不完整，直接报错。

    ``encoding`` 只是把「这份文本是怎么解出来的」记在报文上，随审计流水留痕。
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [
        (line_no, line.strip())
        for line_no, line in enumerate(raw_lines, start=1)
        if line.strip()
    ]
    if not lines:
        raise Er7ParseError("报文为空", source=source, code="empty_message")

    first_no, first_line = lines[0]
    if not first_line.startswith("MSH"):
        raise Er7ParseError(
            "报文首段必须是 MSH", line_no=first_no, source=source, code="not_msh"
        )
    if len(first_line) < 4:
        raise Er7ParseError(
            "MSH 段缺少字段分隔符",
            segment="MSH",
            line_no=first_no,
            source=source,
            code="missing_field_sep",
        )

    field_sep = first_line[3]
    remainder = first_line[4:]
    encoding_chars = remainder.split(field_sep)[0] if remainder else ""
    if not encoding_chars:
        encoding_chars = _DEFAULT_ENCODING_CHARS

    segments: list[Segment] = []
    for line_no, line in lines:
        name = line[:3]
        if not _SEGMENT_NAME_RE.match(name):
            raise Er7ParseError(
                f"段名不合法：{line[:12]!r}",
                line_no=line_no,
                source=source,
                code="invalid_segment_name",
            )
        segments.append(
            Segment(
                name=name,
                parts=tuple(line.split(field_sep)),
                line_no=line_no,
                field_sep=field_sep,
                encoding_chars=encoding_chars,
            )
        )

    msh = segments[0]
    if not msh.field(9):
        raise Er7ParseError(
            "缺 MSH-9 报文类型，无法分流",
            segment="MSH",
            line_no=msh.line_no,
            source=source,
            code="missing_message_type",
        )
    if not msh.field(12):
        raise Er7ParseError(
            "缺 MSH-12 版本号",
            segment="MSH",
            line_no=msh.line_no,
            source=source,
            code="missing_version",
        )

    return Message(
        segments=tuple(segments),
        field_sep=field_sep,
        encoding_chars=encoding_chars,
        source=source,
        encoding=encoding,
    )

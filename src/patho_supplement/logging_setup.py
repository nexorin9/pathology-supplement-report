"""结构化行日志 + 每轮运行摘要。

守护进程是常驻的 —— 运维半夜被叫醒时，靠日志就能答出四个问题：

    这一轮处理了多少条？对上了多少条？挂了多少条？写回去成功了几份？

所以日志不是给人读的一段散文，而是**一行一件事**的结构化记录::

    2026-10-28 12:34:56 | INFO  | event=round_summary processed=7 aligned=5 pending=1 writeback_ok=1 writeback_failed=0 elapsed_seconds=0.41

字段是 ``键=值``；值里带空格就加引号。采集侧（``grep`` / ``awk`` / 日志平台）
按空格切就能取到字段，不必写正则去解析中文散文。

三条硬规矩：

1. **结果值默认不入日志。** 病理判读结果（阳性 / 阴性 / 约 15%）是业务数据，
   日志往往要往外发给信息科看，默认遮掉；确实要查时用环境变量显式打开。
2. **患者可识别字段整条丢掉。** 姓名、身份证、联系方式这些，**连字段名都不该
   出现在日志里** —— 把值遮掉却留下「这里有个姓名」本身也是泄露。
3. **每轮一行摘要。** 处理条数、对齐条数、挂起条数、写回成功 / 失败数、耗时
   都进摘要行，运维不必翻上下文就知道这一轮正不正常。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, TextIO

from .privacy import desensitize_text, is_sensitive_field

__all__ = [
    "LEVELS",
    "LOGGER_NAME",
    "PLACEHOLDER_RESULT_VALUE",
    "RESULT_VALUE_FIELDS",
    "RoundSummary",
    "StructuredLineFormatter",
    "StructuredLogger",
    "build_round_summary",
    "format_line",
    "format_value",
    "redact_fields",
    "setup_logging",
]

#: 日志器名字。所有本产品的日志都挂在这棵树下，采集侧按前缀过滤即可。
LOGGER_NAME = "patho_supplement"

#: 日志级别（名字 → ``logging`` 里的数值）。
LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

#: 结果值字段：装的是病理判读结果，默认遮蔽。
RESULT_VALUE_FIELDS: frozenset[str] = frozenset(
    {
        "value",
        "values",
        "result",
        "resultvalue",
        "resulttext",
        "结果",
        "结果值",
        "观察结果",
        "判读",
        "结论",
    }
)

#: 结果值遮蔽后的占位符（保留「这里原本有东西」的事实）。
PLACEHOLDER_RESULT_VALUE = "【结果值已隐去】"

#: 挂在日志器上的 handler 标记：重复装日志时只摘自己挂的，不动别人的。
_MANAGED_FLAG = "_patho_supplement_managed"


def _normalize(name: str) -> str:
    """字段名归一：去空白与下划线连字符、转小写。"""
    return re.sub(r"[\s_\-]+", "", str(name or "")).lower()


def _is_result_value_field(name: str) -> bool:
    """这个字段装的是不是结果值。"""
    normalized = _normalize(name)
    if not normalized:
        return False
    if normalized in RESULT_VALUE_FIELDS:
        return True
    # ``base_value``、``观察值`` 这类后缀形态一并算结果值。
    return normalized.endswith("value") or normalized.endswith("值")


def mask_result_value(value: Any = None) -> str:
    """把结果值遮成占位符。"""
    return PLACEHOLDER_RESULT_VALUE


def redact_fields(
    fields: Mapping[str, Any], *, include_result_values: bool = False
) -> dict[str, Any]:
    """日志字段脱敏。

    - 患者可识别字段（姓名 / 身份证 / 联系方式…）：**整条丢掉**；
    - 结果值字段：默认遮成占位符（``include_result_values=True`` 才放行）；
    - 其余字符串：过一遍文本遮蔽（顺手挡掉混进来的身份证号、手机号）。

    脱敏是幂等的 —— 同一份字段遮两遍，结果一样。
    """
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if is_sensitive_field(key):
            continue
        if _is_result_value_field(key) and not include_result_values:
            result[key] = mask_result_value(value)
            continue
        if value is None or isinstance(value, (int, float, bool)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = desensitize_text(value)
        else:
            result[key] = desensitize_text(
                json.dumps(value, ensure_ascii=False, default=str)
            )
    return result


def format_value(value: Any) -> str:
    """把一个字段值写成日志里的一枚 token。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, int):
        return str(value)
    text = "" if value is None else str(value)
    if text == "":
        return '""'
    if any(char.isspace() for char in text) or '"' in text or "|" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def format_line(event: str, fields: Mapping[str, Any]) -> str:
    """拼一行结构化日志（``event=... 键=值 键=值``）。

    字段顺序按传入顺序 —— 同一类事件每次的字段顺序一致，采集侧切分才稳。
    """
    parts = [f"event={format_value(event)}"]
    for key, value in fields.items():
        parts.append(f"{key}={format_value(value)}")
    return " ".join(parts)


@dataclass
class RoundSummary:
    """一轮的运行摘要。

    五个口径是固定的（处理 / 对齐 / 挂起 / 写回成功 / 写回失败），另外加一个
    耗时；其余计数放 ``extra``，不挤占这五个主口径。
    """

    #: 本轮处理（入账）的结果条数。
    processed: int = 0
    #: 本轮对齐上、并入草稿的条数。
    aligned: int = 0
    #: 本轮挂起、等人补齐的条数。
    pending: int = 0
    #: 本轮回写成功（草稿写回病理系统）的份数。
    writeback_ok: int = 0
    #: 本轮回写失败的份数。
    writeback_failed: int = 0
    #: 本轮耗时（秒）。
    elapsed_seconds: float = 0.0
    #: 附加计数（文件数、重复投递、拒收、冲突、通知……）。
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """可序列化的视图（落盘 / 接口回传）。"""
        data: dict[str, Any] = {
            "processed": self.processed,
            "aligned": self.aligned,
            "pending": self.pending,
            "writeback_ok": self.writeback_ok,
            "writeback_failed": self.writeback_failed,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }
        data.update(self.extra)
        return data

    def as_fields(self) -> dict[str, Any]:
        """日志字段（键名固定，采集侧按名取值）。"""
        fields: dict[str, Any] = {
            "processed": self.processed,
            "aligned": self.aligned,
            "pending": self.pending,
            "writeback_ok": self.writeback_ok,
            "writeback_failed": self.writeback_failed,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }
        fields.update(self.extra)
        return fields

    def summary_line(self) -> str:
        """中文一行摘要（给人读的那一份）。"""
        return (
            f"本轮处理 {self.processed} 条｜对齐 {self.aligned} 条｜挂起 {self.pending} 条"
            f"｜写回成功 {self.writeback_ok} 份、失败 {self.writeback_failed} 份"
            f"｜耗时 {self.elapsed_seconds:.2f} 秒"
        )


def build_round_summary(source: Any) -> RoundSummary:
    """从一轮的产出对象算出运行摘要。

    守护轮次（``DaemonRound``）与批处理重放（``ReplayOutcome``）字段名不完全
    一样，这里按「有就用、没有就退回 0」的口径取，不写死某一类 —— 换个产出
    对象也不用改这里。
    """
    processed = int(getattr(source, "accepted", 0) or 0)
    aligned = int(getattr(source, "rationale_count", 0) or 0)

    pending_value = getattr(source, "pending_count", None)
    if pending_value is None:
        pending_value = len(getattr(source, "pending", ()) or ())
    pending = int(pending_value or 0)

    writeback_ok = int(getattr(source, "drafts_written", 0) or 0)
    writeback_failed = int(getattr(source, "drafts_failed", 0) or 0)
    elapsed = float(getattr(source, "elapsed_seconds", 0.0) or 0.0)

    extra: dict[str, Any] = {}
    for name in (
        "files_seen",
        "files_scanned",
        "duplicates",
        "malformed",
        "conflicts",
        "pending_escalated",
        "notify_sent",
        "notify_failed",
    ):
        value = getattr(source, name, None)
        if value is not None:
            extra[name] = value
    drafts = getattr(source, "drafts", None)
    if drafts is not None:
        # 批处理重放不写回病理系统，草稿只落本地文件 —— 份数单独记一个字段，
        # 不混进「写回成功」。
        extra["drafts"] = len(drafts)
    placeholder = getattr(source, "placeholder_count", None)
    if placeholder is not None:
        extra["placeholder_count"] = placeholder

    return RoundSummary(
        processed=processed,
        aligned=aligned,
        pending=pending,
        writeback_ok=writeback_ok,
        writeback_failed=writeback_failed,
        elapsed_seconds=elapsed,
        extra=extra,
    )


class StructuredLineFormatter(logging.Formatter):
    """一行一件事：时间 | 级别 | 字段。"""

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, self.datefmt)
        return f"{stamp} | {record.levelname:<5} | {record.getMessage()}"


class StructuredLogger:
    """结构化行日志器。

    包一个标准库 ``Logger``：字段先脱敏，再拼成一行 ``键=值``。业务层只用
    ``event()`` 与几个快捷方法，不直接碰 ``logging``。
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        include_result_values: bool = False,
        name: str = LOGGER_NAME,
    ) -> None:
        self._logger = logger if logger is not None else logging.getLogger(name)
        #: 是否把结果值原样写进日志。默认关 —— 见模块开头的三条硬规矩。
        self.include_result_values = include_result_values

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    @property
    def name(self) -> str:
        return self._logger.name

    def event(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        """记一条事件（字段先脱敏）。"""
        payload = redact_fields(fields, include_result_values=self.include_result_values)
        self._logger.log(level, format_line(event, payload))

    def debug(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.DEBUG, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.INFO, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.WARNING, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.event(event, level=logging.ERROR, **fields)

    def round_summary(self, summary: RoundSummary, *, level: int = logging.INFO) -> None:
        """记一轮的运行摘要行。"""
        self.event("round_summary", level=level, **summary.as_fields())


def _resolve_level(level: str) -> int:
    """日志级别名 → 数值；认不出来直接报错（配错了要看得见）。"""
    name = str(level or "INFO").strip().upper()
    if name not in LEVELS:
        raise ValueError(
            f"日志级别只能是 {'/'.join(LEVELS)}，当前值 {level!r}"
        )
    return LEVELS[name]


def setup_logging(
    *,
    level: str = "INFO",
    path: Path | str | None = None,
    stream: TextIO | None = None,
    include_result_values: bool = False,
    logger_name: str = LOGGER_NAME,
) -> StructuredLogger:
    """装好结构化行日志，返回日志器。

    - ``path`` 给了就写文件，否则写 ``stream``（默认标准错误）；
    - 重复调用不会把同一份输出打两遍 —— 本模块挂上去的 handler 先摘掉再重挂，
      别人挂在同一个日志器上的 handler 不动；
    - ``include_result_values`` 决定结果值是否原样入日志（默认不）。

    标准输出留给业务输出（草稿摘要、清单），日志走标准错误 —— 两者不打架。
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(_resolve_level(level))

    for handler in list(logger.handlers):
        if getattr(handler, _MANAGED_FLAG, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 摘旧 handler 失败不该挡住装日志
                pass

    formatter = StructuredLineFormatter()
    handler: logging.Handler
    if path is not None:
        target = Path(path).expanduser()
        if target.parent and str(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
    else:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(logger.level)
    setattr(handler, _MANAGED_FLAG, True)
    logger.addHandler(handler)
    # 记录已由本日志器处理，不再往根日志器传 —— 嵌到别的应用里时同一行不会
    # 打两遍。需要被外部采集时用 ``addHandler`` 另挂一个 handler 即可。
    logger.propagate = False

    return StructuredLogger(logger, include_result_values=include_result_values)

"""时间格式化。

院内报���里的时间是 HL7 时间戳（``YYYYMMDDHHMMSS``），给人看的草稿和清单里
要写成 ``YYYY-MM-DD HH:MM``。认不出来的格式原样给出 —— 宁可给个看不懂的
原值，也不要猜错时间。
"""

from __future__ import annotations

from datetime import datetime

__all__ = ["format_observed_at", "now_text"]


def format_observed_at(value: str | None) -> str:
    """把 HL7 时间戳整理成 ``YYYY-MM-DD HH:MM``；认不出来就原样给出。"""
    if not value:
        return "时间未知"
    raw = value.strip()
    if len(raw) >= 12 and raw[:8].isdigit() and raw[8:12].isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def now_text() -> str:
    """当前时间（``YYYY-MM-DD HH:MM:SS``）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

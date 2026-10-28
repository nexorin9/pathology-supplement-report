"""当班医师路由：按排班表找当班的人，排班缺失时降级到科室默认收件人。

**为什么要有这一层**：补充报告草稿是要**有人签发**的 —— 签发的是当班医师，
不是全科室。发到「病理科公共邮箱」的结果是：邮件躺在公共信箱里，谁也没
负责，报告拖到第二天才签。院内的做法是排班表 —— 谁白班、谁夜班、哪一周，
写在一张表里。通知应当照那张表走。

**两条硬规矩**：

1. **排班命中就按排班走。** 排班表命中当前时段时，通知只发给当班的那位
   （可多位），收件人写进消息，随通知一起留痕。
2. **排班缺失就降级，但降级要看得见。** 排班表没配、装载失败、已过期、
   当前时段没排班 —— 这四种情况一律降级到**科室默认收件人**
   （环境变量 ``NOTIFY_RECIPIENTS``），并在运行摘要里显式写明「降级了、
   为什么降级」。排班表本身就是一份要有人维护的文件，它一定会过期；
   **过期不能等于不通知**。

排班表装载失败**不抛异常**：一份排班表被同事改坏了，不该让整批草稿的
通知发不出去（通知发不出去只是通知发不出去）。失败降级 + 告警，和
:mod:`patho_supplement.align.lexicon` 的词典回退是同一条口径。

**排班表格式**（外置 JSON，科室自己维护）::

    {
      "version": "2026.10",
      "department": "病理科",
      "valid_from": "2026-10-01",
      "valid_to": "2026-12-31",
      "shifts": [
        {"id": "day", "name": "白班", "start": "08:00", "end": "18:00",
         "days": ["MON", "TUE", "WED", "THU", "FRI"],
         "recipients": ["pathologist-a@example.invalid"]},
        {"id": "night", "name": "夜班", "start": "18:00", "end": "08:00",
         "recipients": ["pathologist-b@example.invalid"]}
      ]
    }

- ``start`` / ``end``：班次起止时间（``HH:MM``；**结束早于开始**表示跨零点
  的夜班，``18:00–08:00`` 就是当天 18 点到次日 8 点）；
- ``days``：可选，限定星期几（``MON``…``SUN`` 或「周一」…「周日」）；
  缺省 = 每天；
- ``recipients``：当班医师的收件人（邮箱）；缺省 = 该班次没有收件人，
  按「排班缺失」降级；
- ``valid_from`` / ``valid_to``：可选，班次或整张排班表的有效期；
- ``version``：排班表版本标识，随运行摘要一起留痕 —— 「当时生效的是哪一
  版排班表」要答得出来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "DutyRoster",
    "DutyRosterError",
    "DutyRouter",
    "DutyShift",
    "RouteResult",
    "ROUTE_DEFAULT",
    "ROUTE_LABELS",
    "ROUTE_NONE",
    "ROUTE_SHIFT",
    "load_duty_roster",
]

logger = logging.getLogger(__name__)

# --- 路由依据（机器可读；审计与摘要按这一套口径统计） ---------------------

#: 排班命中：当前时段有当班班次，收件人取当班医师。
ROUTE_SHIFT = "duty_shift"
#: 降级到科室默认收件人（排班表不可用 / 已过期 / 当前时段没排班）。
ROUTE_DEFAULT = "default"
#: 排班没命中，科室默认收件人也没配 —— 这条通知没有收件人。
ROUTE_NONE = "none"

#: 路由依据的中文标签（摘要与审计明细里用）。
ROUTE_LABELS: dict[str, str] = {
    ROUTE_SHIFT: "排班命中",
    ROUTE_DEFAULT: "科室默认收件人",
    ROUTE_NONE: "无收件人",
}

#: 星期几的编号（与 ``date.weekday()`` 一致：周一 = 0）。
_WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_WEEKDAY_ALIASES: dict[str, int] = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


class DutyRosterError(ValueError):
    """排班表文件本身不可用（缺失 / 非法 JSON / 结构不对）。

    加载器捕获它并降级到科室默认收件人，因此这个异常**不会**冒到主流程 ——
    只在显式要求严格模式时才抛出。
    """


# ------------------------------------------------------------------ 班次


@dataclass(frozen=True)
class DutyShift:
    """一个班次：起止时间 + 星期限制 + 有效期 + 当班收件人。"""

    shift_id: str
    name: str = ""
    start: str = "00:00"
    end: str = "00:00"
    #: 限定星期几（``date.weekday()`` 的编号）；空 = 每天。
    weekdays: tuple[int, ...] = ()
    recipients: tuple[str, ...] = ()
    valid_from: date | None = None
    valid_to: date | None = None

    @property
    def label(self) -> str:
        """班次的可读名（没有名字就用 ID）。"""
        return self.name or self.shift_id

    @property
    def start_time(self) -> time:
        return _parse_time(self.start)

    @property
    def end_time(self) -> time:
        return _parse_time(self.end)

    @property
    def crosses_midnight(self) -> bool:
        """结束早于开始 → 跨零点的夜班。"""
        return self.start_time > self.end_time

    @property
    def is_full_day(self) -> bool:
        """起止时间相同 → 整天值班（周末值班常这么写）。"""
        return self.start_time == self.end_time

    def shift_date_for(self, moment: datetime) -> date | None:
        """这个时刻落在本班次里吗？返回**班次开始的那一天**。

        夜班跨零点：``18:00–08:00`` 的班次，在 23 点属于当天开始的班次，
        在凌晨 3 点属于**前一天**开始的班次 —— 星期限制与有效期都按「班次
        开始那一天」算，否则周日夜班会算成周一夜班。
        """
        clock = moment.time()
        if self.is_full_day:
            return moment.date()
        if not self.crosses_midnight:
            if self.start_time <= clock < self.end_time:
                return moment.date()
            return None
        if clock >= self.start_time:
            return moment.date()
        if clock < self.end_time:
            return moment.date() - timedelta(days=1)
        return None

    def applies_on(self, day: date) -> bool:
        """这个班次在指定日期上是否成立（星期限制 + 有效期）。"""
        return self.miss_reason(day) is None

    def miss_reason(self, day: date) -> str | None:
        """这个班次在这一天**为什么**不成立；成立时返回 ``None``。

        降级说明要说得具体：科室看到「已过期」会更新排班表，看到「不在排班日」
        会去查是不是排错了星期 —— 两件事要分开。
        """
        if self.weekdays and day.weekday() not in self.weekdays:
            return "当前时段的排班不在排班日"
        if self.valid_from is not None and day < self.valid_from:
            return "当前时段的排班尚未生效"
        if self.valid_to is not None and day > self.valid_to:
            return "当前时段的排班已过期"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "weekdays": list(self.weekdays),
            "recipients": list(self.recipients),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }


# ------------------------------------------------------------------ 排班表


@dataclass(frozen=True)
class DutyRoster:
    """一份已装载的当班排班表。

    ``loaded=False`` 表示**装载失败**（文件缺失 / 非法 JSON / 结构不对）：
    这种排班表一律按「排班缺失」降级，不猜当班是谁。
    """

    shifts: tuple[DutyShift, ...] = ()
    version: str = ""
    source: str = ""
    department: str = ""
    valid_from: date | None = None
    valid_to: date | None = None
    warnings: tuple[str, ...] = ()
    loaded: bool = False

    @property
    def shift_count(self) -> int:
        return len(self.shifts)

    def expired_on(self, day: date) -> bool:
        """整张排班表在这一天是否已过期。"""
        return self.valid_to is not None and day > self.valid_to

    def not_yet_on(self, day: date) -> bool:
        """整张排班表在这一天是否尚未生效。"""
        return self.valid_from is not None and day < self.valid_from

    def summary(self) -> dict[str, Any]:
        """运行摘要视图（写进运行摘要可查）。"""
        return {
            "version": self.version,
            "source": self.source,
            "department": self.department,
            "shift_count": self.shift_count,
            "loaded": self.loaded,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "warnings": list(self.warnings),
        }


# ------------------------------------------------------------------ 路由结果


@dataclass(frozen=True)
class RouteResult:
    """一次路由的结果。

    ``basis`` 说明收件人是怎么来的：``duty_shift``（排班命中）/
    ``default``（降级到科室默认收件人）/ ``none``（没有收件人）。
    ``degraded=True`` 表示**走了降级路径** —— 运行摘要里必须看得见。
    """

    recipients: tuple[str, ...] = ()
    basis: str = ROUTE_NONE
    degraded: bool = False
    detail: str = ""
    shift_id: str | None = None
    shift_name: str | None = None
    roster_version: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def basis_label(self) -> str:
        return ROUTE_LABELS.get(self.basis, self.basis)

    def summary_line(self) -> str:
        """一句话说明（进守护进程每轮运行摘要）。"""
        if self.basis == ROUTE_SHIFT:
            who = self.shift_name or self.shift_id or "当班"
            return f"收件人路由：排班命中（{who}｜收件人 {len(self.recipients)} 人）"
        if self.basis == ROUTE_DEFAULT:
            return f"收件人路由：降级到科室默认收件人（{self.detail}）"
        return f"收件人路由：没有收件人（{self.detail}）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "basis_label": self.basis_label,
            "recipients": list(self.recipients),
            "recipient_count": len(self.recipients),
            "degraded": self.degraded,
            "detail": self.detail,
            "shift_id": self.shift_id,
            "shift_name": self.shift_name,
            "roster_version": self.roster_version,
            "warnings": list(self.warnings),
            "summary": self.summary_line(),
        }


# ------------------------------------------------------------------ 路由器


class DutyRouter:
    """当班医师路由器。

    ``roster_path`` 给了就按文件装载（文件改了会自动重载）；没给就是
    「没有排班表」，一律降级到科室默认收件人。
    """

    def __init__(
        self,
        roster: DutyRoster | None = None,
        *,
        default_recipients: Sequence[str] = (),
        roster_path: Path | str | None = None,
    ) -> None:
        self._roster = roster
        self._roster_path = (
            Path(roster_path).expanduser() if roster_path is not None else None
        )
        self._mtime: float | None = None
        self._default_recipients = tuple(
            item.strip() for item in default_recipients if item and item.strip()
        )

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, config: Any) -> "DutyRouter":
        """按配置面装配（排班表路径与默认收件人都来自环境变量）。"""
        return cls(
            roster_path=getattr(config, "notify_duty_roster_path", None),
            default_recipients=getattr(config, "notify_recipients", ()) or (),
        )

    @property
    def default_recipients(self) -> tuple[str, ...]:
        return self._default_recipients

    @property
    def roster_path(self) -> Path | None:
        return self._roster_path

    # ------------------------------------------------------------ 排班表

    def current_roster(self) -> DutyRoster:
        """当前生效的排班表（文件改了就重新装载）。

        没配排班表路径时返回一份 ``loaded=False`` 的空排班表 —— 那是
        「没有排班表」这一事实，不是错误。
        """
        if self._roster_path is None:
            return self._roster or DutyRoster(
                loaded=False,
                warnings=("未配置排班表（NOTIFY_DUTY_ROSTER_PATH）",),
            )
        mtime = _safe_mtime(self._roster_path)
        if self._roster is None or mtime != self._mtime:
            self._roster = load_duty_roster(self._roster_path)
            self._mtime = mtime
        return self._roster

    # ------------------------------------------------------------ 路由

    def route(self, *, at: datetime | None = None) -> RouteResult:
        """取当班收件人。

        ``at`` 给了就按那个时刻路由（测试与演练用），默认取当前时间。
        """
        moment = at or datetime.now()
        roster = self.current_roster()

        if not roster.loaded:
            reason = roster.warnings[0] if roster.warnings else "排班表不可用"
            return self._fallback(roster, reason)
        if roster.expired_on(moment.date()):
            return self._fallback(
                roster, f"排班表已过期（有效至 {roster.valid_to.isoformat()}）"
            )
        if roster.not_yet_on(moment.date()):
            return self._fallback(
                roster, f"排班表尚未生效（{roster.valid_from.isoformat()} 起）"
            )

        candidates: list[DutyShift] = []
        missed_reasons: list[str] = []
        for shift in roster.shifts:
            shift_day = shift.shift_date_for(moment)
            if shift_day is None:
                continue
            reason = shift.miss_reason(shift_day)
            if reason:
                missed_reasons.append(reason)
                continue
            candidates.append(shift)

        if not candidates:
            if missed_reasons:
                # 都说同一个原因就说清楚那个原因；原因不一致就说个笼统的。
                unique = sorted(set(missed_reasons))
                reason = (
                    unique[0]
                    if len(unique) == 1
                    else "当前时段的排班已过期或不在排班日"
                )
                return self._fallback(roster, reason)
            return self._fallback(roster, "当前时段没有排班")

        # 多个班次同时命中时的取舍：**有收件人的班次优先**（没写收件人的
        # 班次约等于没排），其次取**最晚开始**的那个（同起点按班次 ID 排序）。
        # 谁是当班的以排班表为准，不由程序随手挑；命中的多寡写进告警，
        # 让人知道排班表本身可能要修。
        usable = [shift for shift in candidates if shift.recipients] or candidates
        usable.sort(key=lambda shift: (shift.start_time, shift.shift_id))
        shift = usable[-1]
        warnings = list(roster.warnings)
        if len(candidates) > 1:
            warnings.append(
                f"当前时段命中 {len(candidates)} 个班次，按最晚开始的「{shift.label}」取"
            )

        if not shift.recipients:
            return self._fallback(
                roster, f"当班班次「{shift.label}」没有配置收件人", warnings=warnings
            )

        return RouteResult(
            recipients=shift.recipients,
            basis=ROUTE_SHIFT,
            degraded=False,
            detail=f"当班班次「{shift.label}」",
            shift_id=shift.shift_id,
            shift_name=shift.label,
            roster_version=roster.version,
            warnings=tuple(warnings),
        )

    def _fallback(
        self,
        roster: DutyRoster,
        reason: str,
        *,
        warnings: Sequence[str] = (),
    ) -> RouteResult:
        """降级到科室默认收件人（没有默认收件人就如实说没有）。"""
        all_warnings = list(roster.warnings) + list(warnings)
        if self._default_recipients:
            return RouteResult(
                recipients=self._default_recipients,
                basis=ROUTE_DEFAULT,
                degraded=True,
                detail=(
                    f"{reason}；已改用科室默认收件人"
                    f"（{len(self._default_recipients)} 个）"
                ),
                roster_version=roster.version,
                warnings=tuple(all_warnings),
            )
        return RouteResult(
            recipients=(),
            basis=ROUTE_NONE,
            degraded=True,
            detail=f"{reason}；科室默认收件人也没配置，这条通知没有收件人",
            roster_version=roster.version,
            warnings=tuple(all_warnings),
        )


# ------------------------------------------------------------------ 装载


def load_duty_roster(path: Path | str, *, strict: bool = False) -> DutyRoster:
    """装载当班排班表。

    ``strict=False``（默认）时**装载失败不抛异常**：返回一份
    ``loaded=False`` 的排班表并留下告警，由路由层降级到科室默认收件人。
    ``strict=True`` 时改为抛 :class:`DutyRosterError`（自检与测试用）。
    """
    file_path = Path(path).expanduser()
    try:
        shifts, meta, warnings = _load_from_file(file_path)
    except DutyRosterError as exc:
        if strict:
            raise
        message = str(exc)
        logger.warning("当班排班表装载失败：%s", message)
        return DutyRoster(
            shifts=(),
            version="",
            source=str(file_path),
            loaded=False,
            warnings=(message,),
        )

    roster = DutyRoster(
        shifts=shifts,
        version=meta.get("version", "") or f"file:{file_path.name}",
        source=str(file_path),
        department=meta.get("department", ""),
        valid_from=meta.get("valid_from"),
        valid_to=meta.get("valid_to"),
        warnings=tuple(warnings),
        loaded=True,
    )
    for warning in warnings:
        logger.warning("当班排班表：%s", warning)
    return roster


def _load_from_file(
    file_path: Path,
) -> tuple[tuple[DutyShift, ...], dict[str, Any], list[str]]:
    """从外置 JSON 文件装载班次与元信息。"""
    if not file_path.exists():
        raise DutyRosterError(f"排班表文件不存在：{file_path}")
    if not file_path.is_file():
        raise DutyRosterError(f"排班表路径不是文件：{file_path}")
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DutyRosterError(f"排班表文件读取失败：{file_path}（{exc}）") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DutyRosterError(
            f"排班表文件不是合法 JSON：{file_path}（第 {exc.lineno} 行 {exc.msg}）"
        ) from exc

    if isinstance(payload, Mapping):
        raw_shifts = payload.get("shifts")
    elif isinstance(payload, list):
        raw_shifts = payload
    else:
        raise DutyRosterError("排班表根节点必须是对象（含 shifts 字段）或数组")
    if not isinstance(raw_shifts, list):
        raise DutyRosterError("排班表 shifts 字段必须是数组")

    shifts: list[DutyShift] = []
    warnings: list[str] = []
    for index, raw in enumerate(raw_shifts):
        shift, shift_warnings = _parse_shift(raw, index)
        warnings.extend(shift_warnings)
        if shift is None:
            continue
        shifts.append(shift)

    if not shifts:
        raise DutyRosterError(f"排班表里一条可用班次都没有：{file_path}")

    meta: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        raw_version = payload.get("version")
        if isinstance(raw_version, str):
            meta["version"] = raw_version.strip()
        raw_department = payload.get("department")
        if isinstance(raw_department, str):
            meta["department"] = raw_department.strip()
        meta["valid_from"] = _parse_date(payload.get("valid_from"), warnings, "排班表 valid_from")
        meta["valid_to"] = _parse_date(payload.get("valid_to"), warnings, "排班表 valid_to")

    return tuple(shifts), meta, warnings


def _parse_shift(raw: object, index: int) -> tuple[DutyShift | None, list[str]]:
    """解析一条班次。**单条非法只跳过这一条**并留下告警。"""
    where = f"第 {index + 1} 条班次"
    if not isinstance(raw, Mapping):
        return None, [f"{where}不是对象，已跳过"]

    shift_id = raw.get("id")
    if not isinstance(shift_id, str) or not shift_id.strip():
        return None, [f"{where}缺 id（班次标识），已跳过"]
    shift_id = shift_id.strip()

    name = raw.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else shift_id

    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, str) or not start.strip():
        return None, [f"{where}缺 start（开始时间），已跳过"]
    if not isinstance(end, str) or not end.strip():
        return None, [f"{where}缺 end（结束时间），已跳过"]
    try:
        _parse_time(start)
        _parse_time(end)
    except ValueError as exc:
        return None, [f"{where}的时间格式不对（{exc}），已跳过"]

    recipients_raw = raw.get("recipients") or []
    if not isinstance(recipients_raw, list):
        return None, [f"{where}的 recipients 不是数组，已跳过"]
    recipients = tuple(
        item.strip() for item in recipients_raw if isinstance(item, str) and item.strip()
    )

    weekdays_raw = raw.get("days") or []
    if not isinstance(weekdays_raw, list):
        return None, [f"{where}的 days 不是数组，已跳过"]
    weekdays: list[int] = []
    for item in weekdays_raw:
        if not isinstance(item, str):
            continue
        code = _normalize_weekday(item)
        if code is None:
            continue
        if code not in weekdays:
            weekdays.append(code)
    weekdays.sort()

    warnings: list[str] = []
    valid_from = _parse_date(raw.get("valid_from"), warnings, f"{where} valid_from")
    valid_to = _parse_date(raw.get("valid_to"), warnings, f"{where} valid_to")

    shift = DutyShift(
        shift_id=shift_id,
        name=name,
        start=start.strip(),
        end=end.strip(),
        weekdays=tuple(weekdays),
        recipients=recipients,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    return shift, warnings


# ------------------------------------------------------------------ 解析小工具


def _parse_time(value: str) -> time:
    """把 ``HH:MM`` / ``H:MM`` / ``HHMM`` 解析成 :class:`datetime.time`。"""
    raw = value.strip()
    if not raw:
        raise ValueError("时间为空")
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) < 2:
            raise ValueError(f"时间格式不对：{value!r}")
        hour_text, minute_text = parts[0], parts[1]
    else:
        if len(raw) not in (3, 4):
            raise ValueError(f"时间格式不对：{value!r}")
        hour_text, minute_text = raw[:-2], raw[-2:]
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError(f"时间格式不对：{value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{value!r}")
    return time(hour=hour, minute=minute)


def _normalize_weekday(value: str) -> int | None:
    """把星期几的各种写法折算成编号（周一 = 0）；认不出来返回 ``None``。"""
    raw = value.strip().upper()
    if not raw:
        return None
    if raw in _WEEKDAY_CODES:
        return _WEEKDAY_ALIASES[raw]
    text = raw.replace("周", "").replace("星期", "").replace("礼拜", "")
    if text in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[text]
    for index, code in enumerate(_WEEKDAY_CODES):
        if code.startswith(text) and len(text) == 3:
            return index
    return None


def _parse_date(value: object, warnings: list[str], where: str) -> date | None:
    """解析 ``YYYY-MM-DD``；认不出来就当作没填（并留下告警）。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        warnings.append(f"{where} 不是 YYYY-MM-DD 格式（{value!r}），按未填处理")
        return None


def _safe_mtime(path: Path) -> float | None:
    """取文件修改时间；读不到就返回 ``None``（文件缺失或无权限）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None

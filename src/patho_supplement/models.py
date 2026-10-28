"""领域模型。

全系统的数据形状只有这几种：报告基线、取材部位（蜡块）、加做申请、结果条目、
对齐依据、草稿与草稿版本。归属键统一为 **病理号 + 部位序号 + 蜡块号** 三元组，
任何一条结果都要能说清「它属于哪一块」。

所有模型都是纯数据（dataclass），不带行为与 IO，便于落库、比对与导出。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Alignment",
    "Block",
    "CheckItemState",
    "Draft",
    "DraftEntry",
    "DraftState",
    "DraftVersion",
    "MatchLevel",
    "OrderAction",
    "OrderRequest",
    "OwnershipKey",
    "ReportBaseline",
    "ResultEntry",
    "ResultStatus",
]


class ResultStatus(str, Enum):
    """OBX-11 结果状态。"""

    PRELIMINARY = "P"       # 初步结果
    FINAL = "F"             # 终报
    CORRECTION = "C"        # 更正（取代同键旧值）
    NOT_OBTAINED = "X"      # 无法获得（已回报但不可并入草稿）

    @property
    def label(self) -> str:
        return {
            ResultStatus.PRELIMINARY: "初步",
            ResultStatus.FINAL: "终报",
            ResultStatus.CORRECTION: "更正",
            ResultStatus.NOT_OBTAINED: "无法获得",
        }[self]

    @property
    def mergeable(self) -> bool:
        """这条结果能不能并入草稿。

        「无法获得」（``X``）不能 —— 报告里写一行空项，医师会以为这项没做。
        它已回报，只是没有结果，进待人工处理清单等人补。
        """
        return self is not ResultStatus.NOT_OBTAINED

    @property
    def authority(self) -> int:
        """同块同项的权威度：终报压过初步，更正压过终报。

        用于**同一观察时间**下的取舍 —— 观察时间一样时，看谁更权威。
        """
        return {
            ResultStatus.NOT_OBTAINED: 0,
            ResultStatus.PRELIMINARY: 1,
            ResultStatus.FINAL: 2,
            ResultStatus.CORRECTION: 3,
        }[self]


class OrderAction(str, Enum):
    """ORC-1 订单控制动作。"""

    NEW = "NW"                # 新增申请
    CANCEL = "CA"             # 取消
    HOLD = "HD"               # 暂停
    TERMINATE = "DC"          # 终止
    RESULT_FOLLOWS = "RE"     # 结果随后

    @property
    def label(self) -> str:
        return {
            OrderAction.NEW: "新增",
            OrderAction.CANCEL: "取消",
            OrderAction.HOLD: "暂停",
            OrderAction.TERMINATE: "终止",
            OrderAction.RESULT_FOLLOWS: "结果随后",
        }[self]


class MatchLevel(str, Enum):
    """四级匹配命中方式。"""

    L1_BLOCK_EXACT = "L1_block_exact"
    L2_REGION_NORMALIZED = "L2_region_normalized"
    L3_REGION_FUZZY = "L3_region_fuzzy"
    L4_PENDING = "L4_pending"

    @property
    def confidence(self) -> str:
        """置信等级：pending 无等级。"""
        return {
            MatchLevel.L1_BLOCK_EXACT: "高",
            MatchLevel.L2_REGION_NORMALIZED: "中",
            MatchLevel.L3_REGION_FUZZY: "低",
            MatchLevel.L4_PENDING: "无",
        }[self]


class DraftState(str, Enum):
    """草稿四态。"""

    PENDING = "pending"            # 待生成
    GENERATED = "generated"        # 已生成
    WRITTEN_BACK = "written_back"  # 已回写
    SIGNED = "signed"              # 已签发（由院内系统回写，本系统只读）

    @property
    def label(self) -> str:
        """中文标签（报错信息与状态表里用）。"""
        return {
            DraftState.PENDING: "待生成",
            DraftState.GENERATED: "已生成",
            DraftState.WRITTEN_BACK: "已回写",
            DraftState.SIGNED: "已签发",
        }[self]


class CheckItemState(str, Enum):
    """检查项状态。"""

    AWAITING = "awaiting"          # 待回报
    RETURNED = "returned"          # 已回报
    ALIGNED = "aligned"            # 已对齐
    MERGED = "merged"              # 已并入草稿
    MANUAL = "manual"              # 待人工
    CANCELLED = "cancelled"        # 已取消
    HELD = "held"                  # 已暂停
    TERMINATED = "terminated"      # 已终止

    @property
    def label(self) -> str:
        """中文标签（报错信息与状态表里用）。"""
        return {
            CheckItemState.AWAITING: "待回报",
            CheckItemState.RETURNED: "已回报",
            CheckItemState.ALIGNED: "已对齐",
            CheckItemState.MERGED: "已并入",
            CheckItemState.MANUAL: "待人工",
            CheckItemState.CANCELLED: "已取消",
            CheckItemState.HELD: "已暂停",
            CheckItemState.TERMINATED: "已终止",
        }[self]


@dataclass(frozen=True)
class OwnershipKey:
    """归属键：病理号 + 部位序号 + 蜡块号。

    部位序号与蜡块号都允许为空 —— 院内原始回报里常常缺其中一个，
    但病理号一定在。缺字段本身就是「要不要挂起」的判据，故此处不做填充。
    """

    accession_no: str
    region_seq: int | None = None
    block_no: str | None = None

    def as_tuple(self) -> tuple[str, int | None, str | None]:
        return (self.accession_no, self.region_seq, self.block_no)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志与展示
        return f"{self.accession_no}/{self.region_seq}/{self.block_no}"


@dataclass(frozen=True)
class Block:
    """一个取材部位（蜡块）。"""

    region_seq: int | None = None
    region_desc: str | None = None
    block_no: str | None = None
    order_no: str | None = None
    ordered_items: tuple[str, ...] = ()

    def ownership_key(self, accession_no: str) -> OwnershipKey:
        return OwnershipKey(accession_no, self.region_seq, self.block_no)


@dataclass(frozen=True)
class ReportBaseline:
    """病理报告基线（自病理系统取回的部位与已申请项目）。"""

    accession_no: str
    blocks: tuple[Block, ...] = ()
    base_version: str | None = None
    source_system: str = "pathology-system"
    fetched_at: str | None = None

    def ordered_item_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for block in self.blocks:
            for code in block.ordered_items:
                if code not in seen:
                    seen.append(code)
        return tuple(seen)


@dataclass(frozen=True)
class OrderRequest:
    """加做申请（申请侧事件）。"""

    accession_no: str
    order_no: str | None
    item_code: str
    action: OrderAction
    item_name: str | None = None
    order_time: str | None = None
    region_seq: int | None = None
    block_no: str | None = None
    region_desc: str | None = None
    source_system: str = "pathology-system"

    def ownership_key(self) -> OwnershipKey:
        return OwnershipKey(self.accession_no, self.region_seq, self.block_no)


@dataclass(frozen=True)
class ResultEntry:
    """一条加做检查结果回报。"""

    accession_no: str
    item_code: str
    item_name: str | None = None
    value: str | None = None
    status: ResultStatus = ResultStatus.FINAL
    observed_at: str | None = None
    order_no: str | None = None
    region_seq: int | None = None
    block_no: str | None = None
    region_desc: str | None = None
    source_system: str = "result-inbox"
    source_file: str | None = None

    def ownership_key(self) -> OwnershipKey:
        return OwnershipKey(self.accession_no, self.region_seq, self.block_no)

    @property
    def mergeable(self) -> bool:
        """这条结果能不能并入草稿。

        两条硬规则：

        - **无法获得**（``OBX-11=X``）不行 —— 已回报但没有值，写进报告等于
          让医师以为这项没做；
        - **值为空**不行 —— 空行在报告里跟「无表达」长得一样，一个空值就可能
          被读成阴性结论。空值进待人工处理清单，让人补。

        其余情况（初步 / 终报 / 更正，且有值）都可以并入。
        """
        if not self.status.mergeable:
            return False
        return bool((self.value or "").strip())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultEntry":
        payload = dict(data)
        payload["status"] = ResultStatus(payload.get("status", "F"))
        return cls(**payload)


@dataclass(frozen=True)
class Alignment:
    """对齐依据：一条结果凭什么算到某一块。

    六个必备字段：**命中方式**、**得分**、**召回键**、**来源系统**、
    **回报时间**、**置信等级**。生成与展示规则见
    :mod:`patho_supplement.align.rationale`。

    挂起的结果也留一条依据，但**没有置信等级** —— 挂起的结论是「不敢下结论」，
    不是「可信度低」。
    """

    accession_no: str
    item_code: str
    match_level: MatchLevel
    score: float = 0.0
    recall_key: str | None = None
    block_no: str | None = None
    region_seq: int | None = None
    region_desc: str | None = None
    source_system: str | None = None
    observed_at: str | None = None
    #: L3 词法命中时匹配上的词（写进依据，医师看得懂凭什么）。
    matched_terms: tuple[str, ...] = ()
    #: 挂起原因（未对上时写清为什么没对上）。
    pending_reason: str | None = None
    #: 需人工补齐的字段（未对上时给出）。
    missing_fields: tuple[str, ...] = ()

    @property
    def confidence(self) -> str:
        """置信等级：L1 高 / L2 中 / L3 低；挂起无等级。"""
        return self.match_level.confidence

    @property
    def pending(self) -> bool:
        """这条依据是不是「没对上」的那一类。"""
        return self.match_level is MatchLevel.L4_PENDING

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["match_level"] = self.match_level.value
        data["confidence"] = self.confidence
        data["matched_terms"] = list(self.matched_terms)
        data["missing_fields"] = list(self.missing_fields)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alignment":
        payload = dict(data)
        payload.pop("confidence", None)
        payload["match_level"] = MatchLevel(payload["match_level"])
        payload["matched_terms"] = tuple(payload.get("matched_terms") or ())
        payload["missing_fields"] = tuple(payload.get("missing_fields") or ())
        return cls(**payload)


@dataclass(frozen=True)
class DraftEntry:
    """草稿里的一行。"""

    item_code: str
    item_name: str | None = None
    value: str | None = None
    region_seq: int | None = None
    region_desc: str | None = None
    block_no: str | None = None
    source_system: str | None = None
    observed_at: str | None = None
    alignment: Alignment | None = None
    placeholder: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value": self.value,
            "region_seq": self.region_seq,
            "region_desc": self.region_desc,
            "block_no": self.block_no,
            "source_system": self.source_system,
            "observed_at": self.observed_at,
            "alignment": self.alignment.to_dict() if self.alignment else None,
            "placeholder": self.placeholder,
        }


@dataclass(frozen=True)
class Draft:
    """一份补充报告草稿。"""

    accession_no: str
    version: int
    entries: tuple[DraftEntry, ...] = ()
    body: str = ""
    base_version: str | None = None
    created_at: str | None = None
    state: DraftState = DraftState.PENDING

    @property
    def merged_count(self) -> int:
        """实际并入结果的条数（不含「待回报」占位）。"""
        return sum(1 for entry in self.entries if not entry.placeholder)

    @property
    def placeholder_count(self) -> int:
        return sum(1 for entry in self.entries if entry.placeholder)


@dataclass(frozen=True)
class DraftVersion:
    """草稿版本链上的一环。

    一版 = 一条 :class:`Draft` 的落库记录：条目数、生成时间、基础版本号
    （这一版是基于病理系统的哪一版写的）、内容指纹与状态。**旧版本不被覆盖**
    —— 医师签发后被追问「上一版写了什么」，按版本号回查就能答上来。
    """

    accession_no: str
    version: int
    base_version: str | None = None
    entry_count: int = 0
    content_hash: str = ""
    created_at: str | None = None
    state: DraftState = DraftState.GENERATED
    body: str = field(default="", repr=False)

    @classmethod
    def from_row(cls, row: Any) -> "DraftVersion":
        """从数据库行还原一版草稿。"""
        return cls(
            accession_no=row["accession_no"],
            version=int(row["version"]),
            base_version=row["base_version"],
            entry_count=int(row["entry_count"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            state=DraftState(row["state"]),
            body=row["body"],
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

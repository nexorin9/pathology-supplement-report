"""审计层。

补充报告由医师签发，签发之前每一条「凭什么这么归属、为什么这条没进草稿」都得
留得住痕迹 —— 内审与飞检语境下要能当场查出来。本包负责把这些痕迹写进状态库，
并按病理号导出来（复核、内审、飞检核对用的核对依据）。

- :mod:`patho_supplement.audit.ledger` —— 审计流水写入与读取（入站拒收、对齐依据、
  状态迁移、写回回执、通知发送共用同一张表）
- :mod:`patho_supplement.audit.export` —— 审计流水导出（CSV 可排序筛选 + 文本可打印）
"""

from __future__ import annotations

__all__ = ["export", "ledger"]

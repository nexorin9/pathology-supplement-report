"""入站接入层。

免疫组化室 / 分子实验室把加做检查结果回报成 HL7 v2 报文，落到结果目录里。
本包负责把这些报文读成结构化结果条目：

- :mod:`patho_supplement.ingest.er7` —— ER7 切分与段解析（不可解析即报错）
- :mod:`patho_supplement.ingest.zbr` —— 院内扩展段（蜡块号 / 取材部位描述 / 部位序号）
- :mod:`patho_supplement.ingest.oru_parser` —— 结果侧报文解析
- :mod:`patho_supplement.ingest.orm_parser` —— 申请侧报文解析
- :mod:`patho_supplement.ingest.idempotency` —— 幂等键与去重账本
- :mod:`patho_supplement.ingest.inbox_watcher` —— 结果目录监听
"""

from __future__ import annotations

__all__ = ["er7", "idempotency", "inbox_watcher", "orm_parser", "oru_parser", "zbr"]

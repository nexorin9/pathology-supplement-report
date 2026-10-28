"""挂起与续跑。

对不上的结果进这里等人补齐，补齐后从挂起的那一步接着跑 —— 不从头重跑整批。

- :mod:`patho_supplement.pending.reasons` —— 挂起原因分类与缺失字段映射
- :mod:`patho_supplement.pending.records` —— 挂起记录与待人工处理清单
- :mod:`patho_supplement.pending.store` —— 挂起项快照持久化
- :mod:`patho_supplement.pending.resume` —— 从挂起点续跑
"""

from __future__ import annotations

__all__ = ["reasons", "records", "resume", "store"]

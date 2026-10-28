"""成稿与回写层。

- :mod:`patho_supplement.compose.baseline` —— 病理报告基线（部位列表与已申请项目）
- :mod:`patho_supplement.compose.draft_renderer` —— 按部位分组渲染草稿正文
- :mod:`patho_supplement.compose.pathology_client` —— 出站 HTTP 客户端
- :mod:`patho_supplement.compose.writeback_queue` —— 回写失败重试队列
"""

from __future__ import annotations

__all__ = ["baseline", "draft_renderer", "pathology_client", "writeback_queue"]

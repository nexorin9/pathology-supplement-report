"""对齐层。

回答一个问题：**这条回报结果属于哪一块（取材部位 / 蜡块）**。

- :mod:`patho_supplement.align.matcher` —— 结构化硬过滤的精确命中
- :mod:`patho_supplement.align.query_derivation` —— 待查项派生与检索键派生
- :mod:`patho_supplement.align.normalizer` —— 蜡块号与部位描述归一化
- :mod:`patho_supplement.align.lexicon` —— 部位同义词典
- :mod:`patho_supplement.align.scorer` —— 中文词法打分
- :mod:`patho_supplement.align.filtering` —— 硬过滤键与过滤策略
- :mod:`patho_supplement.align.retriever` —— 多路召回扇出与合并
- :mod:`patho_supplement.align.merger` —— 候选合并、业务幂等键去重与按分排序
- :mod:`patho_supplement.align.rationale` —— 对齐依据
"""

from __future__ import annotations

__all__ = [
    "filtering",
    "lexicon",
    "matcher",
    "merger",
    "normalizer",
    "query_derivation",
    "rationale",
    "retriever",
    "scorer",
]

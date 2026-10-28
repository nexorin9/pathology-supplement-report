"""中文 BM25 词法打分器（替换向量相似度后端）。

**为什么需要这一层**：源产品的检索后端是 embedding 相似度检索
（``github_ref/haystack/haystack/components/retrievers/text_embedding_retriever.py``、
``.../retrievers/in_memory/embedding_retriever.py``）。相似度给不出「这条结果
就是 B3 块」这种确定性绑定 —— 0.87 分既可能是 B3 也可能是 B5；而在病理报告
里，**绑错一块就是一份签出去却写错部位的补充报告**。

向量相似度在本产品里被换成两件确定性的东西：

- **结构化硬过滤**（部位序号 / 蜡块号 / 项目码，见
  :mod:`patho_supplement.align.matcher`）：字面全中才算命中，不中才退到
  模糊匹配；
- **中文词法打分**（本模块）：硬过滤没中时，用部位描述的**词面相关度**给
  候选排序。词法分**可复现、可解释**：同一个候选集合、同一个查询，两次打分
  结果一模一样，而且能讲清楚「凭什么算这条 B3 块」。

**中文怎么切**：中文没有空格，词法打分的成败取决于切分。本模块用
**字符 bigram**：连续汉字串切成相邻两字一组（「子宫体」→「子宫」「宫体」），
单字成词时保留单字，字母数字串整段保留并转小写（「CD20」→「cd20」）。
bigram 不依赖词典、不需要分词器、不会因生僻字漏切；代价是短词会被切开 ——
但本产品的部位描述本就是「子宫体内侧」这种多字词，bigram 正好。

**同义扩展**：别名折算到规范词参与打分（口径与
:mod:`patho_supplement.align.lexicon` 一致）。基线写「子宫体」、回报写
「宫体」，两边都折算到规范词「子宫体」之后再比 —— 别名与规范词因此对同一个
候选打出**完全相同**的分，不会因为书写习惯不同而把同一部位排到后面。

**打分公式**：标准 BM25::

    raw  = Σ_{t ∈ Q∩D} IDF(t) · f(t,D)·(k1+1) / (f(t,D) + k1·(1 - b + b·|D|/avgdl))
    IDF(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

raw 没有上界，而判定阈值（``BM25_THRESHOLD``）是 0–1 量纲，所以再压一次::

    score = raw / (raw + saturation)

这个变换**严格单调**（raw 越大分越高），把分数固定在 [0, 1] 区间里，
阈值才有确定含义。``k1`` / ``b`` / ``saturation`` 都可配（见
:class:`ScorerParams`）—— 不同科室的部位描述习惯不同，参数就该由科室自己调。

**语料**：IDF 要有语料才有意义 —— 语料就是**候选部位描述集合**（对应源产品
的内存文档库：文档即候选部位）。语料为空时退化为统一 IDF，所有候选仍然
可比，只是区分度下降。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

from .lexicon import Lexicon, builtin_lexicon
from .normalizer import normalize_region_desc

__all__ = [
    "DEFAULT_B",
    "DEFAULT_K1",
    "DEFAULT_SATURATION",
    "LexicalScorer",
    "ScoredCandidate",
    "ScorerParams",
    "expand_terms",
    "tokenize",
]

#: BM25 词频饱和参数：越大越不惩罚高频词。
DEFAULT_K1 = 1.5
#: BM25 文档长度归一化参数：0 = 不惩罚长文档，1 = 完全按长度归一。
DEFAULT_B = 0.75
#: 原始分 → [0, 1] 的饱和常数。
DEFAULT_SATURATION = 2.0

#: 语料为空时的统一 IDF（相当于「该词只在一篇文档里出现过」）。
_UNIFORM_IDF = math.log(1.0 + 0.5 / 1.5)

#: 字母数字段。
_ALNUM_RUN_RE = re.compile(r"[0-9A-Za-z]+")
#: 两种段的合并扫描：字母数字段在前，汉字段在后。
#: 汉字段覆盖 CJK 统一表意文字（U+4E00–U+9FFF）、扩展 A（U+3400–U+4DBF）、
#: 兼容表意文字（U+F900–U+FAFF）与扩展 B 及以后（U+20000–U+3FFFF）。
#: 扩展 B 以后是生僻字区 —— 病理报告里人名地名常见，不切会让整段被丢掉。
_RUN_RE = re.compile(
    r"[0-9A-Za-z]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003ffff]+"
)


@lru_cache(maxsize=1)
def _default_lexicon() -> Lexicon:
    """内置最小词表（进程内缓存一份，避免每次打分都重建）。"""
    return builtin_lexicon()


def tokenize(text: str | None) -> tuple[str, ...]:
    """把一段文本切成词法打分用的 token 序列。

    切分口径：

    - 先过归一化层（全角转半角、去空白与标点、方位词归并）；
    - 连续汉字段切成**字符 bigram**（「子宫体」→「子宫」「宫体」）；单字汉字
      保留为单字 token（「肝」→「肝」）；
    - 字母数字段整段保留并转小写（「CD20」→「cd20」）—— 项目码与蜡块号
      不该被切开。

    输入为空、只有标点或 ``None`` 时返回空元组（该文本不参与打分）。
    本函数不抛异常。
    """
    if text is None:
        return ()
    normalized = normalize_region_desc(str(text))
    if not normalized:
        return ()

    tokens: list[str] = []
    for match in _RUN_RE.finditer(normalized):
        run = match.group(0)
        if _ALNUM_RUN_RE.fullmatch(run):
            tokens.append(run.lower())
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(tokens)


def expand_terms(
    text: str | None,
    *,
    lexicon: Lexicon | None = None,
    department: str | None = None,
) -> tuple[str, ...]:
    """词形扩展：原文本的 token + 词典折算到的规范词的 token。

    「别名折算到规范词参与打分」的具体落点：查词表拿到规范词，把规范词也
    切一遍 token 并进集合。这样「宫体」与「子宫体」切出的 token 集合完全
    相同（``宫体`` + 规范词 ``子宫体`` 的 ``子宫``/``宫体``），两者对同一个
    候选打出的分**一模一样** —— 书写习惯不同不会影响排序结果。

    查不到规范词、查不到就查不到（不猜），扩展结果与原 token 相同。
    """
    base = tokenize(text)
    active = lexicon if lexicon is not None else _default_lexicon()
    canonical = active.canonicalize(text, department=department)
    extra = tokenize(canonical) if canonical else ()
    if not extra:
        return base
    merged: list[str] = []
    for token in (*base, *extra):
        if token not in merged:
            merged.append(token)
    return tuple(merged)


@dataclass(frozen=True)
class ScorerParams:
    """BM25 打分参数（可配）。

    - ``k1``：词频饱和参数，必须为正；
    - ``b``：文档长度归一化参数，取值 0–1；
    - ``saturation``：原始分 → [0, 1] 的饱和常数，必须为正。

    越界一律 **报错并说明原因**，不静默改用默认值 —— 「配错了却看不出来」
    是院内工具最常见的投诉来源。
    """

    k1: float = DEFAULT_K1
    b: float = DEFAULT_B
    saturation: float = DEFAULT_SATURATION

    def __post_init__(self) -> None:
        if not math.isfinite(self.k1) or self.k1 <= 0:
            raise ValueError(f"BM25 参数 k1 必须为正数，当前值 {self.k1}")
        if not math.isfinite(self.b) or not 0.0 <= self.b <= 1.0:
            raise ValueError(f"BM25 参数 b 必须在 0 与 1 之间，当前值 {self.b}")
        if not math.isfinite(self.saturation) or self.saturation <= 0:
            raise ValueError(f"打分饱和常数必须为正数，当前值 {self.saturation}")

    def as_dict(self) -> dict[str, float]:
        """可打印的参数视图（写进运行摘要可查）。"""
        return {"k1": self.k1, "b": self.b, "saturation": self.saturation}


@dataclass(frozen=True)
class ScoredCandidate:
    """一个候选部位的打分结果。

    - ``text``：候选部位描述原文（审计里看得到）；
    - ``score``：归一化后的分（0–1，越大越像）；
    - ``raw_score``：BM25 原始分（可解释、可复算）；
    - ``matched_terms``：命中的查询词（写进对齐依据，医师能看懂凭什么）。
    """

    text: str
    score: float
    raw_score: float = 0.0
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CorpusStats:
    """语料统计：文档总数、词频、文档平均长度。"""

    total_docs: int
    doc_freq: Mapping[str, int]
    avgdl: float

    def idf(self, term: str) -> float:
        """词的 IDF（恒为正 —— 常见词贡献小，生僻词贡献大）。"""
        if self.total_docs <= 0:
            return _UNIFORM_IDF
        df = self.doc_freq.get(term, 0)
        return math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))


class LexicalScorer:
    """中文 BM25 词法打分器。

    语料（候选部位描述集合）在构造时给定，用于 IDF 与文档长度归一化；
    打分时用**同一份语料统计**给每个候选算分，因此各候选的分数**可比**。

    例::

        scorer = LexicalScorer(["子宫体内侧", "宫颈部", "乳腺组织"])
        for cand in scorer.score_candidates("宫体内侧", ["子宫体内侧", "宫颈部"]):
            print(cand.text, round(cand.score, 4))
    """

    def __init__(
        self,
        documents: Iterable[str] = (),
        *,
        params: ScorerParams | None = None,
        lexicon: Lexicon | None = None,
        department: str | None = None,
    ) -> None:
        """构造打分器。

        ``documents`` 是语料（候选部位描述集合）。``lexicon`` 缺省用内置最小
        词表；``department`` 决定科室限定条目是否生效。
        """
        self.params = params if params is not None else ScorerParams()
        self.lexicon = lexicon if lexicon is not None else _default_lexicon()
        self.department = department
        self._corpus: tuple[tuple[str, ...], ...] = tuple(
            expand_terms(document, lexicon=self.lexicon, department=department)
            for document in documents
        )
        self._stats = self._build_stats(self._corpus)

    # ------------------------------------------------------------ 语料统计

    @staticmethod
    def _build_stats(corpus: Sequence[Sequence[str]]) -> _CorpusStats:
        """由 token 序列的集合算语料统计。"""
        doc_freq: dict[str, int] = {}
        for tokens in corpus:
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1
        total = len(corpus)
        avgdl = (sum(len(tokens) for tokens in corpus) / total) if total else 1.0
        return _CorpusStats(total_docs=total, doc_freq=doc_freq, avgdl=avgdl or 1.0)

    @property
    def corpus_size(self) -> int:
        """语料文档数。"""
        return self._stats.total_docs

    @property
    def avgdl(self) -> float:
        """语料平均文档长度（token 数）。"""
        return self._stats.avgdl

    def idf(self, term: str) -> float:
        """词的 IDF（按当前语料统计）。"""
        return self._stats.idf(term)

    # ------------------------------------------------------------ 打分

    def _score_pair(
        self,
        query_terms: Sequence[str],
        doc_terms: Sequence[str],
        stats: _CorpusStats,
    ) -> tuple[float, float, tuple[str, ...]]:
        """算一对 (查询 token, 文档 token) 的分。

        返回 ``(归一化分, 原始分, 命中词)``。
        """
        if not query_terms or not doc_terms:
            return 0.0, 0.0, ()

        counts: dict[str, int] = {}
        for token in doc_terms:
            counts[token] = counts.get(token, 0) + 1

        doc_len = len(doc_terms)
        avgdl = stats.avgdl or 1.0
        k1 = self.params.k1
        b = self.params.b

        raw = 0.0
        matched: list[str] = []
        # 查询词按出现顺序去重后逐个累加（重复查询词不重复加权）。
        for term in dict.fromkeys(query_terms):
            freq = counts.get(term, 0)
            if not freq:
                continue
            matched.append(term)
            denominator = freq + k1 * (1.0 - b + b * (doc_len / avgdl))
            raw += stats.idf(term) * (freq * (k1 + 1.0)) / denominator

        if raw <= 0.0:
            return 0.0, 0.0, ()
        score = raw / (raw + self.params.saturation)
        return score, raw, tuple(matched)

    def score_detail(self, query: str | None, document: str | None) -> ScoredCandidate:
        """给一对 (查询, 候选部位描述) 打分，并给出命中词。"""
        query_terms = expand_terms(
            query, lexicon=self.lexicon, department=self.department
        )
        doc_terms = expand_terms(
            document, lexicon=self.lexicon, department=self.department
        )
        score, raw, matched = self._score_pair(query_terms, doc_terms, self._stats)
        return ScoredCandidate(
            text=document or "", score=score, raw_score=raw, matched_terms=matched
        )

    def score(self, query: str | None, document: str | None) -> float:
        """给一对 (查询, 候选部位描述) 打分，返回 [0, 1] 的归一化分。"""
        return self.score_detail(query, document).score

    def score_candidates(
        self, query: str | None, candidates: Sequence[str]
    ) -> list[ScoredCandidate]:
        """给一个查询对多个候选分别打分，按分降序返回。

        各候选用**同一份语料统计**算分，所以分数可比；同分时保持传入顺序
    （稳定序），保证同一批候选在任何一次运行里排出的次序一致。

        语料为空时以**候选集合本身**为语料算 IDF —— 候选集合本来就是这一轮
        的检索空间，用它当语料最贴切（对应源产品的内存文档库：文档即候选）。
        """
        if not candidates:
            return []
        stats = self._stats
        if stats.total_docs <= 0:
            stats = self._build_stats(
                [
                    expand_terms(
                        candidate, lexicon=self.lexicon, department=self.department
                    )
                    for candidate in candidates
                ]
            )
        query_terms = expand_terms(
            query, lexicon=self.lexicon, department=self.department
        )
        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            doc_terms = expand_terms(
                candidate, lexicon=self.lexicon, department=self.department
            )
            score, raw, matched = self._score_pair(query_terms, doc_terms, stats)
            scored.append(
                ScoredCandidate(
                    text=candidate, score=score, raw_score=raw, matched_terms=matched
                )
            )
        # 按分降序；同分保持传入顺序。
        order = sorted(range(len(scored)), key=lambda index: (-scored[index].score, index))
        return [scored[index] for index in order]

    def summary(self) -> dict[str, object]:
        """打分器的运行摘要视图（写进运行摘要可查）。"""
        return {
            "params": self.params.as_dict(),
            "corpus_size": self.corpus_size,
            "avgdl": round(self.avgdl, 4),
            "lexicon_version": self.lexicon.version,
            "department": self.department,
        }

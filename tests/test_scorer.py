"""中文 BM25 词法打分器。

覆盖五件事：中文怎么切词、同义扩展怎么把别名折算到规范词、同一待查项对
多个候选的分数是否可比且与词面相关度单调、默认参数在固定基准集上是否给出
确定排序（可复现）、以及 ``k1`` / ``b`` 是否真的可配。
"""

from __future__ import annotations

import pytest

from patho_supplement.align.lexicon import Lexicon, LexiconEntry
from patho_supplement.align.scorer import (
    DEFAULT_B,
    DEFAULT_K1,
    DEFAULT_SATURATION,
    LexicalScorer,
    ScoredCandidate,
    ScorerParams,
    expand_terms,
    tokenize,
)

#: 打分基准语料：四份候选部位描述。
CORPUS = ("子宫体内侧", "宫颈部", "乳腺组织", "肝组织")


def _scorer(documents=CORPUS, **kwargs) -> LexicalScorer:
    """按基准语料造一个打分器。"""
    return LexicalScorer(documents, **kwargs)


# ---------------------------------------------------------------- 切词


def test_tokenize_splits_han_run_into_character_bigrams() -> None:
    """连续汉字切成相邻两字一组。"""
    assert tokenize("子宫体") == ("子宫", "宫体")
    assert tokenize("子宫体内侧") == ("子宫", "宫体", "体内", "内侧")


def test_tokenize_keeps_single_han_character() -> None:
    """单字成词时保留单字。"""
    assert tokenize("肝") == ("肝",)


def test_tokenize_keeps_alnum_run_whole_and_lowercases() -> None:
    """字母数字段整段保留并转小写 —— 项目码与蜡块号不该被切开。"""
    assert tokenize("CD20") == ("cd20",)
    assert tokenize("B3") == ("b3",)
    assert tokenize("CD20阳性") == ("cd20", "阳性")


def test_tokenize_handles_mixed_text() -> None:
    """中英混排按段切。"""
    assert tokenize("子宫 体 CD20") == ("子宫", "宫体", "cd20")


def test_tokenize_drops_punctuation_and_normalizes_full_width() -> None:
    """标点不入词；全角转半角。"""
    assert tokenize("子宫体（内侧）") == ("子宫", "宫体", "体内", "内侧")
    assert tokenize("Ｂ３") == ("b3",)


def test_tokenize_returns_empty_for_blank_input() -> None:
    """空串、None、纯标点都返回空元组（不参与打分）。"""
    assert tokenize("") == ()
    assert tokenize(None) == ()
    assert tokenize("（）") == ()
    assert tokenize("   ") == ()


# ---------------------------------------------------------------- 参数


def test_default_params() -> None:
    """默认参数就是约定的三个值。"""
    params = ScorerParams()
    assert params.k1 == DEFAULT_K1
    assert params.b == DEFAULT_B
    assert params.saturation == DEFAULT_SATURATION
    assert params.as_dict() == {
        "k1": DEFAULT_K1,
        "b": DEFAULT_B,
        "saturation": DEFAULT_SATURATION,
    }


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"k1": 0}, "k1 必须为正数"),
        ({"k1": -1.5}, "k1 必须为正数"),
        ({"k1": float("inf")}, "k1 必须为正数"),
        ({"b": -0.1}, "b 必须在 0 与 1 之间"),
        ({"b": 1.5}, "b 必须在 0 与 1 之间"),
        ({"saturation": 0}, "饱和常数必须为正数"),
        ({"saturation": -2.0}, "饱和常数必须为正数"),
    ],
)
def test_invalid_params_are_rejected_with_readable_reason(kwargs, reason) -> None:
    """越界参数报错并说明原因，不静默改用默认值。"""
    with pytest.raises(ValueError) as excinfo:
        ScorerParams(**kwargs)
    assert reason in str(excinfo.value)


def test_k1_is_configurable() -> None:
    """k1 改的是词频饱和：越大，高频重复词的边际收益越小。"""
    low = _scorer(params=ScorerParams(k1=0.5))
    high = _scorer(params=ScorerParams(k1=3.0))
    assert low.score_detail("宫体", "子宫体内侧").raw_score != high.score_detail(
        "宫体", "子宫体内侧"
    ).raw_score
    assert low.score_detail("宫体", "子宫体内侧").raw_score > high.score_detail(
        "宫体", "子宫体内侧"
    ).raw_score


def test_b_is_configurable() -> None:
    """b 改的是文档长度归一：越大，长文档被压得越狠。"""
    short_doc = _scorer(params=ScorerParams(b=0.0)).score_detail(
        "宫体", "子宫体内侧"
    ).raw_score
    long_doc = _scorer(params=ScorerParams(b=1.0)).score_detail(
        "宫体", "子宫体内侧"
    ).raw_score
    assert short_doc > long_doc


def test_saturation_is_configurable() -> None:
    """饱和常数改的是量纲：越大，同样的原始分压得越低。"""
    low = _scorer(params=ScorerParams(saturation=1.0)).score("宫体", "子宫体内侧")
    high = _scorer(params=ScorerParams(saturation=4.0)).score("宫体", "子宫体内侧")
    assert 0.0 < high < low < 1.0


# ---------------------------------------------------------------- 同义扩展


def test_alias_folds_into_canonical_terms() -> None:
    """别名折算到规范词参与打分：token 集合里多出规范词的切法。"""
    assert expand_terms("宫体") == ("宫体", "子宫")
    assert expand_terms("子宫体") == ("子宫", "宫体")
    assert expand_terms("内膜") == ("内膜", "子宫", "宫体")


def test_unknown_text_is_not_expanded() -> None:
    """查不到就查不到，不猜一个最像的规范词。"""
    assert expand_terms("没听说过的部位") == tokenize("没听说过的部位")


def test_alias_scores_higher_than_unrelated_region() -> None:
    """题面验收点：别名命中规范词的得分高于无关词得分。"""
    scorer = _scorer()
    alias_score = scorer.score("宫体", "子宫体内侧")
    unrelated = scorer.score("宫体", "乳腺组织")
    assert alias_score > unrelated
    assert unrelated == 0.0


def test_alias_and_canonical_query_score_identically() -> None:
    """别名与规范词对同一个候选打出完全相同的分（书写习惯不影响排序）。"""
    scorer = _scorer()
    assert scorer.score("宫体", "子宫体内侧") == scorer.score("子宫体", "子宫体内侧")
    assert scorer.score("内膜", "子宫体内侧") == scorer.score("子宫体", "子宫体内侧")


def test_alias_document_is_matched_by_canonical_query() -> None:
    """候选写成别名时，规范词查询照样命中。"""
    scorer = _scorer()
    assert scorer.score("子宫体", "宫体") > 0.0
    assert scorer.score("子宫体", "宫体") == scorer.score("子宫体", "子宫体")


def test_department_scoped_alias_only_expands_in_its_department() -> None:
    """科室限定的别名只在本科室参与打分。"""
    custom = Lexicon(
        entries=(LexiconEntry(canonical="子宫体", aliases=("宫体",), departments=("妇科",)),),
        version="test",
        source="test",
    )
    in_dept = _scorer(lexicon=custom, department="妇科")
    other_dept = _scorer(lexicon=custom, department="普外科")
    no_dept = _scorer(lexicon=custom)

    assert in_dept.score("宫体", "子宫体内侧") > other_dept.score("宫体", "子宫体内侧")
    assert other_dept.score("宫体", "子宫体内侧") == no_dept.score("宫体", "子宫体内侧")
    assert "子宫" in in_dept.score_detail("宫体", "子宫体内侧").matched_terms
    assert "子宫" not in other_dept.score_detail("宫体", "子宫体内侧").matched_terms


# ---------------------------------------------------------------- 打分行为


def test_score_without_overlap_is_zero() -> None:
    """词面毫无交集 → 0 分。"""
    scorer = _scorer()
    assert scorer.score("子宫体", "乳腺组织") == 0.0


def test_score_is_monotonic_with_lexical_relevance() -> None:
    """词面越像分越高：完全命中 > 部分命中 > 毫无交集。"""
    scorer = _scorer()
    exact = scorer.score("子宫体内侧", "子宫体内侧")
    partial = scorer.score("子宫体内侧", "子宫体")
    unrelated = scorer.score("子宫体内侧", "乳腺组织")
    assert exact > partial > unrelated


def test_score_is_bounded_in_unit_interval() -> None:
    """归一化后的分落在 [0, 1]，阈值才有确定量纲。"""
    scorer = _scorer()
    for query in ("子宫体内侧", "宫体", "乳腺", "肝"):
        for document in CORPUS:
            score = scorer.score(query, document)
            assert 0.0 <= score <= 1.0


def test_matched_terms_are_reported() -> None:
    """命中词要能取到 —— 对齐依据要靠它讲清楚「凭什么算这条」。"""
    scorer = _scorer()
    detail = scorer.score_detail("宫体", "子宫体内侧")
    assert detail.matched_terms
    assert "宫体" in detail.matched_terms
    assert detail.raw_score > 0.0
    assert scorer.score_detail("宫体", "乳腺组织").matched_terms == ()


def test_blank_inputs_score_zero_instead_of_raising() -> None:
    """空输入返回确定结果，不抛未捕获异常。"""
    scorer = _scorer()
    assert scorer.score("", "子宫体内侧") == 0.0
    assert scorer.score(None, "子宫体内侧") == 0.0
    assert scorer.score("子宫体内侧", "") == 0.0
    assert scorer.score("子宫体内侧", None) == 0.0


# ---------------------------------------------------------------- 多候选排序


def test_score_candidates_returns_descending_scores() -> None:
    """同一待查项对多个候选分别打分，按分降序返回。"""
    scorer = _scorer()
    results = scorer.score_candidates("宫体", list(CORPUS))
    assert [item.text for item in results] == [
        "子宫体内侧",
        "宫颈部",
        "乳腺组织",
        "肝组织",
    ]
    scores = [item.score for item in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0].score > 0.0
    assert all(isinstance(item, ScoredCandidate) for item in results)


def test_score_candidates_picks_the_right_region_first() -> None:
    """候选里混着多个部位时，同一部位排第一。"""
    scorer = _scorer()
    results = scorer.score_candidates("乳腺组织", list(CORPUS))
    assert results[0].text == "乳腺组织"
    assert results[0].score > results[1].score


def test_score_candidates_on_empty_list() -> None:
    """没有候选就返回空列表。"""
    assert _scorer().score_candidates("宫体", []) == []


def test_score_candidates_uses_candidates_as_corpus_when_empty() -> None:
    """语料为空时以候选集合本身为语料：排序结论不变。"""
    without_corpus = LexicalScorer().score_candidates("宫体", list(CORPUS))
    with_corpus = _scorer().score_candidates("宫体", list(CORPUS))
    assert [item.text for item in without_corpus] == [
        item.text for item in with_corpus
    ]
    assert without_corpus[0].score > 0.0


def test_ties_keep_input_order() -> None:
    """同分保持传入顺序（稳定序），并列时结果不随输入抖动。"""
    scorer = _scorer()
    results = scorer.score_candidates("宫颈部", list(CORPUS))
    # 「宫颈部」只与候选「宫颈部」有词面交集，其余三份都是 0 分且保持传入顺序。
    assert results[0].text == "宫颈部"
    assert results[0].score > 0.0
    tails = [item.text for item in results if item.score == results[-1].score]
    assert tails == ["子宫体内侧", "乳腺组织", "肝组织"]


# ---------------------------------------------------------------- 可复现


def test_same_input_twice_gives_identical_scores() -> None:
    """同输入两次打分结果一致（可复现）。"""
    scorer = _scorer()
    first = scorer.score_candidates("宫体", list(CORPUS))
    second = scorer.score_candidates("宫体", list(CORPUS))
    assert first == second


def test_two_scorers_on_same_corpus_agree() -> None:
    """两个独立打分器对同一语料给出同一结果 —— 不依赖任何进程内状态。"""
    first = _scorer().score_candidates("宫体", list(CORPUS))
    second = _scorer().score_candidates("宫体", list(CORPUS))
    assert [(item.text, item.score) for item in first] == [
        (item.text, item.score) for item in second
    ]


def test_fixed_corpus_gives_fixed_ranking_and_scores() -> None:
    """基准集上的确定排序（golden）：默认参数固定，结果就固定。"""
    results = _scorer().score_candidates("宫体", list(CORPUS))
    assert [(item.text, round(item.score, 6)) for item in results] == [
        ("子宫体内侧", 0.511464),
        ("宫颈部", 0.0),
        ("乳腺组织", 0.0),
        ("肝组织", 0.0),
    ]


def test_scores_real_baseline_blocks_from_fixture() -> None:
    """拿基线夹具里的真实部位清单打分：别名「宫体」应归到「子宫体」那一块。"""
    import json
    from pathlib import Path

    fixture = Path(__file__).resolve().parent / "fixtures" / "inbox" / "baseline.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    blocks = payload["reports"][0]["blocks"]
    candidates = [block["region_desc"] for block in blocks]
    assert candidates == ["子宫体", "宫颈"]

    scorer = LexicalScorer(candidates)
    results = scorer.score_candidates("宫体", candidates)
    assert results[0].text == "子宫体"
    assert results[0].score > 0.0
    assert results[1].text == "宫颈"
    assert results[1].score == 0.0


# ---------------------------------------------------------------- 语料统计


def test_corpus_stats_are_reported() -> None:
    """语料规模与平均长度可查（写进运行摘要）。"""
    scorer = _scorer()
    assert scorer.corpus_size == len(CORPUS)
    assert scorer.avgdl > 0.0


def test_rare_term_has_higher_idf_than_common_term() -> None:
    """生僻部位词的 IDF 高于各篇都有的通用词。"""
    scorer = _scorer()
    assert scorer.idf("子宫") > scorer.idf("组织")
    assert scorer.idf("子宫") > 0.0


def test_summary_reports_params_and_lexicon_version() -> None:
    """打分器摘要写明参数与生效的词典版本（对齐结果可复现的前提）。"""
    summary = _scorer().summary()
    assert summary["params"]["k1"] == DEFAULT_K1
    assert summary["params"]["b"] == DEFAULT_B
    assert summary["corpus_size"] == len(CORPUS)
    assert summary["lexicon_version"]

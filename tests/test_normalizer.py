"""蜡块号与部位描述归一化。

覆盖三件事：写法差异能不能收敛到同一个键、归一化幂不等、以及边界输入
（空串、纯标点、全角、脏写法）都返回确定结果而不抛异常。
"""

from __future__ import annotations

import pytest

from patho_supplement.align.matcher import MatchState, align_entry
from patho_supplement.models import MatchLevel
from patho_supplement.align.normalizer import (    normalize_block_no,
    normalize_region_desc,
    same_block_no,
    same_region_desc,
)
from patho_supplement.models import Block, ReportBaseline, ResultEntry

#: 蜡块号的各种写法 —— 归一化后必须落到同一个值。
_BLOCK_VARIANTS = ("B3", "b3", "B-3", "B03", "b-03", "B 3", " B - 3 ", "Ｂ３", "Ｂ－３", "B_3", "B.3")


# ---------------------------------------------------------------- 蜡块号归一化


def test_block_no_variants_collapse_to_one_key() -> None:
    values = {normalize_block_no(variant) for variant in _BLOCK_VARIANTS}
    assert values == {"B3"}


def test_block_no_strips_leading_zeros_only_in_first_number_group() -> None:
    assert normalize_block_no("B03") == "B3"
    assert normalize_block_no("B003") == "B3"
    assert normalize_block_no("003") == "3"
    assert normalize_block_no("B00") == "B0"          # 全零保留一位，不抹成空串
    assert normalize_block_no("B03A02") == "B3A02"    # 只处理第一段数字


def test_block_no_keeps_distinct_blocks_distinct() -> None:
    assert normalize_block_no("B3") != normalize_block_no("B4")
    assert normalize_block_no("A1") != normalize_block_no("B1")
    assert not same_block_no("B3", "B4")
    assert same_block_no("B-3", "b03")


def test_block_no_strips_trailing_unit_word() -> None:
    assert normalize_block_no("B3号") == "B3"
    assert normalize_block_no("B3块") == "B3"
    assert normalize_block_no("B3蜡块") == "B3"        # 「蜡块」先于「块」剥
    assert normalize_block_no("号") is None            # 单字尾缀不算


def test_block_no_returns_none_for_missing_or_pure_punctuation() -> None:
    assert normalize_block_no(None) is None
    assert normalize_block_no("") is None
    assert normalize_block_no("   ") is None
    assert normalize_block_no("-_-/") is None
    assert normalize_block_no("。。") is None
    assert normalize_block_no("〇") is None             # 中文数字不是编号


def test_block_no_handles_full_width_and_mixed_separators() -> None:
    assert normalize_block_no("Ｂ３") == "B3"
    assert normalize_block_no("Ｂ－３") == "B3"
    assert normalize_block_no("b 3") == "B3"
    assert normalize_block_no("B/3") == "B3"
    assert normalize_block_no("B- 3") == "B3"


def test_block_no_never_raises_and_is_deterministic() -> None:
    for raw in ("", "  ", "---", "B", "3", "Ｂ", "Ω", "!!!", "B3" * 200):
        first = normalize_block_no(raw)
        second = normalize_block_no(raw)
        assert first == second
        assert first is None or isinstance(first, str)


def test_block_no_is_idempotent() -> None:
    for raw in _BLOCK_VARIANTS + ("", "-", "B03号", "Ｂ０３"):
        once = normalize_block_no(raw)
        assert normalize_block_no(once) == once


# ---------------------------------------------------------------- 部位描述归一化


def test_region_desc_strips_whitespace_and_punctuation() -> None:
    assert normalize_region_desc("子宫体内侧") == normalize_region_desc("子宫 体（内侧）")
    assert normalize_region_desc("子宫体内侧") == normalize_region_desc("子宫体内侧,")
    assert normalize_region_desc("子宫体内侧") == normalize_region_desc("子宫体内侧。")


def test_region_desc_merges_direction_words() -> None:
    assert normalize_region_desc("左侧卵巢") == normalize_region_desc("左卵巢")
    assert normalize_region_desc("右侧输卵管") == normalize_region_desc("右输卵管")
    assert normalize_region_desc("左 侧 卵 巢") == normalize_region_desc("左卵巢")
    # 「左」本身是部位名的一部分，不能被并掉
    assert normalize_region_desc("左叶") == "左叶"
    assert normalize_region_desc("左附件") == "左附件"


def test_region_desc_converts_full_width() -> None:
    assert normalize_region_desc("左侧卵巢") == "左卵巢"
    assert normalize_region_desc("子宫体内侧") == normalize_region_desc("子宫体内侧")


def test_region_desc_returns_none_for_missing_or_pure_punctuation() -> None:
    assert normalize_region_desc(None) is None
    assert normalize_region_desc("") is None
    assert normalize_region_desc("   ") is None
    assert normalize_region_desc("（）") is None
    assert normalize_region_desc("...") is None


def test_region_desc_keeps_chinese_content_intact() -> None:
    assert normalize_region_desc("宫颈鳞癌") == "宫颈鳞癌"
    assert normalize_region_desc("子宫内膜") == "子宫内膜"


def test_region_desc_is_idempotent() -> None:
    for raw in ("左侧卵巢", "子宫 体（内侧）", "", "（）", "左附件"):
        once = normalize_region_desc(raw)
        assert normalize_region_desc(once) == once


def test_region_desc_is_deterministic() -> None:
    assert normalize_region_desc("左侧卵巢") == normalize_region_desc("左侧卵巢")


# ---------------------------------------------------------------- 相等判定


def test_same_block_no_treats_missing_as_not_equal() -> None:
    assert same_block_no(None, None) is False
    assert same_block_no("", "B3") is False
    assert same_block_no("B3", None) is False
    assert same_block_no("---", "---") is False


def test_same_region_desc_treats_missing_as_not_equal() -> None:
    assert same_region_desc(None, None) is False
    assert same_region_desc("", "子宫体") is False
    assert same_region_desc("子宫体", None) is False
    assert same_region_desc("左侧卵巢", "左卵巢") is True


# ---------------------------------------------------------------- 对齐接入


def _entry(block_no: str | None, region_desc: str | None = "子宫体") -> ResultEntry:
    return ResultEntry(
        accession_no="ACC-EXAMPLE-0001",
        item_code="IHC-ER",
        value="阴性",
        block_no=block_no,
        region_seq=1,
        region_desc=region_desc,
    )


def _baseline(block_no: str) -> ReportBaseline:
    return ReportBaseline(
        accession_no="ACC-EXAMPLE-0001",
        blocks=(
            Block(
                region_seq=1,
                region_desc="子宫体",
                block_no=block_no,
                ordered_items=("IHC-ER",),
            ),
        ),
    )


def test_l1_match_survives_block_no_formatting_difference() -> None:
    """基线写 B-1、回报写 b01 —— 同一块，必须对上。"""
    outcome = align_entry(_entry(block_no="b01"), _baseline("B-1"))
    assert outcome.state == MatchState.MATCHED
    assert outcome.matched is True
    assert outcome.candidate is not None


def test_l1_match_does_not_bind_a_different_block() -> None:
    outcome = align_entry(_entry(block_no="B2"), _baseline("B-1"))
    assert outcome.state == MatchState.NO_BLOCK
    assert outcome.matched is False


def test_missing_block_no_falls_to_region_level_when_desc_is_unique() -> None:
    """缺蜡块号 + 部位描述唯一命中 → L2 成立（id 6 四级匹配起启用）。

    旧口径只有 L1（蜡块号），缺蜡块号一律挂起；现在部位描述唯一命中时
    可以确定归属，置信「中」，不再让人工白补一次。
    """
    outcome = align_entry(_entry(block_no=None), _baseline("B-1"))
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED
    assert outcome.candidate.block.block_no == "B-1"


def test_missing_block_no_without_region_desc_stays_pending() -> None:
    """缺蜡块号又缺部位描述 → 没有任何定位依据，挂起等人补齐。"""
    outcome = align_entry(
        _entry(block_no=None, region_desc=None), _baseline("B-1")
    )
    assert outcome.state == MatchState.MISSING_BLOCK_NO
    assert outcome.missing_fields == ("block_no", "region_desc")


def test_pure_punctuation_block_no_is_treated_as_missing() -> None:
    """纯符号蜡块号等于没填（归一化层口径）→ 走部位描述层。"""
    outcome = align_entry(_entry(block_no="---"), _baseline("B-1"))
    assert outcome.state == MatchState.MATCHED
    assert outcome.candidate is not None
    assert outcome.candidate.match_level is MatchLevel.L2_REGION_NORMALIZED


@pytest.mark.parametrize(
    "raw",
    ["", " ", "　", "---", "（）", "B3", "b-03"],
)
def test_normalizers_accept_odd_input_without_raising(raw: str) -> None:
    assert normalize_block_no(raw) is None or isinstance(normalize_block_no(raw), str)
    assert normalize_region_desc(raw) is None or isinstance(normalize_region_desc(raw), str)

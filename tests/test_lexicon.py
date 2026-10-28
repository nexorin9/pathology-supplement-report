"""部位同义词典。

覆盖四件事：别名能不能折算到规范词、科室限定怎么取舍、装载失败能不能回退
到内置词表且告警可见、以及本次装载的词典版本与条目数能不能写进运行摘要。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from patho_supplement.align.lexicon import (
    BUILTIN_SOURCE,
    BUILTIN_VERSION,
    Lexicon,
    LexiconEntry,
    LexiconError,
    builtin_lexicon,
    load_lexicon,
)
from patho_supplement.replay import run_replay

#: 词典夹具目录。
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lexicon"


# ---------------------------------------------------------------- 内置最小词表


def test_builtin_lexicon_has_at_least_ten_entry_groups() -> None:
    """内置最小词表不少于 10 组常见取材部位别名。"""
    lexicon = builtin_lexicon()
    assert lexicon.entry_count >= 10
    assert lexicon.alias_count >= 10


def test_builtin_lexicon_covers_the_named_example() -> None:
    """题面点名的「子宫体 / 宫体 / 内膜」必须在内置词表里。"""
    lexicon = builtin_lexicon()
    assert lexicon.canonicalize("宫体") == "子宫体"
    assert lexicon.canonicalize("子宫体") == "子宫体"
    assert lexicon.canonicalize("内膜") == "子宫体"


def test_alias_folds_into_canonical_term() -> None:
    """别名折算到规范词；查不到就是查不到，不猜。"""
    lexicon = builtin_lexicon()
    assert lexicon.canonicalize("乳腺腺体") == "乳腺"
    assert lexicon.canonicalize("乳腺组织") == "乳腺"
    assert lexicon.canonicalize("乳腺") == "乳腺"
    assert lexicon.canonicalize("没听说过的部位") is None
    assert lexicon.canonicalize("") is None
    assert lexicon.canonicalize(None) is None


def test_variants_expand_to_canonical_plus_aliases() -> None:
    """参与打分的词形 = 规范词 + 它的别名（别名折算到规范词参与打分）。"""
    lexicon = builtin_lexicon()
    variants = lexicon.variants("乳腺腺体")
    assert "乳腺" in variants
    assert "乳腺组织" in variants
    assert "乳腺腺体" in variants
    assert lexicon.variants("没听说过的部位") == ()


def test_alias_flag_distinguishes_alias_from_canonical() -> None:
    lexicon = builtin_lexicon()
    assert lexicon.is_alias("宫体") is True
    assert lexicon.is_alias("子宫体") is False


def test_normalization_applies_to_lookup() -> None:
    """查词前先归一化：写法带空格与标点也查得到同一条。"""
    lexicon = builtin_lexicon()
    assert lexicon.canonicalize("乳腺（腺体）") == "乳腺"
    assert lexicon.canonicalize(" 乳腺 腺体 ") == "乳腺"


# ---------------------------------------------------------------- 科室限定


def test_department_scoped_entry_only_applies_in_that_department() -> None:
    """科室限定的别名只在本科室成立。"""
    lexicon = builtin_lexicon()
    # 「壁层胸膜」是胸外科限定条目
    assert lexicon.canonicalize("壁层胸膜", department="胸外科") == "胸膜"
    # 科室不对 / 科室未知 → 不套用这条别名（宁可查不到，也不跨科室套）
    assert lexicon.canonicalize("壁层胸膜", department="普外科") is None
    assert lexicon.canonicalize("壁层胸膜") is None
    # 妇科限定的「宫腔内容物」同理
    assert lexicon.canonicalize("宫腔内容物", department="妇科") == "宫腔"
    assert lexicon.canonicalize("宫腔内容物") is None


def test_unrestricted_entry_applies_in_any_department() -> None:
    lexicon = builtin_lexicon()
    assert lexicon.canonicalize("乳腺腺体", department="普外科") == "乳腺"
    assert lexicon.canonicalize("乳腺腺体", department=None) == "乳腺"


def test_general_entry_wins_over_restricted_one() -> None:
    """同一个词既命中通用条目又命中科室限定条目时，通用条目优先。"""
    general = LexiconEntry(canonical="宫颈", aliases=("子宫颈",))
    restricted = LexiconEntry(canonical="颈", aliases=("子宫颈",), departments=("妇科",))
    merged = Lexicon(entries=(general, restricted), version="test", source="test")
    assert merged.canonicalize("子宫颈") == "宫颈"
    assert merged.canonicalize("子宫颈", department="妇科") == "宫颈"


# ---------------------------------------------------------------- 外置词典加载


def test_load_valid_external_lexicon() -> None:
    """外置 JSON 正常装载：条目、别名、版本都对得上。"""
    lexicon = load_lexicon(FIXTURES / "valid-lexicon.json")
    assert lexicon.entry_count == 3
    assert lexicon.version == "test-2026.10"
    assert lexicon.source.endswith("valid-lexicon.json")
    assert lexicon.warnings == ()
    assert lexicon.canonicalize("乳腺腺体") == "乳腺"
    assert lexicon.canonicalize("宫体", department="妇科") == "子宫体"


def test_load_accepts_top_level_array() -> None:
    """顶层直接是数组也算合法词典。"""
    payload = [{"canonical": "乳腺", "aliases": ["乳腺组织"]}]
    path = FIXTURES / "array-shape.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        lexicon = load_lexicon(path)
        assert lexicon.entry_count == 1
        assert lexicon.canonicalize("乳腺组织") == "乳腺"
    finally:
        path.unlink()


def test_load_without_path_returns_builtin() -> None:
    """不给路径直接用内置词表。"""
    lexicon = load_lexicon()
    assert lexicon.entry_count >= 10
    assert lexicon.version == BUILTIN_VERSION
    assert lexicon.source == BUILTIN_SOURCE
    assert lexicon.warnings == ()


# ---------------------------------------------------------------- 失败回退


@pytest.mark.parametrize(
    "name, reason",
    [
        ("malformed-json.json", "不是合法 JSON"),
        ("wrong-shape.json", "entries 字段必须是数组"),
        ("empty-entries.json", "一条可用条目都没有"),
    ],
)
def test_load_failure_falls_back_to_builtin(name: str, reason: str, caplog) -> None:
    """装载失败回退到内置最小词表，主流程不中断，告警可见。"""
    path = FIXTURES / name

    with caplog.at_level(logging.WARNING, logger="patho_supplement.align.lexicon"):
        lexicon = load_lexicon(path)

    # 回退成功：词典仍然可用
    assert lexicon.entry_count >= 10
    assert lexicon.version == BUILTIN_VERSION
    assert lexicon.source == BUILTIN_SOURCE
    assert lexicon.canonicalize("宫体", department="妇科") == "子宫体"
    # 告警可见：日志里有，且写在词典自身上
    assert lexicon.warnings, "回退必须留下告警"
    assert any(reason in warning for warning in lexicon.warnings)
    assert any("词典" in record.message for record in caplog.records)


def test_missing_file_falls_back_and_says_so(tmp_path: Path) -> None:
    """文件不存在也要回退，且告警要点名是哪个文件。"""
    missing = tmp_path / "不存在的词典.json"
    lexicon = load_lexicon(missing)
    assert lexicon.entry_count >= 10
    assert lexicon.warnings
    assert "不存在的词典.json" in lexicon.warnings[0]


def test_strict_mode_raises_instead_of_falling_back(tmp_path: Path) -> None:
    """严格模式供自检使用：装载失败直接报错，不静默回退。"""
    missing = tmp_path / "不存在的词典.json"
    with pytest.raises(LexiconError):
        load_lexicon(missing, strict=True)


def test_single_bad_entry_does_not_kill_whole_dictionary() -> None:
    """单条写错只跳过这一条，其余条目照常生效。"""
    lexicon = load_lexicon(FIXTURES / "partial-bad-entry.json")
    assert lexicon.entry_count == 2  # 缺 canonical 的那条被跳过
    assert lexicon.canonicalize("肺部") == "肺"
    assert lexicon.canonicalize("乳腺") == "乳腺"
    assert lexicon.warnings, "被跳过的条目要留下告警"


# ---------------------------------------------------------------- 运行摘要


def test_summary_reports_entry_count_and_version() -> None:
    """本次装载的词典条目数与版本标识要能被摘要读到。"""
    lexicon = load_lexicon(FIXTURES / "valid-lexicon.json")
    summary = lexicon.summary()
    assert summary["version"] == "test-2026.10"
    assert summary["entry_count"] == 3
    assert summary["alias_count"] >= 3
    assert summary["source"].endswith("valid-lexicon.json")
    assert summary["warnings"] == []


def test_replay_summary_records_the_lexicon_in_effect(tmp_path: Path) -> None:
    """运行摘要里写明当时生效的词典版本与条目数（对齐结果可复现的前提）。"""
    inbox = Path(__file__).resolve().parent / "fixtures" / "inbox"
    outcome = run_replay(
        inbox,
        output_dir=tmp_path / "out",
        lexicon_path=FIXTURES / "valid-lexicon.json",
    )
    assert outcome.lexicon["version"] == "test-2026.10"
    assert outcome.lexicon["entry_count"] == 3
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary["lexicon"]["version"] == "test-2026.10"
    assert summary["lexicon"]["entry_count"] == 3


def test_replay_summary_records_builtin_when_no_file_given(tmp_path: Path) -> None:
    """不给词典文件时，运行摘要记的是内置词表版本。"""
    inbox = Path(__file__).resolve().parent / "fixtures" / "inbox"
    outcome = run_replay(inbox, output_dir=tmp_path / "out")
    assert outcome.lexicon["version"] == BUILTIN_VERSION
    assert outcome.lexicon["entry_count"] >= 10


def test_replay_with_broken_lexicon_still_completes(tmp_path: Path) -> None:
    """词典坏了也不能让这一轮出不来 —— 回退 + 告警，草稿照常产出。"""
    inbox = Path(__file__).resolve().parent / "fixtures" / "inbox"
    outcome = run_replay(
        inbox,
        output_dir=tmp_path / "out",
        lexicon_path=FIXTURES / "malformed-json.json",
    )
    assert outcome.draft_entry_count > 0
    assert outcome.lexicon["warnings"]

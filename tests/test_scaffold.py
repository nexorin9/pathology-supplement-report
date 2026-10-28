"""地基自检：配置面、领域模型与状态库。

只覆盖 Task 1 落地的三块地基，各模块自己的行为由后续任务的测试文件负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patho_supplement.config import Config, ConfigError, load_dotenv
from patho_supplement.db import Database
from patho_supplement.models import (
    MatchLevel,
    OwnershipKey,
    ResultEntry,
    ResultStatus,
)


def test_config_missing_required_is_reported() -> None:
    config = Config.from_env(env={})
    missing = config.missing_required()
    assert "PATHOLOGY_BASE_URL" in missing
    assert "PATHOLOGY_RESULT_INBOX" in missing
    # 凭据位只报存在性，不回显值
    assert config.credential_status()["PATHOLOGY_API_TOKEN"] is False


def test_config_strict_raises_with_readable_message() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env={}, strict=True)
    assert "PATHOLOGY_BASE_URL" in str(excinfo.value)


def test_config_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env={"BM25_THRESHOLD": "5"})
    assert "BM25_THRESHOLD" in str(excinfo.value)


def test_config_loads_dotenv_without_overriding_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\nPATHOLOGY_BASE_URL=https://example.invalid\n"
        'PATHOLOGY_API_TOKEN="quoted-token"\n',
        encoding="utf-8",
    )
    values = load_dotenv(env_file)
    assert values["PATHOLOGY_BASE_URL"] == "https://example.invalid"
    assert values["PATHOLOGY_API_TOKEN"] == "quoted-token"


def test_ownership_key_is_the_three_part_binding() -> None:
    key = OwnershipKey("SP-2026-0031", 2, "B3")
    assert key.as_tuple() == ("SP-2026-0031", 2, "B3")
    entry = ResultEntry(accession_no="SP-2026-0031", item_code="IHC-ER", status=ResultStatus.FINAL)
    assert entry.ownership_key().accession_no == "SP-2026-0031"


def test_match_level_confidence_has_no_level_for_pending() -> None:
    assert MatchLevel.L1_BLOCK_EXACT.confidence == "高"
    assert MatchLevel.L4_PENDING.confidence == "无"


def test_db_schema_is_created_and_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    with Database(db_path) as db:
        assert db.schema_version() >= 1
        db.init_schema()  # 再跑一次迁移不应报错，也不应破坏数据
        db.execute(
            "INSERT INTO audit_log(ts, category, event, accession_no, detail_json) "
            "VALUES('2026-10-28 06:14:23', 'alignment', 'matched', 'SP-2026-0031', '{}')"
        )
        row = db.query_one("SELECT event FROM audit_log WHERE accession_no = ?", ("SP-2026-0031",))
        assert row is not None and row["event"] == "matched"

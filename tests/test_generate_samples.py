"""样例合成生成器的验收。

生成器本身是「造数据的工具」，工具的验收标准跟产品不一样：不是跑得通，而是
**可重跑**（同一种子两次生成逐字节相同）、**形态齐**（该有的脏数据形态都有）、
**规模够**（能放大到院内一天的量）、**不出界**（不夹带患者可识别明细）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

from patho_supplement.replay import run_replay

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATOR_PATH = PROJECT_ROOT / "tests" / "fixtures" / "generate_samples.py"


def _load_generator():
    """按路径加载生成器脚本（它不是包里的模块，是个可执行脚本）。"""
    spec = importlib.util.spec_from_file_location("generate_samples", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在定义时按 ``cls.__module__`` 回查模块 —— 必须先登记进
    # ``sys.modules``，否则加载会炸在 ``_is_type`` 上。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


def test_same_seed_produces_identical_dataset(tmp_path):
    """同一 seed 两次生成：目录指纹与清单都逐字节相同。"""
    first_root, first = generator.generate(out=tmp_path / "a", flat=True, seed=42)
    second_root, second = generator.generate(out=tmp_path / "b", flat=True, seed=42)

    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert generator.dataset_fingerprint(first_root) == generator.dataset_fingerprint(
        second_root
    )
    # 清单本身也必须一致（指纹不把清单算进去，清单单独比）。
    assert (first_root / "manifest.json").read_bytes() == (
        second_root / "manifest.json"
    ).read_bytes()


def test_different_seed_produces_different_dataset(tmp_path):
    """换个种子就该换一批数据 —— 否则「可重跑」就成了「只有一种数据」。"""
    _, first = generator.generate(out=tmp_path / "a", flat=True, seed=42)
    _, second = generator.generate(out=tmp_path / "b", flat=True, seed=7)
    assert first["dataset_sha256"] != second["dataset_sha256"]


def test_default_scale_matches_contract(tmp_path):
    """默认规模：3 份报告，ORU 落在 24–30 条之间，另加 2 条 ORM。"""
    _, manifest = generator.generate(out=tmp_path / "gen", flat=True, seed=42)
    assert manifest["reports"] == 3
    assert 24 <= manifest["oru_messages"] <= 30
    assert manifest["orm_messages"] == 2


def test_every_required_shape_is_present(tmp_path):
    """九种形态逐条在案：别名 / 缺蜡块号 / 重复投递 / 更正 / 倒挂 / 编码 / 空值 / 超长。"""
    _, manifest = generator.generate(out=tmp_path / "gen", flat=True, seed=42)
    counts = manifest["shape_counts"]
    for shape in (
        generator.SHAPE_REGION_ALIAS,
        generator.SHAPE_MISSING_BLOCK,
        generator.SHAPE_DUPLICATE,
        generator.SHAPE_CORRECTION,
        generator.SHAPE_INVERTED,
        generator.SHAPE_GBK,
        generator.SHAPE_MIXED,
        generator.SHAPE_EMPTY_VALUE,
        generator.SHAPE_OVERLONG,
    ):
        assert counts.get(shape, 0) >= 1, f"形态没覆盖：{shape}"


def test_generated_dataset_runs_through_product(tmp_path):
    """生成的数据能跑通产品真路径：成稿、重复、更正、倒挂、脏编码各走一遍。"""
    root, manifest = generator.generate(out=tmp_path / "gen", flat=True, seed=42)
    outcome = run_replay(root, output_dir=tmp_path / "out")

    assert outcome.malformed == 0
    assert outcome.drafts, "一��草稿都没出"
    assert outcome.duplicates >= 1, "重复投递没被认出来"
    assert outcome.corrections_applied >= 1, "更正没生效"
    assert outcome.inverted_corrections >= 1, "回报时间倒挂没被扣下来"
    assert outcome.decode_counts.get("gbk", 0) >= 1
    assert outcome.decode_counts.get("mixed", 0) >= 1
    assert outcome.placeholder_count >= 1, "已申请未回报的项没占位"
    # 生效结果只有两个去向：进草稿或进待人工处理清单。
    assert outcome.effective_result_count == outcome.draft_entry_count + len(
        outcome.pending
    )
    assert manifest["dataset_sha256"]


def test_scale_option_reaches_target_volume(tmp_path):
    """``--scale`` 能放大到 2000 条 ORU / 约 300 份报告。"""
    _, manifest = generator.generate(out=tmp_path / "big", flat=True, seed=42, scale=2000)
    assert manifest["oru_messages"] == 2000
    assert manifest["reports"] >= 250
    counts = manifest["shape_counts"]
    assert counts.get(generator.SHAPE_REGION_ALIAS, 0) >= 1
    assert counts.get(generator.SHAPE_OVERLONG, 0) >= 1


def test_generated_files_carry_no_patient_identifiers(tmp_path):
    """数据边界：生成的文件里没有姓名 / 身份证 / 手机号一类的可识别明细。"""
    root, _ = generator.generate(out=tmp_path / "gen", flat=True, seed=42)
    # 身份证号（18 位）与手机号（11 位）的连续数字串。
    id_pattern = re.compile(r"\b\d{15}(\d{2}[\dxX])?\b")
    phone_pattern = re.compile(r"\b1[3-9]\d{9}\b")
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        assert not id_pattern.search(text), f"{path.name} 里有疑似身份证号的数字串"
        assert not phone_pattern.search(text), f"{path.name} 里有疑似手机号的数字串"
        assert "PID|" not in text, f"{path.name} 里有患者标识段"


def test_generator_only_uses_standard_library():
    """断网降级的前提：只用标准库，不装第三方依赖也能跑。"""
    tree = ast.parse(GENERATOR_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "argparse",
        "hashlib",
        "json",
        "random",
        "sys",
        "tempfile",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
    }, f"引入了非标准库依赖：{sorted(imported - {'__future__'})}"


def test_manifest_is_self_describing(tmp_path):
    """清单要说清种子、规模、形态与逐文件指纹 —— 别人才敢拿它当基准。"""
    root, manifest = generator.generate(out=tmp_path / "gen", flat=True, seed=42)
    written = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert written["seed"] == 42
    assert written["oru_messages"] == manifest["oru_messages"]
    names = {entry["name"] for entry in written["files"]}
    assert names == {path.name for path in root.iterdir() if path.name != "manifest.json"}
    for entry in written["files"]:
        assert len(entry["sha256"]) == 64

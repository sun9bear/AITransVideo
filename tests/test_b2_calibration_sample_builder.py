import json
from pathlib import Path

from scripts.b2_calibration_sample_builder import (
    B2_DEFAULT_CANDIDATES,
    PRIMARY_SCRIPT,
    SECONDARY_SCRIPT,
    build_samples,
)


def test_default_candidates_count() -> None:
    assert len(B2_DEFAULT_CANDIDATES) == 27


def test_default_candidates_are_sorted_and_unique() -> None:
    assert B2_DEFAULT_CANDIDATES == sorted(set(B2_DEFAULT_CANDIDATES))


def test_b1_anchors_included_in_default_candidates() -> None:
    anchors = {
        "longanyang", "longanhuan", "longhuhu_v3", "longanzhi_v3",
        "longyingjing_v3", "longlaobo_v3", "longlaoyi_v3", "longjielidou_v3",
        "longanwen_v3", "longxiaoxia_v3", "longanyun_v3", "longcheng_v3",
    }
    for a in anchors:
        assert a in B2_DEFAULT_CANDIDATES, f"B1 anchor {a} missing from B2 candidates"


def test_child_voices_all_included() -> None:
    child_voices = {
        "longhuhu_v3", "longpaopao_v3", "longjielidou_v3",
        "longxian_v3", "longling_v3", "longshanshan_v3", "longniuniu_v3",
    }
    for c in child_voices:
        assert c in B2_DEFAULT_CANDIDATES, f"child voice {c} missing from B2 candidates"


def test_dialect_voices_excluded_from_default_candidates() -> None:
    dialect = {"longjiaxin_v3", "longjiayi_v3", "longanyue_v3", "longlaotie_v3", "longshange_v3", "longanmin_v3"}
    for d in dialect:
        assert d not in B2_DEFAULT_CANDIDATES, f"dialect voice {d} in B2 candidates"


def test_primary_script_length_in_range() -> None:
    assert 40 <= len(PRIMARY_SCRIPT) <= 80


def test_secondary_script_length_in_range() -> None:
    assert 30 <= len(SECONDARY_SCRIPT) <= 80


def test_build_samples_dry_run(tmp_path: Path) -> None:
    output_dir = tmp_path / "samples"
    manifest = build_samples(
        output_dir=output_dir,
        voices=["longanyang", "longanhuan"],
        dry_run=True,
    )
    assert len(manifest) == 2
    assert manifest["longanyang"]["dry_run"] is True
    assert manifest["longanyang"]["primary"] is not None
    assert manifest["longanyang"]["secondary"] is not None


def test_build_samples_with_mock_helper(tmp_path: Path) -> None:
    """Test with a mock helper that writes a minimal WAV file."""
    output_dir = tmp_path / "samples"
    helper = tmp_path / "mock_helper.py"
    # Mock helper: reads request, writes a tiny file, prints success JSON
    helper.write_text(
        'import json, sys\n'
        'req = json.load(open(sys.argv[1], encoding="utf-8"))\n'
        'out = req["output_path"]\n'
        'with open(out, "wb") as f:\n'
        '    f.write(b"RIFF" + b"\\x00" * 1100)\n'
        'print(json.dumps({"ok": True, "output_path": out, "bytes": 1104}))\n',
        encoding="utf-8",
    )

    manifest = build_samples(
        output_dir=output_dir,
        voices=["longanyang"],
        helper_script=helper,
    )

    assert manifest["longanyang"]["error"] is None
    assert manifest["longanyang"]["primary"] is not None
    primary_path = Path(manifest["longanyang"]["primary"])
    assert primary_path.exists()
    assert primary_path.stat().st_size > 1000


def test_manifest_json_written(tmp_path: Path) -> None:
    output_dir = tmp_path / "samples"
    build_samples(output_dir=output_dir, voices=["longanyang"], dry_run=True)
    # build_samples itself doesn't write manifest; the CLI main() does.
    # But we can verify the return value is serializable.
    manifest = build_samples(output_dir=output_dir, voices=["test_voice"], dry_run=True)
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "test_voice" in serialized

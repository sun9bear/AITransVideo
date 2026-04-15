"""Tests for services.calibration_persistence.

Bug context (Job 6673, 2026-04-15): cache-hit reruns lost probe cps
because it lived only in TTSGenerator memory. This module persists +
reloads the probe calibration so cloned voices (which never hit
voice_catalog) keep their real cps across pipeline restarts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.calibration_persistence import (
    CALIBRATION_FILENAME,
    CALIBRATION_SCHEMA_VERSION,
    load_probe_calibration,
    persist_probe_calibration,
)


# ----- persist + load roundtrip -----

def test_persist_then_load_roundtrip(tmp_path: Path) -> None:
    persist_probe_calibration(
        tmp_path,
        cps_global=3.34,
        cps_by_speaker={"speaker_a": 3.34, "speaker_b": 4.12},
        speaker_voices={
            "speaker_a": "vt_speaker_a_1776252490214",
            "speaker_b": "Chinese_Female_Anchor_001",
        },
    )

    cache_path = tmp_path / CALIBRATION_FILENAME
    assert cache_path.exists()

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["version"] == CALIBRATION_SCHEMA_VERSION
    assert saved["global_chars_per_second"] == 3.34
    assert saved["chars_per_second_by_speaker"] == {"speaker_a": 3.34, "speaker_b": 4.12}
    assert saved["speaker_voice_ids"]["speaker_a"] == "vt_speaker_a_1776252490214"
    assert "calibrated_at" in saved

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={
            "speaker_a": "vt_speaker_a_1776252490214",
            "speaker_b": "Chinese_Female_Anchor_001",
        },
    )
    assert g == pytest.approx(3.34)
    assert by == {"speaker_a": 3.34, "speaker_b": 4.12}


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    g, by = load_probe_calibration(tmp_path)
    assert g is None
    assert by == {}


# ----- voice_id invalidation (the cloned-voice safety net) -----

def test_load_invalidates_when_speaker_voice_changed(tmp_path: Path) -> None:
    """User re-cloned speaker_a → cache must be discarded so pipeline re-probes."""
    persist_probe_calibration(
        tmp_path,
        cps_global=3.34,
        cps_by_speaker={"speaker_a": 3.34},
        speaker_voices={"speaker_a": "vt_speaker_a_OLD"},
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={"speaker_a": "vt_speaker_a_NEW"},  # different
    )
    assert g is None
    assert by == {}


def test_load_invalidates_when_new_speaker_added(tmp_path: Path) -> None:
    """User added speaker_b after probe — cache only has speaker_a, invalidate."""
    persist_probe_calibration(
        tmp_path,
        cps_global=3.34,
        cps_by_speaker={"speaker_a": 3.34},
        speaker_voices={"speaker_a": "vt_speaker_a_xxx"},
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={
            "speaker_a": "vt_speaker_a_xxx",
            "speaker_b": "newly_added_voice",  # not in cache
        },
    )
    assert g is None
    assert by == {}


def test_load_succeeds_when_voices_unchanged(tmp_path: Path) -> None:
    """Same voice_ids → cache valid → cps reused."""
    persist_probe_calibration(
        tmp_path,
        cps_global=4.12,
        cps_by_speaker={"speaker_a": 4.12, "speaker_b": 3.78},
        speaker_voices={"speaker_a": "v1", "speaker_b": "v2"},
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={"speaker_a": "v1", "speaker_b": "v2"},
    )
    assert g == pytest.approx(4.12)
    assert by == {"speaker_a": 4.12, "speaker_b": 3.78}


def test_load_succeeds_when_no_expected_voices_passed(tmp_path: Path) -> None:
    """expected_voices=None bypasses voice validation. Useful for legacy or
    diagnostic callers that just want whatever cps was last calibrated."""
    persist_probe_calibration(
        tmp_path,
        cps_global=4.12,
        cps_by_speaker={"speaker_a": 4.12},
        speaker_voices={"speaker_a": "anything"},
    )

    g, by = load_probe_calibration(tmp_path)  # no validation
    assert g == pytest.approx(4.12)
    assert by == {"speaker_a": 4.12}


# ----- defensive paths -----

def test_persist_skips_invalid_global_cps(tmp_path: Path) -> None:
    """Don't lock in garbage cps. None / 0 / negative all skip persisting."""
    persist_probe_calibration(tmp_path, cps_global=None, cps_by_speaker=None)  # type: ignore[arg-type]
    assert not (tmp_path / CALIBRATION_FILENAME).exists()

    persist_probe_calibration(tmp_path, cps_global=0.0, cps_by_speaker=None)
    assert not (tmp_path / CALIBRATION_FILENAME).exists()

    persist_probe_calibration(tmp_path, cps_global=-1.5, cps_by_speaker=None)
    assert not (tmp_path / CALIBRATION_FILENAME).exists()


def test_persist_filters_out_invalid_per_speaker_values(tmp_path: Path) -> None:
    """Speakers with None / 0 / negative cps don't get into the cache."""
    persist_probe_calibration(
        tmp_path,
        cps_global=4.0,
        cps_by_speaker={
            "speaker_a": 4.0,
            "speaker_b": 0,         # filtered
            "speaker_c": -1.0,      # filtered
            "speaker_d": None,      # filtered  # type: ignore[dict-item]
            "speaker_e": 5.5,
        },
    )

    g, by = load_probe_calibration(tmp_path)
    assert g == pytest.approx(4.0)
    assert by == {"speaker_a": 4.0, "speaker_e": 5.5}


def test_load_handles_corrupt_json(tmp_path: Path) -> None:
    """Garbage on disk → log + return defaults, don't raise."""
    (tmp_path / CALIBRATION_FILENAME).write_text("{not json", encoding="utf-8")

    g, by = load_probe_calibration(tmp_path)
    assert g is None
    assert by == {}


def test_load_handles_wrong_top_level_type(tmp_path: Path) -> None:
    """File is valid JSON but a list instead of a dict — graceful return."""
    (tmp_path / CALIBRATION_FILENAME).write_text('[1, 2, 3]', encoding="utf-8")

    g, by = load_probe_calibration(tmp_path)
    assert g is None
    assert by == {}


def test_load_invalidates_on_version_mismatch(tmp_path: Path) -> None:
    """Future schema changes: bump CALIBRATION_SCHEMA_VERSION → old caches discarded."""
    (tmp_path / CALIBRATION_FILENAME).write_text(json.dumps({
        "version": CALIBRATION_SCHEMA_VERSION + 999,  # future schema
        "global_chars_per_second": 4.0,
        "chars_per_second_by_speaker": {"speaker_a": 4.0},
        "speaker_voice_ids": {"speaker_a": "v1"},
    }), encoding="utf-8")

    g, by = load_probe_calibration(tmp_path)
    assert g is None
    assert by == {}


def test_load_handles_invalid_cps_in_per_speaker_dict(tmp_path: Path) -> None:
    """Saved per-speaker dict contains string / null entries — skip them."""
    (tmp_path / CALIBRATION_FILENAME).write_text(json.dumps({
        "version": CALIBRATION_SCHEMA_VERSION,
        "global_chars_per_second": 4.0,
        "chars_per_second_by_speaker": {
            "speaker_a": 4.0,
            "speaker_b": "not a number",
            "speaker_c": None,
        },
        "speaker_voice_ids": {},
    }), encoding="utf-8")

    g, by = load_probe_calibration(tmp_path)
    assert g == pytest.approx(4.0)
    assert by == {"speaker_a": 4.0}  # b/c filtered out


# ----- multi-speaker scenarios (the "user asked about" coverage) -----

def test_multi_speaker_all_cloned_roundtrip(tmp_path: Path) -> None:
    """Common scenario: user clones 3 speakers, none in catalog. All persist."""
    persist_probe_calibration(
        tmp_path,
        cps_global=3.50,
        cps_by_speaker={
            "speaker_a": 3.34,
            "speaker_b": 4.12,
            "speaker_c": 3.78,
        },
        speaker_voices={
            "speaker_a": "vt_speaker_a_111",
            "speaker_b": "vt_speaker_b_222",
            "speaker_c": "vt_speaker_c_333",
        },
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={
            "speaker_a": "vt_speaker_a_111",
            "speaker_b": "vt_speaker_b_222",
            "speaker_c": "vt_speaker_c_333",
        },
    )
    assert g == pytest.approx(3.50)
    assert by == {"speaker_a": 3.34, "speaker_b": 4.12, "speaker_c": 3.78}


def test_multi_speaker_one_recloned_invalidates_all(tmp_path: Path) -> None:
    """User re-cloned only speaker_b. Conservative all-or-nothing invalidation:
    cache is discarded, pipeline re-probes everyone. (Per-speaker partial
    validity would be nicer but is not needed for current scope.)"""
    persist_probe_calibration(
        tmp_path,
        cps_global=3.50,
        cps_by_speaker={
            "speaker_a": 3.34,
            "speaker_b": 4.12,
            "speaker_c": 3.78,
        },
        speaker_voices={
            "speaker_a": "vt_speaker_a_111",
            "speaker_b": "vt_speaker_b_222_OLD",
            "speaker_c": "vt_speaker_c_333",
        },
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={
            "speaker_a": "vt_speaker_a_111",
            "speaker_b": "vt_speaker_b_222_NEW",  # only this changed
            "speaker_c": "vt_speaker_c_333",
        },
    )
    assert g is None
    assert by == {}


def test_multi_speaker_mixed_clone_and_system_voice(tmp_path: Path) -> None:
    """Hybrid scenario: speaker_a clone, speaker_b/c system voices.
    All three get persisted, all three reload."""
    persist_probe_calibration(
        tmp_path,
        cps_global=4.0,
        cps_by_speaker={
            "speaker_a": 3.34,  # clone
            "speaker_b": 4.50,  # system voice
            "speaker_c": 4.20,  # system voice
        },
        speaker_voices={
            "speaker_a": "vt_speaker_a_xxx",
            "speaker_b": "Chinese_Female_Anchor",
            "speaker_c": "Chinese_Male_Reporter",
        },
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={
            "speaker_a": "vt_speaker_a_xxx",
            "speaker_b": "Chinese_Female_Anchor",
            "speaker_c": "Chinese_Male_Reporter",
        },
    )
    assert g == pytest.approx(4.0)
    assert by == {"speaker_a": 3.34, "speaker_b": 4.50, "speaker_c": 4.20}


def test_persist_overwrites_existing_cache(tmp_path: Path) -> None:
    """Re-running probe should overwrite the cache, not merge or append."""
    persist_probe_calibration(
        tmp_path,
        cps_global=3.0,
        cps_by_speaker={"speaker_a": 3.0},
        speaker_voices={"speaker_a": "v1"},
    )
    persist_probe_calibration(
        tmp_path,
        cps_global=5.0,  # new value
        cps_by_speaker={"speaker_a": 5.0, "speaker_b": 5.5},  # new speaker added
        speaker_voices={"speaker_a": "v1", "speaker_b": "v2"},
    )

    g, by = load_probe_calibration(
        tmp_path,
        expected_voices={"speaker_a": "v1", "speaker_b": "v2"},
    )
    assert g == pytest.approx(5.0)
    assert by == {"speaker_a": 5.0, "speaker_b": 5.5}

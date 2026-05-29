"""Phase 1 (plan 2026-05-29 free-tier): voiceclone spike harness smoke test.

Mocks the provider + reference extraction — never hits the real API or ffmpeg.
"""

import json

import scripts.spike.mimo_voiceclone_spike as spike


def test_run_spike_smoke(tmp_path, monkeypatch):
    (tmp_path / "translation").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "speech_for_asr.wav").write_bytes(b"\x00" * 100)
    segs = {"segments": [
        {"segment_id": 1, "speaker_id": "speaker_a", "start_ms": 0, "end_ms": 6000, "cn_text": "你好测试"},
    ]}
    (tmp_path / "translation" / "segments.json").write_text(json.dumps(segs), encoding="utf-8")

    (tmp_path / "ref.wav").write_bytes(b"\x00" * 200)
    monkeypatch.setattr(spike, "extract_speaker_references",
                        lambda *a, **k: {"speaker_a": tmp_path / "ref.wav"})
    monkeypatch.setattr(spike, "synthesize_voiceclone", lambda *a, **k: b"\x00" * 5000)

    report = spike.run_spike(str(tmp_path), max_segments=1, out_dir=str(tmp_path / "spike_out"))
    assert report["attempted"] == 1
    assert report["succeeded"] == 1
    assert report["failed"] == 0
    assert report["results"][0]["out_bytes"] == 5000
    assert (tmp_path / "spike_out").exists()
    assert (tmp_path / "spike_out" / "report.json").exists()


def test_run_spike_records_no_reference(tmp_path, monkeypatch):
    (tmp_path / "translation").mkdir()
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "speech_for_asr.wav").write_bytes(b"\x00" * 100)
    segs = {"segments": [
        {"segment_id": 2, "speaker_id": "speaker_b", "start_ms": 0, "end_ms": 6000, "cn_text": "测试"},
    ]}
    (tmp_path / "translation" / "segments.json").write_text(json.dumps(segs), encoding="utf-8")
    # No reference returned for speaker_b -> segment recorded as failed/no_reference
    monkeypatch.setattr(spike, "extract_speaker_references", lambda *a, **k: {})
    monkeypatch.setattr(spike, "synthesize_voiceclone", lambda *a, **k: b"\x00" * 5000)

    report = spike.run_spike(str(tmp_path), max_segments=1, out_dir=str(tmp_path / "out"))
    assert report["attempted"] == 1
    assert report["succeeded"] == 0
    assert report["results"][0]["error"] == "no_reference"

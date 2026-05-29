"""Free-tier Phase 1 internal spike: run MiMo voiceclone over a real job_dir and
record latency / failure / usage / output (plan 2026-05-29).

Manual developer tool only — requires MIMO_API_KEY. NOT for CI / automatic
paths (paid-API compliance: MiMo voiceclone is free now, but only ever invoked
by an explicit developer/allowlist run).

Usage:
  python -m scripts.spike.mimo_voiceclone_spike <job_dir> --max-segments 8
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from services.tts.mimo_tts_provider import synthesize_voiceclone
from services.tts.voiceclone_reference import extract_speaker_references


def _load_segments(job_dir: Path) -> list[dict]:
    raw = json.loads((job_dir / "translation" / "segments.json").read_text(encoding="utf-8"))
    return raw.get("segments", raw if isinstance(raw, list) else [])


def run_spike(job_dir: str, *, max_segments: int = 8, out_dir: str | None = None) -> dict:
    job = Path(job_dir)
    out = Path(out_dir) if out_dir else (job / "reports" / "voiceclone_spike")
    out.mkdir(parents=True, exist_ok=True)
    segments = _load_segments(job)
    refs = extract_speaker_references(
        segments, job / "audio" / "speech_for_asr.wav", out / "refs"
    )
    results: list[dict] = []
    targets = [s for s in segments if (s.get("cn_text") or "").strip()][:max_segments]
    for s in targets:
        spk = s.get("speaker_id")
        rec: dict = {"segment_id": s.get("segment_id"), "speaker_id": spk, "ok": False}
        ref = refs.get(spk)
        if ref is None:
            rec["error"] = "no_reference"
            results.append(rec)
            continue
        t0 = time.time()
        try:
            audio = synthesize_voiceclone(s["cn_text"].strip(), reference_audio=ref)
            (out / f"seg_{s.get('segment_id')}.wav").write_bytes(audio)
            rec.update(ok=True, out_bytes=len(audio), latency_s=round(time.time() - t0, 2))
        except Exception as exc:  # noqa: BLE001 — record + continue batch
            rec.update(error=repr(exc)[:200], latency_s=round(time.time() - t0, 2))
        results.append(rec)
    oks = [r for r in results if r["ok"]]
    lat = sorted(r["latency_s"] for r in oks)
    report = {
        "job_dir": str(job),
        "attempted": len(results),
        "succeeded": len(oks),
        "failed": len(results) - len(oks),
        "latency_p50": lat[len(lat) // 2] if lat else None,
        "latency_max": lat[-1] if lat else None,
        "results": results,
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir")
    ap.add_argument("--max-segments", type=int, default=8)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    rep = run_spike(args.job_dir, max_segments=args.max_segments, out_dir=args.out_dir)
    print(json.dumps({k: v for k, v in rep.items() if k != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

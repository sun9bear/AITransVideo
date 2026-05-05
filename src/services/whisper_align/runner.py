"""Subprocess entry: load faster-whisper, transcribe one WAV, dump JSON.

Phase C of 2026-05-04-subtitle-audio-sync-plan.

Invoked by the parent process via:
    python -m services.whisper_align.runner --wav <path> --language zh --model small

Output (stdout, single JSON line):
    {"words": [{"start_ms": int, "end_ms": int, "text": str}, ...],
     "duration_ms": int}

Lives in its own subprocess so the ~1.5GB ``small``/INT8 model exits
with this process. Never pin model RAM in the long-lived Job-API or
runner processes.

faster-whisper is imported LAZILY inside main() — module-level imports
stay free of the dependency so the parent process can import this
module (e.g. to discover the entry point) without pulling in
faster_whisper or its CTranslate2 backend.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> None:
    """Subprocess entry. Parses args, transcribes, dumps JSON, exits 0/non-0."""
    ap = argparse.ArgumentParser(description="faster-whisper word-timestamp runner")
    ap.add_argument("--wav", required=True,
                    help="absolute path to WAV file to transcribe")
    ap.add_argument("--language", default="zh",
                    help="faster-whisper language code, e.g. zh / en")
    ap.add_argument("--model", default="small",
                    help="model size: tiny | base | small | medium | large-v3")
    args = ap.parse_args()

    # Cap CPU usage so we don't starve other pipelines on the 4-core US host.
    # Caller can override before spawning the subprocess if they want more
    # parallelism; default protects shared workloads.
    os.environ.setdefault("OMP_NUM_THREADS", "2")

    # Lazy import: only here, never at module import time. Keeps the parent
    # process free of the faster_whisper / CTranslate2 / huggingface_hub
    # transitive load.
    from faster_whisper import WhisperModel  # noqa: PLC0415

    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        args.wav,
        language=args.language,
        word_timestamps=True,
        # Deterministic enough for tests — beam=1 disables sampling, vad
        # off so we don't lose words to overzealous voice-activity gating.
        beam_size=1,
        vad_filter=False,
    )

    words: list[dict] = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({
                "start_ms": int((w.start or 0.0) * 1000),
                "end_ms": int((w.end or 0.0) * 1000),
                "text": w.word or "",
            })

    payload = {
        "words": words,
        "duration_ms": int((info.duration or 0.0) * 1000),
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()

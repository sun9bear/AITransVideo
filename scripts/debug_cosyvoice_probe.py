from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path


def _bootstrap_repo() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe CosyVoice synthesis stability.")
    parser.add_argument("--voice", default="longanyang")
    parser.add_argument("--model", default="cosyvoice-v3-flash")
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--text", default="Hello from CosyVoice probe.")
    args = parser.parse_args()

    _bootstrap_repo()
    import importlib

    cosyvoice_provider = importlib.import_module("src.services.tts.cosyvoice_provider")

    successes = 0
    failures = 0
    timings: list[float] = []

    print(
        f"probe_start attempts={args.attempts} voice={args.voice} model={args.model}",
        flush=True,
    )

    for idx in range(1, args.attempts + 1):
        started = time.perf_counter()
        try:
            audio = cosyvoice_provider.synthesize(
                text=args.text,
                voice=args.voice,
                model=args.model,
            )
            elapsed = time.perf_counter() - started
            timings.append(elapsed)
            successes += 1
            print(
                f"attempt={idx} result=ok bytes={len(audio)} elapsed_s={elapsed:.3f}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - probe script wants raw failure info
            elapsed = time.perf_counter() - started
            failures += 1
            print(
                f"attempt={idx} result=fail elapsed_s={elapsed:.3f} error={exc}",
                flush=True,
            )

    avg = statistics.mean(timings) if timings else 0.0
    print(
        f"probe_done successes={successes} failures={failures} avg_success_elapsed_s={avg:.3f}",
        flush=True,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Calibrate per-voice speech rate (chars/sec) against all three TTS engines.

Part of the translation-duration-alignment plan Phase 1. Writes results into
`voice_catalog.chars_per_second`, `voice_catalog.chars_per_second_by_model`,
and `voice_catalog.speed_calibrated_at` (added by migration 012).

IMPORTANT — This script calls PAID TTS APIs. Per project CLAUDE.md, paid API
calls must be explicitly triggered by the user. The script DEFAULTS to dry-run
and refuses to make real TTS calls unless `--execute` is passed.

Cost (one-time, per full run):
    MiniMax speech-2.8-turbo   : 81 voices × 3 texts = ¥14.58
    MiniMax speech-2.8-hd      : 81 voices × 3 texts = ¥25.52
    CosyVoice cosyvoice-v3-flash: 65 voices × 3 texts = ¥5.85
    VolcEngine seed-tts-2.0    : 33 voices × 3 texts = ¥4.46
    Total                      : ¥50.4  (780 TTS calls, ~40 min with RPM limits)

Usage:
    # 1. Preview plan (no API calls, no DB writes):
    python gateway/scripts/calibrate_voice_speeds.py --dry-run

    # 2. Actually calibrate (PAID, requires user confirmation):
    DATABASE_URL=postgresql+asyncpg://... \\
        python gateway/scripts/calibrate_voice_speeds.py --execute

    # 3. Single-voice test (for debugging / re-calibration):
    DATABASE_URL=postgresql+asyncpg://... \\
        python gateway/scripts/calibrate_voice_speeds.py --execute \\
            --provider minimax --voice-id Chinese_Male_1 --model speech-2.8-turbo

    # 4. Force re-calibrate (overwrite existing values):
    DATABASE_URL=postgresql+asyncpg://... \\
        python gateway/scripts/calibrate_voice_speeds.py --execute --force

CLI flags:
    --dry-run         : Default. Show what would be calibrated, make NO API calls.
    --execute         : Make real paid API calls and write DB. Requires explicit use.
    --provider NAME   : Only calibrate voices for one provider (minimax/cosyvoice/volcengine).
    --model NAME      : Only calibrate for one model (e.g. speech-2.8-hd). Must pair with --provider.
    --voice-id ID     : Only calibrate a single voice (must pair with --provider).
    --force           : Re-calibrate even if speed_calibrated_at is already set.
    --limit N         : Cap at N voices per provider (for sampling / testing).
    --output-csv PATH : Write results to CSV in addition to DB.

Environment variables (required for --execute):
    DATABASE_URL or AVT_DATABASE_URL — async PostgreSQL connection string.
    MINIMAX_API_KEY, DASHSCOPE_API_KEY, VOLCENGINE_TTS_APP_ID+ACCESS_KEY —
        per-provider credentials (same as the main runtime).

Result merge semantics:
    chars_per_second_by_model is merged, not replaced. Running --model speech-2.8-hd
    only updates the speech-2.8-hd key; other models' values are preserved.
    The scalar chars_per_second is recomputed as the average of all calibrated
    models for that voice.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Ensure src and gateway are importable (same trick as seed_voice_catalog.py)
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_repo_root / "gateway"))

from standard_calibration_texts import STANDARD_TEXTS, count_hanzi  # noqa: E402


# ---------------------------------------------------------------------------
# Calibration targets
# ---------------------------------------------------------------------------

# (provider, model, rpm)
# rpm values from src/services/tts/tts_strategy.py _PROVIDER_RPM, conservative.
CALIBRATION_TARGETS: list[tuple[str, str, int]] = [
    ("minimax",    "speech-2.8-turbo",    20),
    ("minimax",    "speech-2.8-hd",       20),
    ("cosyvoice",  "cosyvoice-v3-flash",  60),   # API allows 180; be conservative
    ("volcengine", "seed-tts-2.0",        30),
]


# Sanity bounds — calibration rejected if outside this range (matches process.py:4066).
MIN_VALID_CPS = 2.0
MAX_VALID_CPS = 8.0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple per-provider interval throttle. Not thread-safe; script is sync."""

    def __init__(self, rpm: int) -> None:
        self.min_interval = 60.0 / max(1, rpm)
        self._last = 0.0

    def wait(self) -> None:
        now = time.time()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.time()


# ---------------------------------------------------------------------------
# ffprobe duration measurement
# ---------------------------------------------------------------------------

def _measure_wav_duration_ms(wav_bytes: bytes) -> int:
    """Measure duration of WAV bytes via ffprobe, returned in ms."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        path = f.name
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, check=True,
        )
        seconds = float(result.stdout.strip())
        return int(round(seconds * 1000))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-provider synthesis wrappers — reuse existing provider modules
# ---------------------------------------------------------------------------

def _synthesize_minimax(text: str, voice_id: str, model: str) -> bytes:
    """Call MiniMax TTS via the existing HTTP client in tts_generator.

    Key sourced from env in this order (first non-empty wins):
      1. MINIMAX_API_KEY — dedicated name some deployments use
      2. AUTODUB_TTS_API_KEY — the generic slot set in production .env
         (autodub.local.json points ``api_key_env_var`` to it)
    """
    from services.tts.tts_generator import _post_json, _build_tts_endpoint

    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or os.environ.get("AUTODUB_TTS_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY or AUTODUB_TTS_API_KEY not set")

    endpoint = _build_tts_endpoint("https://api.minimaxi.com")
    payload = {
        "model": model,
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1.0,
            "vol": 1.0,
        },
        "audio_setting": {
            "format": "wav",
            "sample_rate": 24000,
        },
    }
    response = _post_json(
        endpoint=endpoint,
        api_key=api_key,
        payload=payload,
        timeout_seconds=60.0,
        max_retries=2,
        retry_backoff_seconds=2.0,
    )
    base = response.get("base_resp") or {}
    if base.get("status_code") != 0:
        raise RuntimeError(f"MiniMax error: {base}")
    data = response.get("data") or {}
    audio_hex = data.get("audio", "")
    if not audio_hex:
        raise RuntimeError("MiniMax returned no audio data")
    return bytes.fromhex(audio_hex)


def _synthesize_cosyvoice(text: str, voice_id: str, model: str) -> bytes:
    """Call CosyVoice via the existing provider helper."""
    from services.tts.cosyvoice_provider import synthesize as cv_synth

    return cv_synth(text, voice_id, model=model)


def _synthesize_volcengine(text: str, voice_id: str, resource_id: str) -> bytes:
    """Call VolcEngine via the existing provider."""
    from services.tts.volcengine_tts_provider import synthesize as vc_synth

    return vc_synth(text, voice_id, resource_id=resource_id)


def _get_synth_fn(provider: str) -> Callable[[str, str, str], bytes]:
    if provider == "minimax":
        return _synthesize_minimax
    if provider == "cosyvoice":
        return _synthesize_cosyvoice
    if provider == "volcengine":
        return _synthesize_volcengine
    raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# DB access (load voices, write results)
# ---------------------------------------------------------------------------

def _asyncpg_url(db_url: str) -> str:
    """Strip '+asyncpg' driver tag from SQLAlchemy URL for raw asyncpg use."""
    return db_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _load_voices(
    db_url: str,
    provider: str,
    model: str,
    voice_id: str | None,
    only_chinese: bool,
    only_matchable: bool,
    force: bool,
) -> list[dict]:
    """Load matching voice rows from voice_catalog via raw asyncpg.

    Uses asyncpg directly so the script can run inside the app container
    (which doesn't have gateway/voice_catalog_models on its sys.path).
    """
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_url(db_url))
    try:
        sql = (
            "SELECT voice_id, display_name, "
            "chars_per_second_by_model, provider_config "
            "FROM voice_catalog "
            "WHERE provider = $1 AND archived_at IS NULL"
        )
        params: list = [provider]
        idx = 2
        if voice_id:
            sql += f" AND voice_id = ${idx}"
            params.append(voice_id)
            idx += 1
        if only_matchable:
            sql += " AND matchable = true"
        if only_chinese:
            # Two storage conventions in voice_catalog:
            #   - CosyVoice / VolcEngine rows: language = 'zh'
            #   - MiniMax rows: language = '中文-普通话' or '中文-粤语'
            sql += " AND (language LIKE 'zh%' OR language LIKE '%中文%')"
        rows = await conn.fetch(sql, *params)
    finally:
        await conn.close()

    voices: list[dict] = []
    for r in rows:
        provider_config = r["provider_config"] or {}
        if isinstance(provider_config, str):
            # asyncpg may return JSONB as text when no codec is registered
            import json as _json
            provider_config = _json.loads(provider_config)
        if provider == "volcengine":
            rid = provider_config.get("resource_id", "")
            if rid != model:
                continue

        by_model_raw = r["chars_per_second_by_model"] or {}
        if isinstance(by_model_raw, str):
            import json as _json
            by_model_raw = _json.loads(by_model_raw)
        if not force and model in by_model_raw:
            continue  # already calibrated for this model

        voices.append({
            "voice_id": r["voice_id"],
            "display_name": r["display_name"],
            "existing_by_model": dict(by_model_raw),
        })
    return voices


async def _persist_result(
    db_url: str,
    voice_id: str,
    model: str,
    cps: float,
) -> None:
    """Merge per-model value + recompute scalar average. Raw asyncpg."""
    import asyncpg
    import json as _json

    conn = await asyncpg.connect(_asyncpg_url(db_url))
    try:
        existing_raw = await conn.fetchval(
            "SELECT chars_per_second_by_model FROM voice_catalog WHERE voice_id = $1",
            voice_id,
        )
        if existing_raw is None:
            existing: dict = {}
        elif isinstance(existing_raw, str):
            existing = _json.loads(existing_raw)
        else:
            existing = dict(existing_raw)

        existing[model] = round(cps, 4)
        scalar_avg = round(sum(existing.values()) / len(existing), 4)
        now = datetime.now(timezone.utc)

        rows_affected = await conn.execute(
            "UPDATE voice_catalog "
            "SET chars_per_second = $1, "
            "    chars_per_second_by_model = $2::jsonb, "
            "    speed_calibrated_at = $3, "
            "    updated_at = $3 "
            "WHERE voice_id = $4",
            scalar_avg, _json.dumps(existing), now, voice_id,
        )
        if rows_affected.split()[-1] == "0":
            raise RuntimeError(f"voice_id not found: {voice_id}")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Core calibration per voice
# ---------------------------------------------------------------------------

def _calibrate_one_voice(
    provider: str,
    model: str,
    voice_id: str,
    synth_fn: Callable[[str, str, str], bytes],
    limiter: _RateLimiter,
    verbose: bool = True,
) -> tuple[float | None, str | None]:
    """Run the 3 standard texts through the given voice, return (cps, error).

    Returns (chars_per_sec, None) on success, or (None, error_msg) on failure.
    """
    total_hanzi = 0
    total_ms = 0
    for name, text in STANDARD_TEXTS.items():
        limiter.wait()
        try:
            wav = synth_fn(text, voice_id, model)
        except Exception as exc:
            return None, f"synth failed on {name}: {exc}"
        try:
            ms = _measure_wav_duration_ms(wav)
        except Exception as exc:
            return None, f"ffprobe failed on {name}: {exc}"
        hanzi = count_hanzi(text)
        total_hanzi += hanzi
        total_ms += ms
        if verbose:
            seg_cps = hanzi / (ms / 1000.0) if ms > 0 else 0.0
            print(f"    {name}: {hanzi} hanzi / {ms/1000:.2f}s = {seg_cps:.2f} cps")

    if total_ms <= 0:
        return None, "total duration zero"
    cps = total_hanzi / (total_ms / 1000.0)
    if not (MIN_VALID_CPS <= cps <= MAX_VALID_CPS):
        return None, f"cps {cps:.2f} out of sanity range [{MIN_VALID_CPS}, {MAX_VALID_CPS}]"
    return round(cps, 4), None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run(
    db_url: str,
    targets: list[tuple[str, str, int]],
    voice_id: str | None,
    force: bool,
    limit: int | None,
    output_csv: Path | None,
    dry_run: bool,
) -> int:
    """Main driver. Returns exit code."""
    results: list[dict] = []
    total_success = 0
    total_failed = 0

    for provider, model, rpm in targets:
        print(f"\n=== {provider} / {model} (RPM={rpm}) ===")
        voices = await _load_voices(
            db_url, provider, model, voice_id,
            only_chinese=True, only_matchable=True, force=force,
        )
        if limit is not None:
            voices = voices[:limit]
        print(f"  {len(voices)} voices to calibrate")

        if dry_run:
            for v in voices[:10]:
                print(f"    [dry-run] {v['voice_id']} ({v['display_name']})")
            if len(voices) > 10:
                print(f"    ... and {len(voices) - 10} more")
            continue

        limiter = _RateLimiter(rpm)
        synth_fn = _get_synth_fn(provider)

        for i, v in enumerate(voices, 1):
            vid = v["voice_id"]
            print(f"  [{i}/{len(voices)}] {vid} ({v['display_name']})")
            cps, err = _calibrate_one_voice(provider, model, vid, synth_fn, limiter)
            if cps is None:
                total_failed += 1
                print(f"    FAILED: {err}")
                results.append({
                    "provider": provider, "model": model, "voice_id": vid,
                    "chars_per_second": "", "error": err,
                })
                continue
            try:
                await _persist_result(db_url, vid, model, cps)
            except Exception as exc:
                total_failed += 1
                print(f"    FAILED to persist: {exc}")
                results.append({
                    "provider": provider, "model": model, "voice_id": vid,
                    "chars_per_second": cps, "error": f"persist: {exc}",
                })
                continue
            total_success += 1
            print(f"    OK: {cps:.3f} chars/sec (saved)")
            results.append({
                "provider": provider, "model": model, "voice_id": vid,
                "chars_per_second": cps, "error": "",
            })

    print(f"\n=== Summary ===")
    print(f"Success: {total_success}")
    print(f"Failed:  {total_failed}")

    if output_csv and results:
        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["provider", "model", "voice_id", "chars_per_second", "error"]
            )
            writer.writeheader()
            writer.writerows(results)
        print(f"Wrote CSV to {output_csv}")

    return 0 if total_failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(description="Voice speed calibration for voice_catalog.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Default. Preview plan, no API calls, no DB writes.")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="Override --dry-run and actually call paid APIs + write DB.")
    parser.add_argument("--provider", default=None, choices=["minimax", "cosyvoice", "volcengine"],
                        help="Only calibrate for one provider.")
    parser.add_argument("--model", default=None,
                        help="Only calibrate for one model. Must be used with --provider.")
    parser.add_argument("--voice-id", default=None,
                        help="Only calibrate a single voice. Must be used with --provider.")
    parser.add_argument("--force", action="store_true",
                        help="Re-calibrate even if already done for this model.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap at N voices per provider (for sampling).")
    parser.add_argument("--output-csv", type=Path, default=None,
                        help="Additionally write results to CSV file.")
    args = parser.parse_args()

    dry_run = not args.execute

    if args.voice_id and not args.provider:
        print("ERROR: --voice-id requires --provider")
        return 2
    if args.model and not args.provider:
        print("ERROR: --model requires --provider")
        return 2

    # Filter targets
    targets = CALIBRATION_TARGETS[:]
    if args.provider:
        targets = [t for t in targets if t[0] == args.provider]
    if args.model:
        targets = [t for t in targets if t[1] == args.model]
    if not targets:
        print("ERROR: no matching targets")
        return 2

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("AVT_DATABASE_URL") or ""
    if not dry_run and not db_url:
        print("ERROR: DATABASE_URL / AVT_DATABASE_URL must be set for --execute")
        return 2

    if dry_run:
        print("=== DRY RUN === (no API calls, no DB writes)")
        print(f"Would calibrate these (provider, model) pairs:")
        for t in targets:
            print(f"  - {t[0]} / {t[1]} (RPM {t[2]})")
        if db_url:
            # Even dry-run queries DB to show voice counts, if URL available.
            return asyncio.run(_run(db_url, targets, args.voice_id, args.force,
                                    args.limit, args.output_csv, dry_run=True))
        print("(Set DATABASE_URL to see exact voice counts.)")
        return 0

    # Execute mode — warn loudly.
    print("=" * 60)
    print("WARNING: --execute mode — this will call PAID TTS APIs")
    print("=" * 60)
    for t in targets:
        print(f"  - {t[0]} / {t[1]}")
    print(f"Cost will depend on voice count. See module docstring.")
    print()

    return asyncio.run(_run(db_url, targets, args.voice_id, args.force,
                            args.limit, args.output_csv, dry_run=False))


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
"""VolcEngine explicit_language parameter effect on cross-language synthesis.

Goal
----
Determine whether the observed failure (en_male_tim_uranus_bigtts +
Chinese text -> "VolcEngine TTS returned no audio data") is caused by:
  (a) voice being hard-bound to its native language (no fix possible), OR
  (b) missing req_params.audio_params.explicit_language parameter (fix
      possible by adding the param to the provider).

Method
------
Synthesize 5 combinations with/without explicit_language and compare
whether audio bytes are produced:

  | voice                           | text lang | explicit_language | expected |
  | ------------------------------- | --------- | ----------------- | -------- |
  | en_male_tim_uranus_bigtts       | zh        | (omitted)         | no audio (baseline) |
  | en_male_tim_uranus_bigtts       | zh        | "zh-cn"           | KEY TEST |
  | en_male_tim_uranus_bigtts       | zh        | "crosslingual"    | alt value |
  | zh_female_yingyujiaoxue_...     | en        | (omitted)         | control  |
  | zh_female_yingyujiaoxue_...     | en        | "en-us"           | control  |

Output: which combinations produced audio + duration + cps. Verdict
derives the root cause.

Safety
------
Paid API. Default --dry-run. Pass --execute for real calls.
Cost: ~5 calls x ~200 chars x CNY 3/10k = ~CNY 0.32.
"""
from __future__ import annotations

import argparse
import base64
import csv
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if _SRC_DIR.exists() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from services.tts.volcengine_tts_provider import (  # noqa: E402
    CODE_AUDIO_CHUNK,
    CODE_FINISH,
    DEFAULT_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    RESOURCE_ID_2_0,
    VolcEngineTTSError,
    _build_headers,
    _do_post,
    _iter_chunk_events,
    _resolve_credentials,
)


# Test texts (inlined for container portability).
T_ZH = (
    "这款手机的屏幕素质让我很震惊。"
    "色彩通透，对比度极高，黑色几乎和关屏没有区别。"
    "最让我意外的是功耗居然还降低了，续航多了将近两小时。"
)

T_EN = (
    "The screen quality on this phone really surprised me. "
    "Colors are vivid, contrast is extremely high, "
    "and what amazed me most is that power consumption actually decreased."
)


# Test matrix: (label, voice_id, text, explicit_language)
# explicit_language=None means the key is omitted from audio_params.
TEST_CASES: list[tuple[str, str, str, str | None]] = [
    ("en_voice_zh_text_no_lang",  "en_male_tim_uranus_bigtts",            T_ZH, None),
    ("en_voice_zh_text_zh-cn",    "en_male_tim_uranus_bigtts",            T_ZH, "zh-cn"),
    ("en_voice_zh_text_cross",    "en_male_tim_uranus_bigtts",            T_ZH, "crosslingual"),
    ("zh_voice_en_text_no_lang",  "zh_female_yingyujiaoxue_uranus_bigtts", T_EN, None),
    ("zh_voice_en_text_en-us",    "zh_female_yingyujiaoxue_uranus_bigtts", T_EN, "en-us"),
]


def _build_payload(
    text: str,
    speaker: str,
    *,
    explicit_language: str | None = None,
) -> dict:
    req_params: dict = {
        "speaker": speaker,
        "text": text,
        "audio_params": {
            "format": "pcm",
            "sample_rate": PCM_SAMPLE_RATE,
        },
    }
    if explicit_language:
        req_params["audio_params"]["explicit_language"] = explicit_language
    return {
        "user": {"uid": "aivideotrans-lang-test"},
        "req_params": req_params,
    }


def _synthesize_once(
    text: str,
    voice_id: str,
    explicit_language: str | None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, str, str]:
    """Return (pcm_bytes, log_id, error_message)."""
    app_id, access_key, _ = _resolve_credentials()
    headers = _build_headers(app_id, access_key, RESOURCE_ID_2_0)
    payload = _build_payload(text, voice_id, explicit_language=explicit_language)

    pcm_chunks: list[bytes] = []
    finished = False
    log_id = ""
    error_msg = ""

    response = _do_post(
        DEFAULT_ENDPOINT,
        headers=headers,
        json=payload,
        stream=True,
        timeout=timeout_seconds,
    )
    try:
        log_id = response.headers.get("X-Tt-Logid", "")
        for event in _iter_chunk_events(response):
            code = event.get("code", -1)
            if code == CODE_AUDIO_CHUNK:
                data_b64 = event.get("data", "")
                if data_b64:
                    pcm_chunks.append(base64.b64decode(data_b64))
                continue
            if code == CODE_FINISH:
                finished = True
                break
            if code > 0:
                error_msg = f"code={code} msg={event.get('message', '')!r}"
                break
    finally:
        response.close()

    if error_msg:
        return b"", log_id, error_msg
    if not finished:
        return b"", log_id, "stream ended without finish"
    if not pcm_chunks:
        return b"", log_id, "no audio data"
    return b"".join(pcm_chunks), log_id, ""


def _pcm_duration_ms(pcm_bytes: bytes) -> int:
    bps = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH
    return int(round(len(pcm_bytes) / bps * 1000)) if bps > 0 else 0


def _default_csv_path() -> Path:
    linux_tmp = Path("/tmp")
    base = linux_tmp if linux_tmp.is_dir() else Path(tempfile.gettempdir())
    return base / "volcengine_explicit_language_test.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--sleep", type=float, default=2.0)
    args = parser.parse_args(argv)

    csv_path = Path(args.csv) if args.csv else _default_csv_path()
    total_chars = sum(len(text) for _, _, text, _ in TEST_CASES)
    cost = total_chars / 10000 * 3.0

    print()
    print("=" * 72)
    print("VolcEngine explicit_language cross-language synthesis test")
    print("=" * 72)
    print(f"  resource_id : {RESOURCE_ID_2_0}")
    print(f"  calls       : {len(TEST_CASES)}")
    print(f"  total chars : {total_chars}")
    print(f"  est. cost   : CNY {cost:.2f}")
    print(f"  csv output  : {csv_path}")
    print()
    print("  Test matrix:")
    print(f"  {'label':<30} {'voice_prefix':<5} {'text_lang':<9} {'explicit_language':<16}")
    for label, vid, text, lang in TEST_CASES:
        prefix = vid.split("_")[0]
        text_lang = "zh" if text == T_ZH else "en"
        print(f"  {label:<30} {prefix:<5} {text_lang:<9} {lang or '(omitted)':<16}")
    print()

    if not args.execute:
        print("[dry-run] No API calls made. Re-run with --execute to proceed.")
        return 0

    print("[execute] Starting...")
    try:
        _resolve_credentials()
    except VolcEngineTTSError as exc:
        print(f"FATAL: credentials missing: {exc}")
        return 2

    results: list[dict] = []
    for idx, (label, voice_id, text, explicit_language) in enumerate(TEST_CASES):
        print(f"  [{idx + 1}/{len(TEST_CASES)}] {label}", end=" ... ", flush=True)
        try:
            t0 = time.time()
            pcm, log_id, err = _synthesize_once(text, voice_id, explicit_language)
            api_ms = int((time.time() - t0) * 1000)
            audio_ms = _pcm_duration_ms(pcm)
            ok = bool(pcm) and not err
            status = "OK" if ok else "FAIL"
            print(f"{status} audio={audio_ms}ms bytes={len(pcm)} api={api_ms}ms logid={log_id}{' err=' + err if err else ''}")
            results.append({
                "label": label,
                "voice_id": voice_id,
                "text_lang": "zh" if text == T_ZH else "en",
                "explicit_language": explicit_language or "",
                "status": status,
                "audio_ms": audio_ms,
                "bytes": len(pcm),
                "api_ms": api_ms,
                "log_id": log_id,
                "error": err,
            })
        except Exception as exc:
            print(f"EXCEPTION: {exc}")
            results.append({
                "label": label,
                "voice_id": voice_id,
                "text_lang": "zh" if text == T_ZH else "en",
                "explicit_language": explicit_language or "",
                "status": "EXCEPTION",
                "audio_ms": 0, "bytes": 0, "api_ms": 0, "log_id": "",
                "error": str(exc)[:200],
            })
        if idx < len(TEST_CASES) - 1:
            time.sleep(args.sleep)

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "label", "voice_id", "text_lang", "explicit_language",
                "status", "audio_ms", "bytes", "api_ms", "log_id", "error",
            ])
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nCSV written: {csv_path}")
    except OSError as exc:
        print(f"CSV write failed: {exc}")

    # Results table
    print()
    print("=" * 72)
    print("Results")
    print("=" * 72)
    print(f"{'label':<30} {'status':<6} {'audio_ms':>9} {'bytes':>9} {'error':<25}")
    print("-" * 72)
    for r in results:
        print(f"{r['label']:<30} {r['status']:<6} {r['audio_ms']:>9} {r['bytes']:>9} {r['error'][:25]:<25}")

    # Verdict
    print()
    print("=" * 72)
    print("Verdict")
    print("=" * 72)
    by_label = {r["label"]: r for r in results}
    en_v_zh_no_lang_ok = by_label.get("en_voice_zh_text_no_lang", {}).get("status") == "OK"
    en_v_zh_zhcn_ok = by_label.get("en_voice_zh_text_zh-cn", {}).get("status") == "OK"
    en_v_zh_cross_ok = by_label.get("en_voice_zh_text_cross", {}).get("status") == "OK"

    if en_v_zh_zhcn_ok or en_v_zh_cross_ok:
        print("RESULT: explicit_language CAN enable cross-language synthesis.")
        print("  -> Fix path: add explicit_language param to volcengine_tts_provider.py")
        print("  -> UX path: warn user that English voice produces Chinese-with-accent")
        if en_v_zh_zhcn_ok:
            print("  Working value: 'zh-cn'")
        if en_v_zh_cross_ok:
            print("  Working value: 'crosslingual'")
    elif not en_v_zh_no_lang_ok and not en_v_zh_zhcn_ok and not en_v_zh_cross_ok:
        print("RESULT: VolcEngine English voices are HARD-BOUND to English.")
        print("  No explicit_language value unlocks cross-language synthesis.")
        print("  -> Fix path: DB matchable=false for all en_* volcengine voices.")
    else:
        print("RESULT: Mixed / unexpected pattern. Review table above manually.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

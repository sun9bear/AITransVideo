# Video Translation Quality Benchmark Fixture

This fixture is generated from historical intermediate project artifacts and gateway metering exports.
It is sanitized for offline benchmark and regression work: no source URLs, payment fields, media files, or full raw transcript payloads should be present.

## Contents

- `manifest.json`: dataset version, selection policy, coverage, and benchmark job index.
- `jobs/bench_*/segments.json`: whitelisted segment-level duration/alignment metrics.
- `jobs/bench_*/pre_tts_events.json`: whitelisted pre-TTS rewrite trigger metrics.
- `jobs/bench_*/speaker_corrections.json`: sanitized speaker correction rows with short redacted snippets.
- `jobs/bench_*/metering_snapshot.json`: whitelisted gateway metering fields.
- `jobs/bench_*/artifact_index.json`: file-level artifact inventory, not raw S2 responses.

## Coverage

- Jobs selected: 12 / 84
- Provider coverage: {'cosyvoice': 2, 'minimax': 5, 'unknown': 3, 'volcengine': 2}
- Jobs with pre-TTS contradictions: 4
- Jobs with speaker corrections: 6

Regenerate with:

```bash
python scripts/benchmark/build_quality_dataset.py --force --max-jobs 12
python scripts/benchmark/validate_quality_dataset.py
python scripts/benchmark/report_quality_baseline.py
```

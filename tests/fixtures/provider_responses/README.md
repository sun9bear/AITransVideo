# Provider response usage fixtures (Phase 0a spike)

Captured 2026-05-29 via in-container API calls on the US host (real keys,
minimal prompts, `max_tokens<=8`). Files contain **only sanitized usage
token counts** — no message content, no API keys, no user audio.

Source of plan: `docs/plans/2026-05-27-xiaomi-mimo-optimization-plan.md` (Phase 0a / Phase 2).

## Confirmed field shapes (OpenAI-compatible)

| Field | MiMo v2.5 | DeepSeek v4-flash |
| --- | --- | --- |
| `usage.prompt_tokens` | ✅ total input | ✅ total input |
| `usage.completion_tokens` | ✅ | ✅ |
| `usage.prompt_tokens_details.cached_tokens` | ✅ | ✅ |
| `usage.prompt_tokens_details.audio_tokens` | ✅ (only when audio input) | n/a (text-only) |
| `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | ❌ | ✅ (extra) |

## ⚠️ PR 2 mapping rule — avoid double-counting

`prompt_tokens` is the **total** input and **already includes** both
`cached_tokens` and `audio_tokens`. The cost engine
(`gateway/cost_management.py::apply_costs`) bills additively:
`input_tokens*input_price + cached_input_tokens*cached_price + audio_input_tokens*audio_price`.

So PR 2 must split, not pass raw `prompt_tokens`:

```
audio_input_tokens = prompt_tokens_details.audio_tokens            # 0 if absent
cached_input_tokens = prompt_tokens_details.cached_tokens          # 0 if absent
input_tokens        = prompt_tokens - audio_input_tokens - cached_input_tokens
output_tokens       = completion_tokens
```

Passing raw `prompt_tokens` as `input_tokens` while also setting
`cached_input_tokens` / `audio_input_tokens` would bill the cached/audio
portion twice.

## MiMo TTS

`mimo-v2.5-tts` rejected a naive `{"messages":[...]}` chat payload with
HTTP 400 — it needs the dedicated TTS payload format (see
`src/services/tts/mimo_tts_provider.py`). TTS is currently limited-free, so
its cost does not depend on the usage shape; PR 3 will capture the proper
TTS response when it wires the upgrade.

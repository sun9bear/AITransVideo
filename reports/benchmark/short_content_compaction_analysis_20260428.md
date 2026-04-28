# Short Content Compaction Analysis - 2026-04-28

## Scope

Read-only scan on the US production host.

- Jobs scanned: latest 30 succeeded jobs with available project `segments.json`
- Candidate target duration: `2000ms <= target_duration_ms < 8000ms`
- Candidate overflow threshold: `first_pass_duration_ms / target_duration_ms >= 1.30`
- Candidate alignment methods: `force_dsp`, `capped_dsp_overflow`, `rewrite_dsp`
- Excluded classes: low-information cue, non-speech/background audio

Raw local artifact:

- `.codex_tmp/short_content_compaction_analysis.json`

## Aggregate

| Metric | Value |
|---|---:|
| Jobs scanned | 30 |
| 2-8s segments | 542 |
| 2-8s overflow/risk segments | 310 |
| Short content compaction candidates | 248 |
| Candidate rate among 2-8s segments | 45.8% |
| Candidate rate among risky 2-8s segments | 80.0% |
| Candidate segments already handled by pre-TTS rewrite | 7 |
| Candidate segments not handled by pre-TTS rewrite | 241 |

## Candidate Classes

| Class | Count |
|---|---:|
| Content clause | 91 |
| Question | 86 |
| Short answer / clause | 71 |

The distribution confirms that the problem is not mostly timer cues or background audio. It is mainly real short-form content: quick questions, interviewer follow-ups, short answers, and compressed conversational clauses.

## Duration Buckets

| Bucket | Total 2-8s segments | Candidates | Main candidate shape |
|---|---:|---:|---|
| 2-3s | 139 | 75 | short answers and compact questions |
| 3-5s | 219 | 106 | mixed questions and content clauses |
| 5-8s | 184 | 67 | longer content clauses and follow-up questions |

## Top Jobs

| Job | Candidates / 2-8s segments | Shape |
|---|---:|---|
| `job_f08cabc1...` CNBC Buffett old run | 57 / 91 | interview questions and short answers |
| `job_a065d99d...` CNBC Buffett new run | 53 / 79 | interview questions and short answers |
| `job_3066774d...` Jensen Huang | 41 / 62 | technical Q&A, compact claims |
| `job_6940aeac...` Anthropic product team | 29 / 42 | interview Q&A |
| `job_f169c0c5...` Jimmy Kimmel | 15 / 30 | short conversational answers |

## Latest Long Video

New run:

- `job_a065d99dd7484d7dac8810b556300c30`
- Title: `Watch CNBC's full interview with Berkshire Hathaway CEO Warren Buffett`
- 2-8s segments: 79
- Candidates: 53
- Candidate classes:
  - question: 25
  - short answer / clause: 13
  - content clause: 15
- Existing pre-TTS rewrite coverage for these candidates: 0 / 53

Old same-source run:

- `job_f08cabc1267642b98a9d774a9e2a5da4`
- 2-8s segments: 91
- Candidates: 57
- Existing pre-TTS rewrite coverage for these candidates: 0 / 57

Interpretation:

- P1 improvements reduced total force-DSP and review burden, but this 2-8s content segment class remains largely untouched.
- The new low-information keep-original route is intentionally irrelevant here; these are real content segments and should not be kept as original audio by default.

## Representative Candidate Shapes

| Source | Current CN shape | Target |
|---|---|---|
| `Would you repeat that this time? If trouble's coming, would you still say buy stocks right now?` | long two-part literal question | compress to one concise question preserving the core ask |
| `How do you read through any of that? What are you hearing? Do you know more than we do on that front?` | three literal questions | compress to one or two short questions |
| `Auto insurance, I'm not sure, I might prefer the 80-year-olds over the 20-year-olds.` | natural but too long for 3s | compress while preserving comparison and stance |
| `These next few years. We've got these models that are going to be able to do all the cyber...` | technical clause too long for slot | compact technical claim, preserve key entity/action |

## Conclusion

This is a real workflow gap and a good candidate for a focused `P1-n short_content_compact_rewrite` stage.

Recommended first production rule:

1. Only apply to `2-8s` segments.
2. Only apply when estimated or measured first-pass TTS exceeds target by at least `30%`.
3. Exclude low-information cues, non-speech/background audio, and `keep_original` segments.
4. Use a dedicated compact spoken-translation rewrite prompt, not the normal duration rewrite prompt.
5. Preserve hard entities: numbers, names, negation, time, company/product names.
6. Enforce strict spoken-char bounds after rewrite.
7. If rewrite output violates bounds or drops required entities, reject it and keep current behavior with review.

Recommended initial metrics:

- `short_content_compact_candidate_count`
- `short_content_compact_accepted_count`
- `short_content_compact_rejected_count`
- `short_content_compact_rejected_reason_distribution`
- `short_content_compact_force_dsp_after_count`
- `short_content_compact_needs_review_after_count`

Go condition:

- On duplicate long interview / technical interview videos, reduce `2-8s force_dsp` by at least 25% without increasing harmful pre-TTS contradiction or obvious semantic loss.

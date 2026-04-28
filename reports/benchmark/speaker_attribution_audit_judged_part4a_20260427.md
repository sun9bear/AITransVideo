# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:31:48.993091+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `6`
- Start: `26`
- Limit: `6`
- Decisions: `6`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 3 |
| `distinct_speaker` | 2 |
| `main_speaker` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 5 |
| `reassign_to_main` | 1 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_003` | s2_speaker | high | keep | The audio clearly matches the assigned interviewer asking a question. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_001` | distinct_speaker | high | keep | The audio is clearly Joe Rogan speaking, matching the assigned distinct speaker. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_002` | main_speaker | high | reassign_to_main | The voice clearly belongs to the main speaker continuing his sentence. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_003` | distinct_speaker | high | keep | The voice is a distinct speaker (Joe Rogan) agreeing with 'Yeah'. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_004` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) making a brief comment. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_005` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking a question. |
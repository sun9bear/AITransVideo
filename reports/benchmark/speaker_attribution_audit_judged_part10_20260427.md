# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:53:41.172267+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `11`
- Start: `104`
- Limit: `20`
- Decisions: `11`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 8 |
| `distinct_speaker` | 3 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 11 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_2f3f66698b294bdd9025aab28b2adac7_cand_002` | s2_speaker | high | keep | The audio clearly contains a female voice saying 'Yeah', which matches the assigned speaker (Becky Quick). |
| `job_2f3f66698b294bdd9025aab28b2adac7_cand_003` | s2_speaker | high | keep | The audio clearly contains a female voice saying 'Yeah', which matches the assigned speaker (Becky Quick). |
| `job_2f3f66698b294bdd9025aab28b2adac7_cand_004` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker. |
| `job_2f3f66698b294bdd9025aab28b2adac7_cand_005` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker. |
| `job_2f3f66698b294bdd9025aab28b2adac7_cand_006` | s2_speaker | high | keep | The audio clearly features the female interviewer asking the question, matching the assigned speaker. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_001` | s2_speaker | high | keep | The audio clearly features the interviewer saying 'Yeah' in agreement, matching the assigned speaker. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_002` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Tom Bilyeu) asking a short question. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_003` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking a question. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_004` | distinct_speaker | high | keep | The audio clearly features a distinct speaker (Joe Rogan) asking the question, matching the assigned speaker. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_005` | distinct_speaker | high | keep | The audio clearly features a distinct speaker (Joe Rogan) asking the question, matching the assigned speaker. |
| `job_b8a76a5a8ab64c03a4478602c45b6032_cand_006` | distinct_speaker | high | keep | The audio clearly features Joe Rogan speaking, confirming the assigned distinct speaker is correct. |
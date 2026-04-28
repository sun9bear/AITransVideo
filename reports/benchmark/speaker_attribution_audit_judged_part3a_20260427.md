# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:30:21.459715+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `6`
- Start: `20`
- Limit: `6`
- Decisions: `6`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 4 |
| `distinct_speaker` | 2 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 6 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_003` | distinct_speaker | high | keep | The audio clearly contains a distinct speaker (the interviewer) saying 'Yeah', which matches the assigned speaker. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_004` | distinct_speaker | high | keep | The audio clearly contains a distinct speaker (the interviewer) asking the question, matching the assigned speaker. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_005` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking the question. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_006` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking the question. |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_001` | s2_speaker | high | keep | The male interviewer clearly says 'That tracks' in response to the guest. |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_002` | s2_speaker | high | keep | The male interviewer clearly says 'Okay, there's a limit' in response to the guest. |
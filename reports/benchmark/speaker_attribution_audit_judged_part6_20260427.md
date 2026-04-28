# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:43:32.193419+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `12`
- Start: `56`
- Limit: `12`
- Decisions: `12`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `distinct_speaker` | 10 |
| `s2_speaker` | 2 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 12 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_7e12f5f49ed04b9b8bc3d1a003b61290_cand_004` | distinct_speaker | high | keep | The audio clearly features the interviewer speaking, distinct from the main speaker. |
| `job_198ffd877c4044869b2f0db988c424fe_cand_001` | distinct_speaker | high | keep | The audio clearly features the interviewer speaking, distinct from the main speaker. |
| `job_198ffd877c4044869b2f0db988c424fe_cand_002` | distinct_speaker | high | keep | The audio clearly features a female interviewer asking a question, distinct from the main speaker. |
| `job_198ffd877c4044869b2f0db988c424fe_cand_003` | distinct_speaker | high | keep | The audio features the same female interviewer asking a question, which is correctly assigned to a distinct speaker. |
| `job_198ffd877c4044869b2f0db988c424fe_cand_004` | s2_speaker | high | keep | The assigned speaker correctly identifies the interviewer speaking. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_001` | distinct_speaker | high | keep | The audio features a distinct male voice from a video insert, different from the main interviewer. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_002` | distinct_speaker | high | keep | The audio features a distinct female narrator introducing the segment, which is clearly different from the main male speaker. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_003` | distinct_speaker | high | keep | The audio is a brief response ('No') from Elon Musk, matching the assigned speaker profile and context. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_004` | distinct_speaker | high | keep | The audio clearly features a distinct female interviewer (Betty Liu) asking a question, matching the assigned speaker. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_005` | distinct_speaker | high | keep | The audio clearly features Elon Musk speaking, which is a distinct voice from the main narrator and matches the assigned speaker. |
| `job_925ad5833c764d2ab386281b58a6c18f_cand_006` | distinct_speaker | high | keep | The audio clearly features a distinct speaker (Jim Farley) as introduced by the narrator. |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_001` | s2_speaker | high | keep | The female interviewer is clearly asking the question, distinct from the main speaker. |
# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:46:04.541595+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `12`
- Start: `68`
- Limit: `12`
- Decisions: `12`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 8 |
| `distinct_speaker` | 4 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 12 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_002` | s2_speaker | high | keep | Clear female voice saying 'Right', matching the assigned speaker. |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_003` | s2_speaker | high | keep | Clear female voice saying 'Yeah', matching the assigned speaker. |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_004` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker. |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_005` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker. |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4_cand_006` | s2_speaker | high | keep | The audio clearly features the female interviewer asking the question, matching the assigned speaker. |
| `job_67009391e2d1425eb52728aca4995130_cand_001` | distinct_speaker | high | keep | The audio features a distinct female interviewer asking the question, which is correctly separated from the main narrator. |
| `job_67009391e2d1425eb52728aca4995130_cand_002` | distinct_speaker | high | keep | The voice belongs to a distinct commentator or reviewer, different from the main reporter. |
| `job_67009391e2d1425eb52728aca4995130_cand_003` | distinct_speaker | high | keep | The voice is a distinct narrator introducing the main reporter at the end of the clip. |
| `job_67009391e2d1425eb52728aca4995130_cand_004` | s2_speaker | high | keep | The audio clearly matches the assigned speaker answering the interviewer's question. |
| `job_67009391e2d1425eb52728aca4995130_cand_005` | s2_speaker | high | keep | The audio clearly matches Elon Musk saying 'No' in response to the question. |
| `job_67009391e2d1425eb52728aca4995130_cand_006` | distinct_speaker | high | keep | The audio clearly matches Elon Musk's voice, who is a distinct speaker in this clip. |
| `job_abb3fedc905c403db67d784c480c170e_cand_001` | s2_speaker | high | keep | The audio is clearly the female interviewer asking a question, correctly assigned. |
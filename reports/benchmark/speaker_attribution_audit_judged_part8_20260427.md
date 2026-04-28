# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:48:37.651802+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `12`
- Start: `80`
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
| `job_abb3fedc905c403db67d784c480c170e_cand_002` | s2_speaker | high | keep | The audio clearly shows the female interviewer saying 'Right' as a backchannel response. |
| `job_abb3fedc905c403db67d784c480c170e_cand_003` | s2_speaker | high | keep | The audio clearly shows the female interviewer saying 'Yeah' in agreement. |
| `job_abb3fedc905c403db67d784c480c170e_cand_004` | s2_speaker | high | keep | The audio clearly contains a female voice saying 'Yeah', which matches the assigned speaker (speaker_a). |
| `job_abb3fedc905c403db67d784c480c170e_cand_005` | s2_speaker | high | keep | The audio clearly contains a female voice asking 'How so?', which matches the assigned speaker (speaker_a). |
| `job_abb3fedc905c403db67d784c480c170e_cand_006` | s2_speaker | high | keep | The audio clearly features the female interviewer asking the question, matching the assigned speaker. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_001` | s2_speaker | high | keep | The audio features the female interviewer asking the question, which aligns with the assigned speaker. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_002` | s2_speaker | high | keep | The audio clearly features the interviewer asking a question, which matches the assigned speaker. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_003` | s2_speaker | high | keep | The audio features the interviewer saying 'Right', confirming the assigned speaker is correct. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_004` | distinct_speaker | high | keep | The audio clearly contains a distinct female voice (the interviewer) saying 'Yeah' in agreement. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_005` | distinct_speaker | high | keep | The audio clearly contains a distinct female voice (the interviewer) asking 'How so?'. |
| `job_d3ca1fb8bf0b42efb906be716ac6995c_cand_006` | distinct_speaker | high | keep | The audio clearly features the interviewer asking 'How so?', confirming the assigned speaker is correct. |
| `job_b98bdc65841f4254bdc2a410eb6e0939_cand_001` | distinct_speaker | high | keep | The audio clearly features the interviewer saying 'Yeah' in agreement, confirming the assigned speaker is correct. |
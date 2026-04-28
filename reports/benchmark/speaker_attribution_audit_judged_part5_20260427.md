# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:40:51.365723+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `12`
- Start: `44`
- Limit: `12`
- Decisions: `12`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `asr_speaker` | 6 |
| `distinct_speaker` | 4 |
| `main_speaker` | 1 |
| `s2_speaker` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 11 |
| `reassign_to_main` | 1 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_006` | asr_speaker | high | keep | The female interviewer clearly repeats 'As a phone' in response to the main speaker. |
| `job_730e7195e9c04ed9906d744f62f2986e_cand_001` | distinct_speaker | high | keep | An audience member asks the question, which is distinct from the main speaker. |
| `job_730e7195e9c04ed9906d744f62f2986e_cand_002` | main_speaker | high | reassign_to_main | The text 'My book...' is spoken by the main speaker in response to an audience member shouting 'Your book'. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_001` | s2_speaker | high | keep | The interviewer correctly asks 'Because?' in response to the main speaker. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_002` | asr_speaker | high | keep | The audio clearly matches the interviewer's voice (speaker_a) making a brief interjection. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_003` | asr_speaker | high | keep | The audio clearly matches the interviewer's voice (speaker_a) asking a short question. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_004` | asr_speaker | high | keep | The audio clearly matches the interviewer asking a follow-up question. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_005` | asr_speaker | high | keep | The audio clearly matches the interviewer asking a short question. |
| `job_b96b20d250934c1caa58d0923b320a4f_cand_006` | asr_speaker | high | keep | The audio clearly features the interviewer asking the question, matching the assigned speaker. |
| `job_7e12f5f49ed04b9b8bc3d1a003b61290_cand_001` | distinct_speaker | high | keep | The audio features the interviewer speaking, which is a distinct speaker from the main speaker. |
| `job_7e12f5f49ed04b9b8bc3d1a003b61290_cand_002` | distinct_speaker | high | keep | The audio clearly features a distinct female interviewer asking a question, which matches the assigned speaker. |
| `job_7e12f5f49ed04b9b8bc3d1a003b61290_cand_003` | distinct_speaker | high | keep | The audio clearly features the same distinct female interviewer asking a question, correctly assigned to the distinct speaker. |
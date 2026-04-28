# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:30:12.689277+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `24`
- Start: `20`
- Limit: `24`
- Decisions: `24`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 13 |
| `asr_speaker` | 5 |
| `distinct_speaker` | 5 |
| `main_speaker` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 23 |
| `reassign_to_main` | 1 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_003` | asr_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) saying 'Yeah'. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_004` | asr_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking the question. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_005` | asr_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan) asking the question. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_006` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan). |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_001` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Lenny). |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_002` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Lenny). |
| `job_6940aeac0c7b442f809ef8fa1925302f_cand_003` | distinct_speaker | high | keep | The audio clearly features the male interviewer (Lenny), which matches the assigned speaker_b. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_001` | distinct_speaker | high | keep | The audio is clearly Joe Rogan speaking, matching the assigned speaker_a. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_002` | distinct_speaker | high | keep | The short interjection is spoken by a distinct voice (Chris Williamson), matching the assigned speaker_c. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_003` | asr_speaker | high | keep | The voice saying 'Yeah' matches the assigned speaker (Joe Rogan). |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_004` | main_speaker | high | reassign_to_main | The voice saying 'Uh, again, trite, right?' is clearly the main speaker (Naval Ravikant), continuing his previous thought. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_005` | asr_speaker | high | keep | The voice asking the question matches the assigned speaker (Joe Rogan). |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_006` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Joe Rogan). |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_001` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Dwarkesh Patel) interrupting. |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_002` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (Dwarkesh Patel) saying 'Yeah'. |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_003` | s2_speaker | high | keep | The audio clearly shows speaker_a saying 'Yeah' as a backchannel response. |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_004` | s2_speaker | high | keep | Speaker_a clearly answers 'Yes' to the question asked by speaker_b. |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_005` | s2_speaker | high | keep | Speaker_a clearly says 'Yeah' in agreement. |
| `job_3066774da2a64848b2f4d1d2824e022b_cand_006` | s2_speaker | high | keep | The brief interruption matches the interviewer's voice. |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_001` | distinct_speaker | high | keep | The speaker is explicitly introduced as Andrew and the voice matches a distinct male speaker. |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_002` | distinct_speaker | high | keep | The voice matches the distinct male speaker continuing his thought. |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_003` | s2_speaker | high | keep | The audio clearly features the female interviewer's voice, matching the assigned speaker. |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_004` | s2_speaker | high | keep | The audio clearly features the female interviewer's voice asking the question, matching the assigned speaker. |
| `job_f08cabc1267642b98a9d774a9e2a5da4_cand_005` | s2_speaker | high | keep | The audio features the male co-host's voice, matching the assigned speaker. |
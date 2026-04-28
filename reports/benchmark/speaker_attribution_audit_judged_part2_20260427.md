# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:25:11.614848+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `12`
- Start: `8`
- Limit: `12`
- Decisions: `12`
- Errors: `0`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 8 |
| `distinct_speaker` | 3 |
| `music_or_non_speech` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 10 |
| `mark_non_speech` | 1 |
| `mark_review` | 1 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_003` | s2_speaker | high | keep | The audio clearly features the male host speaking. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_004` | s2_speaker | high | keep | The audio clearly features the male host speaking over the background music. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_005` | music_or_non_speech | high | mark_non_speech | The audio consists of a crowd chanting and cheering, not the assigned speaker. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_006` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (speaker_a). |
| `job_8d78c2685351464192b41828c2cda049_cand_001` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (speaker_a). |
| `job_8d78c2685351464192b41828c2cda049_cand_002` | s2_speaker | high | keep | The audio clearly matches the assigned speaker (speaker_a). |
| `job_8d78c2685351464192b41828c2cda049_cand_003` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker_a. |
| `job_8d78c2685351464192b41828c2cda049_cand_004` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'How so?', which matches the assigned speaker_a. |
| `job_8d78c2685351464192b41828c2cda049_cand_005` | s2_speaker | high | keep | The audio clearly features the female interviewer asking 'What was it?', which matches the assigned speaker_a. |
| `job_8d78c2685351464192b41828c2cda049_cand_006` | distinct_speaker | high | keep | The audio clearly features the female interviewer (Becky Quick) saying 'Yes. Yeah.', which matches the assigned speaker. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_001` | distinct_speaker | high | mark_review | The speaker is clearly the interviewer (Joe Rogan), but is assigned to an 'unknown speaker' profile. It should likely be merged with speaker_a. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_002` | distinct_speaker | high | keep | The audio features the interviewer (Joe Rogan) saying 'Wow', which correctly matches the assigned speaker. |
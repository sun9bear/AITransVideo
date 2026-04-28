# P2-b Speaker Attribution Judgement Partial Summary

- Audit candidates: `115`
- Judged unique candidates: `44`
- Coverage: `38.3%`
- Jobs covered: `8`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 21 |
| `distinct_speaker` | 11 |
| `asr_speaker` | 6 |
| `music_or_non_speech` | 5 |
| `main_speaker` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 37 |
| `mark_non_speech` | 5 |
| `mark_review` | 1 |
| `reassign_to_main` | 1 |

## Non-Keep Decisions

| Candidate | Job | Segment | Assigned | Decision | Action | Confidence | Reason |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `job_524c5253ad514692bcc06aa170f9567f_cand_001` | `job_524c5253ad514692bcc06aa170f9567f` | 87 | speaker_c | music_or_non_speech | mark_non_speech | high | The audio is a background song playing, not a speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_003` | `job_524c5253ad514692bcc06aa170f9567f` | 101 | speaker_d | music_or_non_speech | mark_non_speech | high | The audio consists of crowd chanting and cheering, not a distinct speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_004` | `job_524c5253ad514692bcc06aa170f9567f` | 82 | speaker_c | music_or_non_speech | mark_non_speech | high | The audio is a background song playing, not a speaker. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_001` | `job_fe50b1a0743d4585ba06ebf9cfb476bd` | 87 | speaker_d | music_or_non_speech | mark_non_speech | high | The audio is a song playing in the background, not spoken dialogue. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_005` | `job_fe50b1a0743d4585ba06ebf9cfb476bd` | 100 | speaker_a | music_or_non_speech | mark_non_speech | high | The audio consists of a crowd chanting and cheering, not the assigned speaker. |
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_001` | `job_2439b210fc104f5f926a5d4f53e7e572` | 33 | speaker_c | distinct_speaker | mark_review | high | The speaker is clearly the interviewer (Joe Rogan), but is assigned to an 'unknown speaker' profile. It should likely be merged with speaker_a. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_004` | `job_650a4d40b8eb423bb5307f3fe6d52acd` | 19 | speaker_a | main_speaker | reassign_to_main | high | The voice saying 'Uh, again, trite, right?' is clearly the main speaker (Naval Ravikant), continuing his previous thought. |

## Implications

- `music_or_non_speech -> mark_non_speech` appears repeatedly in Muniba audience/music segments; P2 should treat music/crowd chanting as non-speech risk, not as a speaker to clone.
- Several low-share speakers are real distinct speakers; deterministic low-share merge would be harmful.
- A small number of short segments are actually main speaker continuations; verifier-based reassignment is justified, but only with high-confidence audio evidence.
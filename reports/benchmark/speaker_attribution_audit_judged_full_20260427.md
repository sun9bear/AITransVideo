# P2-b Speaker Attribution Judgement Full Summary

- Audit candidates: `115`
- Judged unique candidates: `115`
- Coverage: `100.0%`
- Jobs covered: `21`
- Non-keep: `8` (`7.0%`)

## Decision Counts

| Decision | Count |
| --- | ---: |
| `s2_speaker` | 60 |
| `distinct_speaker` | 36 |
| `asr_speaker` | 12 |
| `music_or_non_speech` | 5 |
| `main_speaker` | 2 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `keep` | 107 |
| `mark_non_speech` | 5 |
| `reassign_to_main` | 2 |
| `mark_review` | 1 |

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
| `job_730e7195e9c04ed9906d744f62f2986e_cand_002` | `job_730e7195e9c04ed9906d744f62f2986e` | 111 | speaker_audience | main_speaker | reassign_to_main | high | The text 'My book...' is spoken by the main speaker in response to an audience member shouting 'Your book'. |

## Reason To Action Counts

| Reason | Actions |
| --- | --- |
| `audience_unknown_or_non_speech_profile` | keep:33, mark_non_speech:3, mark_review:1 |
| `duplicate_source_group` | keep:61, mark_non_speech:5, mark_review:1, reassign_to_main:1 |
| `force_dsp` | keep:90, mark_non_speech:4, reassign_to_main:2, mark_review:1 |
| `fragmented_speaker` | keep:6, mark_non_speech:5 |
| `incidental_speaker` | keep:1 |
| `long_low_support_segment` | keep:17, mark_non_speech:5, mark_review:1 |
| `low_duration_share` | keep:34, mark_non_speech:4, reassign_to_main:2 |
| `low_segment_count` | keep:14, reassign_to_main:1 |
| `needs_review` | keep:36, mark_non_speech:4, reassign_to_main:2, mark_review:1 |
| `non_primary_speaker` | keep:107, mark_non_speech:5, reassign_to_main:2, mark_review:1 |
| `short_interaction` | keep:88, reassign_to_main:1 |

## Implications

- Full judgement strongly rejects deterministic low-share speaker merging: `keep` dominates the reviewed candidates.
- The clear automated action is non-speech/music/crowd handling, especially when the model returns high-confidence `music_or_non_speech`.
- Main-speaker reassignment exists but is rare; it should remain verifier-gated instead of becoming a broad structural rule.
- P2 production work should first add non-speech speaker role suppression and review hints, then tighten verifier reporting and only later consider auto-reassignment thresholds.
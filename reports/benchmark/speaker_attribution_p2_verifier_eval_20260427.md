# P2 Speaker Attribution Judgement Summary

- Generated at: `2026-04-27T14:28:42.894148+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Audit candidates: `115`
- Judged unique candidates: `115`
- Coverage: `100.0%`
- Jobs covered: `21`
- Non-keep: `8` (`7.0%`)
- Duplicate decisions ignored: `12`
- Judge errors: `0`

## Go / No-Go

| Item | Decision | Reason |
| --- | --- | --- |
| `broad_low_support_auto_merge` | `NO_GO` | 93.0% of judged candidates were keep, and only 1.7% were main_speaker. |
| `verifier_gated_main_reassignment` | `CAUTIOUS_GO` | Allow only medium/high-confidence local audio verifier main_speaker decisions; observed count=2. |
| `non_speech_profile_marking` | `GO` | Use high-confidence music/non-speech decisions to mark complete low-support non-dialogue speakers; observed count=5. |
| `phrase_or_title_specific_rules` | `NO_GO` | Judged failures span mixed presenter, host, audience, music, and guest cases. |

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
| `job_2439b210fc104f5f926a5d4f53e7e572_cand_001` | `job_2439b210fc104f5f926a5d4f53e7e572` | 33 | speaker_c / 未知说话人1 | distinct_speaker | mark_review | high | The speaker is clearly the interviewer (Joe Rogan), but is assigned to an 'unknown speaker' profile. It should likely be merged with speaker_a. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_004` | `job_524c5253ad514692bcc06aa170f9567f` | 82 | speaker_c / 未知说话人1 | music_or_non_speech | mark_non_speech | high | The audio is a background song playing, not a speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_001` | `job_524c5253ad514692bcc06aa170f9567f` | 87 | speaker_c / 未知说话人1 | music_or_non_speech | mark_non_speech | high | The audio is a background song playing, not a speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_003` | `job_524c5253ad514692bcc06aa170f9567f` | 101 | speaker_d / 致辞嘉宾 | music_or_non_speech | mark_non_speech | high | The audio consists of crowd chanting and cheering, not a distinct speaker. |
| `job_650a4d40b8eb423bb5307f3fe6d52acd_cand_004` | `job_650a4d40b8eb423bb5307f3fe6d52acd` | 19 | speaker_a / 乔·罗根 | main_speaker | reassign_to_main | high | The voice saying 'Uh, again, trite, right?' is clearly the main speaker (Naval Ravikant), continuing his previous thought. |
| `job_730e7195e9c04ed9906d744f62f2986e_cand_002` | `job_730e7195e9c04ed9906d744f62f2986e` | 111 | speaker_audience / 马特·亚伯拉罕斯 | main_speaker | reassign_to_main | high | The text 'My book...' is spoken by the main speaker in response to an audience member shouting 'Your book'. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_001` | `job_fe50b1a0743d4585ba06ebf9cfb476bd` | 87 | speaker_d / 背景音乐 | music_or_non_speech | mark_non_speech | high | The audio is a song playing in the background, not spoken dialogue. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_005` | `job_fe50b1a0743d4585ba06ebf9cfb476bd` | 100 | speaker_a / 男主持人 | music_or_non_speech | mark_non_speech | high | The audio consists of a crowd chanting and cheering, not the assigned speaker. |

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

- Broad deterministic low-support speaker merging remains rejected by this sample.
- Main-speaker reassignment should stay gated by local audio verifier decisions.
- Non-speech/music/crowd handling is the clearest production path, but only for complete low-support speakers or explicit review flags.
- Phrase, title, person-name, or fixed-line rules should not be added from this report.

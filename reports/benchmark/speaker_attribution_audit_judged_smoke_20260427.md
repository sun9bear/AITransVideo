# P2-b Speaker Attribution Model Judgement

- Generated at: `2026-04-27T11:20:23.436283+00:00`
- Audit batch: `reports/benchmark/speaker_attribution_audit_batch_20260427.json`
- Review model: `gemini_pro`
- Candidates loaded: `8`
- Decisions: `8`

## Decision Counts

| Decision | Count |
| --- | ---: |
| `music_or_non_speech` | 4 |
| `distinct_speaker` | 3 |
| `asr_speaker` | 1 |

## Recommended Actions

| Action | Count |
| --- | ---: |
| `mark_non_speech` | 4 |
| `keep` | 4 |

## Decisions

| Candidate | Decision | Confidence | Action | Reason |
| --- | --- | --- | --- | --- |
| `job_524c5253ad514692bcc06aa170f9567f_cand_001` | music_or_non_speech | high | mark_non_speech | The audio is a background song playing, not a speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_002` | asr_speaker | high | keep | The audio is clearly the host speaking over the music. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_003` | music_or_non_speech | high | mark_non_speech | The audio consists of crowd chanting and cheering, not a distinct speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_004` | music_or_non_speech | high | mark_non_speech | The audio is a background song playing, not a speaker. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_005` | distinct_speaker | high | keep | The audio clearly features a distinct male voice from the audience shouting the phrase. |
| `job_524c5253ad514692bcc06aa170f9567f_cand_006` | distinct_speaker | high | keep | The audio features a distinct male guest speaker, correctly assigned. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_001` | music_or_non_speech | high | mark_non_speech | The audio is a song playing in the background, not spoken dialogue. |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd_cand_002` | distinct_speaker | high | keep | The audio clearly features a distinct male voice from the audience shouting the phrase. |
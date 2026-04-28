# P2 Speaker Attribution Convergence Report

- Generated at: `2026-04-27T10:53:15.351040+00:00`
- Projects root: `/mnt/HC_Volume_105524101/aivideotrans/projects`
- Jobs scanned: `30`
- High-risk jobs: `19`
- Duplicate source groups: `5`

## Current Read

- Do not add phrase-cue or single-video rules. Recent failures are mixed structural cases: presenter, host, audience, music, and guest segments can all coexist in one source.
- Deterministic profiling is useful for observation and UI hints, but should not auto-merge all low-share speakers in multi-speaker videos.
- P2-b should stay local-verifier based: only high-risk low-support candidates are allowed to change speaker assignment, and uncertain decisions should keep the existing assignment.

## Duplicate Runs

| Source | Runs | Best By Cost | Best By Speaker |
| --- | ---: | --- | --- |
| `25c702ac1a1e` | 3 | `job_b8a76a5a8ab64c03a4478602c45b6032` | `job_b8a76a5a8ab64c03a4478602c45b6032` |
| `29d30c18ed89` | 3 | `job_abb3fedc905c403db67d784c480c170e` | `job_2f3f66698b294bdd9025aab28b2adac7` |
| `d5d1ff5f9643` | 2 | `job_fe50b1a0743d4585ba06ebf9cfb476bd` | `job_fe50b1a0743d4585ba06ebf9cfb476bd` |
| `f83c54bffd81` | 2 | `job_198ffd877c4044869b2f0db988c424fe` | `job_198ffd877c4044869b2f0db988c424fe` |
| `a7caa3c85f5f` | 2 | `job_925ad5833c764d2ab386281b58a6c18f` | `job_67009391e2d1425eb52728aca4995130` |

## Job Summary

| Job | Title | Speakers | Primary | Roles | Force DSP | Needs Review | Rewrite | Risk |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | --- |
| `job_524c5253ad514692bcc06aa170f9567f` | Muniba mazari speech at vcon 2017 dubai | 4 | 89.5% | fragmented:3, primary:1 | 18 | 17 | 28 | high |
| `job_fe50b1a0743d4585ba06ebf9cfb476bd` | Muniba mazari speech at vcon 2017 dubai | 4 | 89.5% | fragmented:2, incidental:1, primary:1 | 16 | 14 | 32 | high |
| `job_8d78c2685351464192b41828c2cda049` | The Best Life Advice from Charlie Munger Final I | 2 | 77.8% | unknown:2 | 34 | 17 | 16 | high |
| `job_2439b210fc104f5f926a5d4f53e7e572` | The Most Life-Changing 20 Minute Video You’ll Ev | 3 | 92.2% | unknown:3 | 28 | 15 | 36 | high |
| `job_6940aeac0c7b442f809ef8fa1925302f` | How Anthropic’s product team moves faster than a | 2 | 71.7% | unknown:2 | 87 | 74 | 94 | high |
| `job_650a4d40b8eb423bb5307f3fe6d52acd` | The Most Life-Changing 20 Minute Video You’ll Ev | 3 | 92.4% | unknown:3 | 43 | 34 | 21 | high |
| `job_3066774da2a64848b2f4d1d2824e022b` | Jensen Huang – Will Nvidia’s moat persist? | 2 | 76.1% | unknown:2 | 114 | 73 | 78 | high |
| `job_f08cabc1267642b98a9d774a9e2a5da4` | Watch CNBC's full interview with Berkshire Hatha | 4 | 71.7% | unknown:4 | 188 | 130 | 178 | high |
| `job_45179e4a2aa842329ac4e24be6fc3421` | How To Build A Company With AI From The Ground U | 1 | 100.0% | unknown:1 | 2 | 2 | 9 | low |
| `job_730e7195e9c04ed9906d744f62f2986e` | Think Faster, Talk Smarter with Matt Abrahams | 2 | 99.8% | unknown:2 | 22 | 17 | 64 | high |
| `job_b96b20d250934c1caa58d0923b320a4f` | Warren Buffett On The 2008 Crisis | 2 | 82.2% | unknown:2 | 27 | 15 | 33 | high |
| `job_7e12f5f49ed04b9b8bc3d1a003b61290` | Nvidia CEO Jensen Huang on AI's pressure on soft | 2 | 85.7% | unknown:2 | 1 | 0 | 4 | low |
| `job_198ffd877c4044869b2f0db988c424fe` | Nvidia CEO Jensen Huang on AI's pressure on soft | 2 | 85.7% | unknown:2 | 0 | 0 | 0 | low |
| `job_925ad5833c764d2ab386281b58a6c18f` | Why this Chinese EV terrifies Europe’s carmakers | 10 | 43.2% | unknown:10 | 11 | 2 | 31 | high |
| `job_ce814b8cdc2242d7b72764b2f0b72dd4` | The Best Life Advice from Charlie Munger Final I | 2 | 78.9% | unknown:2 | 24 | 7 | 39 | high |
| `job_67009391e2d1425eb52728aca4995130` | Why this Chinese EV terrifies Europe’s carmakers | 10 | 42.4% | unknown:10 | 13 | 5 | 31 | high |
| `job_877146b2c9bc4e67816c2cc2e472413f` | Nvidia CEO Jensen Huang: AI is going to fundamen | 3 | 63.0% | unknown:3 | 2 | 2 | 13 | low |
| `job_abb3fedc905c403db67d784c480c170e` | The Best Life Advice from Charlie Munger Final I | 2 | 77.8% | unknown:2 | 22 | 5 | 38 | high |
| `job_d3ca1fb8bf0b42efb906be716ac6995c` | The Best Life Advice from Charlie Munger Final I | 2 | 78.9% | unknown:2 | 24 | 8 | 37 | high |
| `job_74860805483b4596877b8706195a5e30` | What the Fed Can Do About Anthropic’s Latest Sys | 5 | 48.1% | unknown:5 | 0 | 0 | 12 | low |
| `job_a621ae11e0d14f19be28aa568477c69c` | Why Anthropic’s Mythos Is Sparking Alarm | 8 | 33.0% | unknown:8 | 1 | 1 | 35 | low |
| `job_187fcd0a44164734898466a4bcc26ea9` | Top 10 Trump voter groups now turning on him | 1 | 100.0% | unknown:1 | 0 | 0 | 15 | low |
| `job_ddba907f1cb146c5b8f9914efe4a7ab2` | ‘NEVER underestimate Chinese models’: Netskope C | 3 | 55.3% | unknown:3 | 1 | 0 | 18 | low |
| `job_b98bdc65841f4254bdc2a410eb6e0939` | Extended interview: Jamie Dimon on AI, Iran and  | 2 | 77.6% | unknown:2 | 44 | 25 | 48 | high |
| `job_ef0a73e543e64b428dbeb3b3f5741b4f` | Disappearance of UFO expert Gen. Neil McCasland  | 2 | 55.4% | unknown:2 | 1 | 0 | 6 | low |
| `job_48a6013c9782424aa424ae8803480b41` | Just a regular billionaire | 3 | 52.3% | unknown:3 | 36 | 24 | 10 | high |
| `job_2f3f66698b294bdd9025aab28b2adac7` | The Best Life Advice from Charlie Munger Final I | 2 | 78.9% | unknown:2 | 28 | 28 | 18 | high |
| `job_58a6fb6a7eac46dc9e5d2811ded8bd38` | Charlie Munger interview: You've got to learn ho | 3 | 75.9% | unknown:3 | 0 | 0 | 1 | low |
| `job_b8a76a5a8ab64c03a4478602c45b6032` | The Most Life-Changing 20 Minute Video You’ll Ev | 3 | 92.3% | unknown:3 | 17 | 17 | 31 | high |
| `job_715770b00cc34a7a87a354f5e386139b` | Don't Find A Niche. Become The Niche. | 1 | 100.0% | unknown:1 | 1 | 1 | 73 | low |

## High-Risk Speaker Details

### `job_524c5253ad514692bcc06aa170f9567f`

- Title: Muniba mazari speech at vcon 2017 dubai
- Reasons: dominant_primary_with_multiple_fragmented_speakers, high_force_dsp
- Metrics: force_dsp=18, needs_review=17, rewrite=28, pre_tts=5

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 主持人 | fragmented | 3.7% | 9 | 55.6% | low_share_secondary |
| `speaker_b` | 穆尼巴·马扎里 | primary | 89.5% | 75 | 16.0% | top_duration_speaker |
| `speaker_c` | 未知说话人1 | fragmented | 3.8% | 5 | 20.0% | low_share_secondary |
| `speaker_d` | 致辞嘉宾 | fragmented | 3.1% | 12 | 83.3% | low_share_fragmented |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 18.5s, direct/-: Guys, please have a seat and remember, listen from your hearts. Muniba, we present— I pres
  - segment 2, 11.1s, rewrite_direct/-: Okay, beautiful. Thank you, our Pakistani brothers and sisters. Have a seat. Okay, they're
  - segment 78, 4.2s, force_dsp/medium: We Malaysia, um,
- `speaker_c`:
  - segment 63, 1.8s, direct/-: You're still perfect.
  - segment 82, 22.9s, rewrite_dsp/-: Of stars. I'm going to give you my heart, heart, heart, heart, heart, 'cause you're a sky—
  - segment 83, 22.5s, force_dsp/high: I don't care who want to tear me apart. I don't care if you do, you do, you do, you do. 'C
- `speaker_d`:
  - segment 89, 0.2s, capped_dsp_overflow/medium: Um,
  - segment 90, 0.1s, capped_dsp_overflow/medium: when
  - segment 91, 1.4s, force_dsp/low: I don't know what you see.

### `job_fe50b1a0743d4585ba06ebf9cfb476bd`

- Title: Muniba mazari speech at vcon 2017 dubai
- Reasons: dominant_primary_with_multiple_fragmented_speakers, high_force_dsp, incidental_speaker_detected
- Metrics: force_dsp=16, needs_review=14, rewrite=32, pre_tts=3

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 男主持人 | fragmented | 6.8% | 21 | 71.4% | low_share_fragmented |
| `speaker_b` | 穆尼巴·马扎里 | primary | 89.5% | 74 | 14.9% | top_duration_speaker |
| `speaker_c` | 观众 | incidental | 0.1% | 1 | 100.0% | low_share_short_interactions |
| `speaker_d` | 背景音乐 | fragmented | 3.7% | 4 | 0.0% | low_share_secondary |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 18.5s, rewrite_direct/-: Guys, please have a seat and remember, listen from your hearts. Muniba, we present— I pres
  - segment 2, 11.1s, force_dsp/high: Okay, beautiful. Thank you, our Pakistani brothers and sisters. Have a seat. Okay, they're
  - segment 78, 4.2s, force_dsp/medium: We Malaysia, um,
- `speaker_c`:
  - segment 63, 1.8s, dsp/-: You're still perfect.
- `speaker_d`:
  - segment 82, 23.1s, rewrite_dsp/-: —full of stars. I'm going to give you my heart, heart, heart, heart, 'cause you're a sky—
  - segment 83, 22.5s, rewrite_direct/-: I don't care who want to tear me apart. I don't care if you do, you do, you do, you do. 'C
  - segment 84, 19.9s, rewrite_dsp/-: Cuz you're a sky— cuz you're a sky full of stars. Stars. I want to die in your arms. Arms.

### `job_8d78c2685351464192b41828c2cda049`

- Title: The Best Life Advice from Charlie Munger Final Interview
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=34, needs_review=17, rewrite=16, pre_tts=5

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 贝基·奎克 |  | 22.2% | 28 | 75.0% |  |
| `speaker_b` | 查理·芒格 |  | 77.8% | 24 | 33.3% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 17.4s, dsp/-: Part of the advice that you give people in life is pretty basic stuff, but if you follow i
  - segment 3, 4.1s, force_dsp/medium: I have a hard time thinking of a time when you set your expectations low though.
  - segment 5, 0.3s, force_dsp/low: How so?

### `job_2439b210fc104f5f926a5d4f53e7e572`

- Title: The Most Life-Changing 20 Minute Video You’ll Ever Watch | Naval Ravikant
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=28, needs_review=15, rewrite=36, pre_tts=21

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 乔·罗根 |  | 1.9% | 7 | 85.7% |  |
| `speaker_b` | 纳瓦尔·拉维坎特 |  | 92.2% | 31 | 12.9% |  |
| `speaker_c` | 未知说话人1 |  | 5.9% | 16 | 87.5% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 1.2s, force_dsp/low: What is the meaning of life?
  - segment 7, 16.3s, direct/-: The meaning of life. It's funny that that's the basis of all existential angst, that you d
  - segment 15, 1.1s, force_dsp/low: You got happy before the money?
- `speaker_c`:
  - segment 5, 1.8s, force_dsp/low: And that can also be captured by your own past.
  - segment 10, 24.7s, direct/-: Uh, there's an interesting challenge where I think people need to avoid becoming, uh, a su
  - segment 13, 4.6s, force_dsp/medium: Right, the outcome may have been the same, but the entire experience of getting there—

### `job_6940aeac0c7b442f809ef8fa1925302f`

- Title: How Anthropic’s product team moves faster than anyone else | Cat Wu (Head of Product, Claude Code)
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=87, needs_review=74, rewrite=94, pre_tts=17

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 凯特·吴 |  | 71.7% | 117 | 17.9% |  |
| `speaker_b` | 伦尼 |  | 28.3% | 96 | 37.5% |  |

Sample non-primary segments:
- `speaker_b`:
  - segment 2, 4.1s, force_dsp/medium: I've never seen anything like the pace you folks at Anthropic are shipping at.
  - segment 4, 5.0s, dsp/-: You're interviewing hundreds of PMs and you just keep feeling like they're approaching it
  - segment 6, 3.4s, force_dsp/medium: What do you think are the emerging skills PMs need to develop?

### `job_650a4d40b8eb423bb5307f3fe6d52acd`

- Title: The Most Life-Changing 20 Minute Video You’ll Ever Watch | Naval Ravikant
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=43, needs_review=34, rewrite=21, pre_tts=3

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 乔·罗根 |  | 2.1% | 7 | 85.7% |  |
| `speaker_b` | 纳瓦尔·拉维坎特 |  | 92.4% | 40 | 7.5% |  |
| `speaker_c` | 克里斯·威廉姆森 |  | 5.6% | 12 | 83.3% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 1.2s, force_dsp/low: What is the meaning of life?
  - segment 7, 16.3s, force_dsp/high: The meaning of life. It's funny that that's the basis of all existential angst, that you d
  - segment 15, 1.1s, force_dsp/low: You got happy before the money?
- `speaker_c`:
  - segment 5, 1.8s, force_dsp/low: And that can also be captured by your own past.
  - segment 10, 24.7s, force_dsp/high: Uh, there's an interesting challenge where I think people need to avoid becoming, uh, a su
  - segment 13, 4.6s, force_dsp/medium: Right, the outcome may have been the same, but the entire experience of getting there—

### `job_3066774da2a64848b2f4d1d2824e022b`

- Title: Jensen Huang – Will Nvidia’s moat persist?
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=114, needs_review=73, rewrite=78, pre_tts=15

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 德瓦克什·帕特尔 |  | 23.9% | 104 | 62.5% |  |
| `speaker_b` | 黄仁勋 |  | 76.1% | 135 | 39.3% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 31.0s, dsp/-: We've seen the valuations of a bunch of software companies crash because people are expect
  - segment 6, 31.9s, direct/-: Um, I think in your latest filings it was, you had almost $100 billion in purchase commitm
  - segment 10, 12.4s, direct/-: I do want to understand more concretely whether the upstream can keep up. Um, for many yea

### `job_f08cabc1267642b98a9d774a9e2a5da4`

- Title: Watch CNBC's full interview with Berkshire Hathaway CEO Warren Buffett
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=188, needs_review=130, rewrite=178, pre_tts=38

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 贝基·奎克 |  | 23.1% | 172 | 57.6% |  |
| `speaker_b` | 沃伦·巴菲特 |  | 71.7% | 213 | 23.9% |  |
| `speaker_c` | 乔·科南 |  | 2.5% | 9 | 33.3% |  |
| `speaker_d` | 安德鲁·罗斯·索尔金 |  | 2.6% | 9 | 22.2% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 33.8s, rewrite_dsp/-: We are here in Omaha, Nebraska, this morning with Warren Buffett, the chairman and CEO of
  - segment 3, 18.7s, dsp/-: I want to talk about the letter. Uh, obviously one of the things that you touch on in the
  - segment 6, 35.3s, dsp/-: Although there are a lot of people who look at the market and they say, look, I want to bu
- `speaker_c`:
  - segment 115, 60.0s, dsp/-: Here's the latest on the coronavirus. China reported an additional 150 deaths and 409 new
  - segment 116, 23.0s, dsp/-: at this point, I mean, Boris Johnson out earlier today, Andrew, saying that the risk to UK
  - segment 119, 1.5s, force_dsp/low: I, I just wonder, we don't know.
- `speaker_d`:
  - segment 117, 34.0s, rewrite_dsp/-: Absolutely. And that's dampening global growth. What worries me is that we don't know— 3,
  - segment 120, 23.7s, dsp/-: There are major travel that's being changed right now. Conferences are being canceled. The
  - segment 224, 12.4s, force_dsp/high: I was just gonna follow up on that question, Warren, which was about a year ago we had ask

### `job_730e7195e9c04ed9906d744f62f2986e`

- Title: Think Faster, Talk Smarter with Matt Abrahams
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=22, needs_review=17, rewrite=64, pre_tts=13

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 马特·亚伯拉罕斯 |  | 99.8% | 121 | 14.1% |  |
| `speaker_audience` | 马特·亚伯拉罕斯 |  | 0.2% | 2 | 100.0% |  |

Sample non-primary segments:
- `speaker_audience`:
  - segment 80, 0.9s, force_dsp/low: What are the kids' names?
  - segment 111, 4.2s, force_dsp/medium: My book— well, look at that. Well, thank you. All right.

### `job_b96b20d250934c1caa58d0923b320a4f`

- Title: Warren Buffett On The 2008 Crisis
- Reasons: high_force_dsp, missing_speaker_structure_metadata
- Metrics: force_dsp=27, needs_review=15, rewrite=33, pre_tts=10

| Speaker | Name | Role | Share | Segments | Short Rate | Reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `speaker_a` | 安德鲁·罗斯·索尔金 |  | 17.8% | 31 | 67.7% |  |
| `speaker_b` | 沃伦·巴菲特 |  | 82.2% | 39 | 28.2% |  |

Sample non-primary segments:
- `speaker_a`:
  - segment 1, 10.5s, rewrite_direct/-: I want to just go back to 2008 for a moment and just even start with a very big picture se
  - segment 5, 9.8s, direct/-: It's early 2008, hedge funds, uh, with a lot of subprime mortgages are going under. There
  - segment 8, 7.6s, dsp/-: So take us back. I don't know if you remember this. This is after Bear Stearns goes down.

## P2 Next Step

1. Freeze these jobs as the first P2 speaker convergence set before changing more rules.
2. Add a replay check that flags `dominant primary + multiple fragmented low-share speakers` without automatically merging them.
3. Tighten P2-b verifier reporting: candidate count, decision distribution, and before/after speaker assignment must be visible per job.
4. Only after the report shows stable false-positive patterns should we adjust verifier trigger thresholds. Do not add text phrase rules.

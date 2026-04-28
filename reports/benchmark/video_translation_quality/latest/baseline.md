# Video Translation Quality Baseline

- Jobs: 12
- Segments: 993
- Rewrite segment rate: 21.35%
- Pre-TTS contradiction rate: 70.11%
- Speaker corrections: 58
- Pre-TTS contradiction root-cause proxy: {'missing_pre_post_chars': 61}

## Duration CDF

| Metric | p10 | p25 | p50 | p75 | p90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| All segments target ms | 1280.0 | 3520.0 | 12890.0 | 28890.0 | 46930.0 |
| Speaker correction ms | 1368.0 | 3420.0 | 11049.5 | 18445.0 | 32548.0 |

## Provider Summary

| Provider / model / mode | jobs | segments | rewrite segments | rewrite count | contradictions |
| --- | ---: | ---: | ---: | ---: | ---: |
| cosyvoice/cosyvoice-v3-flash/express | 1 | 10 | 7 | 11.0 | 1 |
| cosyvoice/unknown/unknown | 1 | 12 | 0 | 0.0 | 0 |
| minimax/speech-2.8-hd/studio | 1 | 72 | 30 | 31.0 | 19 |
| minimax/speech-2.8-turbo/studio | 2 | 272 | 59 | 61.0 | 41 |
| minimax/unknown/unknown | 2 | 486 | 116 | 192.0 | 0 |
| unknown/unknown/unknown | 3 | 62 | 0 | 0.0 | 0 |
| volcengine/unknown/unknown | 2 | 79 | 0 | 0.0 | 0 |

## Cost Model

- LLM rewrite cost proxy: CNY 0.0885
- TTS rewrite cost proxy: CNY 5.9 - 20.65
- Manual speaker fix proxy: CNY 174.0
- One avoided TTS rewrite equals about 66.67 - 233.33 extra LLM rewrite-sized calls.

# Translation Quality Benchmark

- Status: `completed`
- Judge: `gpt54`
- Samples: `24`

## Model Ranking

| Rank | Model | Overall | Quality | Constraints | Speed | Reliability | Avg Latency | Cost |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Gemini 3.1 Flash Lite（快速稳定） (`gemini_31_flash_lite`) | 86.81 | 83.50 | 83.58 | 100.00 | 100.00 | 2903 ms | ¥0.25/百万 token |
| 2 | Gemini 3.1 Pro（高质量） (`gemini_pro`) | 83.81 | 86.00 | 92.74 | 40.00 | 100.00 | 32387 ms | ¥2.4/h 音频 |
| 3 | DeepSeek V4 Flash（快速） (`deepseek`) | 82.54 | 79.67 | 76.47 | 92.86 | 100.00 | 6411 ms | $0.14/$0.28 每百万 token |
| 4 | MiMo-V2.5（全模态） (`mimo_v25`) | 82.48 | 79.58 | 81.13 | 85.83 | 100.00 | 9867 ms | Token Plan 1x（音频已验证） |
| 5 | MiMo-V2.5-Pro（Agent 文本） (`mimo_v25_pro`) | 80.03 | 80.00 | 79.25 | 61.42 | 100.00 | 21861 ms | Token Plan 2x（音频 payload 未开放） |

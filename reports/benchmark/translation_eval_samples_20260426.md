# Translation Evaluation Samples

- Version: `translation_model_eval_samples.v1`
- Samples: `24` from `20` selected jobs
- Candidate windows: `1710` from `58` scanned jobs
- Translation prompt source: `runtime default`
- Rewrite prompt source: `runtime default`
- Rewrite cases with real timing: `26`

## Coverage

```json
{
  "samples": 24,
  "jobs_scanned": 58,
  "jobs_selected": 20,
  "candidate_windows": 1710,
  "selection_reasons": {
    "glossary_hit": 21,
    "high_density": 23,
    "long_segment": 17,
    "low_density_oral": 24,
    "multi_speaker": 20,
    "numeric_financial": 16,
    "rewrite_risk": 15,
    "short_backchannel": 6,
    "technical_terms": 11
  },
  "service_modes": {
    "express": 3,
    "studio": 10,
    "unknown": 11
  },
  "translation_prompt_sources": {
    "runtime default": 24
  },
  "rewrite_cases": 26
}
```

## Samples

| Sample | Reasons | Title | Segments | Seconds | Words | Glossary Hits | Prompt Hash |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| trans_eval_001 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment, technical_terms | Jim Jordan reacts to Eric Swalwell's resignation \| Katie Pavlich Tonight | 5 | 180.7 | 617 | Jamie Raskin, Robert Hur, ActBlue, Dana Remus, Covington | `31719deaca5078cb` |
| trans_eval_002 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment, technical_terms | Warren Buffett officially steps down as CEO of Berkshire Hathaway | 5 | 163.2 | 480 | Warren Buffett, Berkshire Hathaway, Greg Abel, CEO, S&P | `3a23821d83f9c03d` |
| trans_eval_003 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, short_backchannel, long_segment, technical_terms | Jensen Huang – Will Nvidia’s moat persist? | 5 | 95.9 | 270 | ASIC, CUDA | `1fe209544ed0a34e` |
| trans_eval_004 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment | Watch CNBC's full interview with Berkshire Hathaway Chairman Warren Buf... | 5 | 117.0 | 307 | Glide Foundation, Warren Buffett, Stephen Curry, Cecil Williams | `b8b31a3e73c64467` |
| trans_eval_005 | glossary_hit, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment | Warren Buffett says he hasn’t spoken to Bill Gates since ‘whole thing’... | 5 | 94.6 | 336 | Epstein files, Warren Buffett, Bill Gates, Berkshire Hathaway, Gates Foundation, CNBC | `5ed63403eedc03ab` |
| trans_eval_006 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, short_backchannel, long_segment | Retired U.S. Navy Rear Admiral Tim Gallaudet on Grusch, UAP claims: Ful... | 5 | 97.7 | 293 | UAP, Go fast video, U.S. Naval Observatory, Sol Foundation | `78ddf8bdd40b8c36` |
| trans_eval_007 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, long_segment, technical_terms | Extended interview: Jamie Dimon on AI, Iran and more | 5 | 127.1 | 471 | Walter Cronkite, The Jetsons, EQ | `aa4a997039670024` |
| trans_eval_008 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, long_segment | How Pakistan, China Played Roles in US-Iran Ceasefire | 5 | 92.3 | 285 | Pakistan, China, Iran, US, New York Times, JCPOA, Joint Comprehensive Plan of Action, Non-Proliferation Treaty, Qatar, Oman | `2de6fa80459321a5` |
| trans_eval_009 | glossary_hit, high_density, low_density_oral, multi_speaker, rewrite_risk, short_backchannel | Charlie Munger’s Life Advice: Let Go of Things That Don’t Matter \| Fina... | 5 | 46.1 | 128 | Warren, Munger | `4c90970476f40701` |
| trans_eval_010 | glossary_hit, high_density, low_density_oral, rewrite_risk, long_segment, technical_terms | The One-Person Business Model (How To Productize Yourself) | 5 | 134.3 | 450 | David Hasselhoff | `7b27e5a375534ebf` |
| trans_eval_011 | glossary_hit, high_density, low_density_oral, multi_speaker, rewrite_risk | Elon Musk's 5 step process for making things in a better way | 5 | 112.5 | 336 | five step process | `98921d32e15398b5` |
| trans_eval_012 | numeric_financial, high_density, low_density_oral, multi_speaker, short_backchannel | Charlie Munger on Staying Sane While Getting Rich \| Final Interview wit... | 5 | 38.2 | 132 |  | `0437dcb93b73f268` |
| trans_eval_013 | glossary_hit, numeric_financial, low_density_oral | Mihaly Csikszentmihalyi: Flow, the secret to happiness | 5 | 102.8 | 245 | Albert Einstein, Susan Jackson, flow experience | `d39c69d8e6fd7bf5` |
| trans_eval_014 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment, technical_terms | Warren Buffett officially steps down as CEO of Berkshire Hathaway | 5 | 161.8 | 475 | Warren Buffett, Berkshire Hathaway, Greg Abel, Omaha, Nebraska | `ae9ce8d68deba8d6` |
| trans_eval_015 | numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, short_backchannel, long_segment, technical_terms | Watch CNBC's full interview with Berkshire Hathaway Chairman Warren Buf... | 5 | 105.9 | 300 |  | `50fa3fd8a783179d` |
| trans_eval_016 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, long_segment, technical_terms | Jim Jordan reacts to Eric Swalwell's resignation \| Katie Pavlich Tonight | 5 | 180.7 | 619 | Jamie Raskin, Robert Hur, ActBlue, House Administration Committee, Dana Remus, Covington | `8ccbdbc8e0d5c17d` |
| trans_eval_017 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, long_segment, technical_terms | Extended interview: Jamie Dimon on AI, Iran and more | 5 | 127.1 | 470 | EQ | `0b5d37bb4a783e39` |
| trans_eval_018 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, long_segment | How Pakistan, China Played Roles in US-Iran Ceasefire | 5 | 92.3 | 284 | Pakistan, China, Iran, US, New York Times, JCPOA, Joint Comprehensive Plan of Action, Russia, Qatar, Oman, non Nuclear Proliferation Treaty | `d15b8fc40aac82ee` |
| trans_eval_019 | high_density, low_density_oral, rewrite_risk | Elon Musk's 5 step process for making things in a better way | 5 | 112.5 | 336 |  | `301058bc80d86d75` |
| trans_eval_020 | glossary_hit, high_density, low_density_oral, multi_speaker, short_backchannel, long_segment | Retired U.S. Navy Rear Admiral Tim Gallaudet on Grusch, UAP claims: Ful... | 5 | 83.5 | 251 | UAP, Schumer Amendment | `6902fc282cd3282d` |
| trans_eval_021 | glossary_hit, numeric_financial, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment, technical_terms | Jensen Huang – Will Nvidia’s moat persist? | 5 | 142.4 | 456 | EUV, CUDA, TSMC | `75c78feec4eebe75` |
| trans_eval_022 | glossary_hit, high_density, low_density_oral, multi_speaker, rewrite_risk, long_segment | Warren Buffett says he hasn’t spoken to Bill Gates since ‘whole thing’... | 5 | 88.1 | 304 | Warren Buffett, Bill Gates, Berkshire Hathaway, Gates Foundation | `2f74d553ac3d7cb7` |
| trans_eval_023 | glossary_hit, numeric_financial, high_density, low_density_oral, rewrite_risk, technical_terms | The One-Person Business Model (How To Productize Yourself) | 5 | 124.5 | 368 | Robert A. Heinlein | `f17b143d763ae85e` |
| trans_eval_024 | glossary_hit, high_density, low_density_oral, multi_speaker, rewrite_risk | Charlie Munger’s Life Advice: Let Go of Things That Don’t Matter \| Fina... | 5 | 80.1 | 204 | Warren, Munger | `1e18c8d8e68baabf` |

# LINUX_STABLE_BASELINE_SUMMARY.md

# Linux Stable Baseline Summary

## 1. Current Linux Stable Baseline Capabilities

- Ubuntu 24.04 single-machine deployment baseline
- `app + caddy` runtime shell
- `Docker Compose` startup
- `systemd` service management
- HTTPS Web UI public entry
- Basic Auth on the public entry
- Job API internal-only access
- real `youtube_url` processing
- `speaker_review -> continue`
- `translation_review -> continue`
- three review paths now use the new frontend as the primary entry: `speaker_review`, `translation_review`, and `voice_review`; old Web UI remains available as fallback
- second-batch light pages are now complete; “My Projects” remains a user-facing history list from job snapshots, “Settings” remains an explanation-only page, and `internal` remains a weak entry
- completed result-summary surface
- manifest-derived whitelist downloads
- same-project minimum recovery path
- page layout and interaction optimization (phase 1) is now complete and frozen; the current-task review focus block, auto-jump after create, fixed review action area with pagination, information noise reduction, and `/projects` title prioritization are now part of the stable baseline
- single-user productization cleanup (non-multi-user foundation) is now complete and frozen; the current result area, download area, and entry hierarchy are now part of the stable baseline

## 2. Current Linux Explicit Boundaries

- single-machine only
- self-use only
- single-active-job
- `youtube_url` only
- process-backed
- no `cancel`
- no multi-user
- no frontend product redesign
- no Skill work
- no commercialization work
- no failed-job resume redesign

## 3. Completed Key Acceptance Items

- P1 runtime shell acceptance
- P2 public entry and long-running acceptance
- P3 real `youtube_url` acceptance
- real review continue acceptance
- completed result-summary acceptance
- whitelist download acceptance
- minimum recovery-path acceptance

## 4. Rules Before Any Further Development

- treat the Linux migration phase as closed
- do not expand features by default
- do not treat failed-job resume as implicitly approved scope
- redefine the next phase before resuming development
- use the current Linux baseline as the new stable reference point

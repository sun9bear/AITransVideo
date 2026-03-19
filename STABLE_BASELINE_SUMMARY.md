# STABLE_BASELINE_SUMMARY

Last updated: 2026-03-19

## Stable Baseline Includes

- Windows single-machine self-use remote workbench
- HTTPS + Basic Auth public entry through Caddy
- loopback-only Web UI and Job API behind the public entry
- real `youtube_url` submit -> review continue -> result-summary -> whitelist download
- usable runtime logs and public-entry access logs

## Explicit Boundary

- single-active-job only
- `youtube_url` only
- process-backed only
- Job API is not a public endpoint
- no `cancel`
- no Linux / multi-user / production expansion in the current baseline

## Closed Key Issues

- review write path no longer trusts frontend `project_dir`
- `/api/project-file` no longer acts as a repository-wide audio read surface
- Job API path now really applies `speakers / voice_a / voice_b`
- public-entry startup no longer reports success before minimum health is true

## Reopen Rule

If development resumes later, the next phase scope must be explicitly redefined first.

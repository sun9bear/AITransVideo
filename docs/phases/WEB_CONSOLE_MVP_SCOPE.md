# WEB_CONSOLE_MVP_SCOPE.md

# Web Console MVP Scope

## 1. Single Goal Of This Phase

This phase has only one goal:

build a single-user Web Console MVP on top of the current Linux stable baseline.

The purpose is to make the already accepted real workflow smoother to use.
It is **not** the start of full productization.

## 2. Must-Have In This Phase

Only the following pages are in scope for the first implementation batch:

- New Translation
- Current Task
- Project Detail, including results and downloads

## 3. Conditional-Only Flows

The following are not standalone primary promises. They appear only when the current task actually enters the corresponding state:

- `speaker_review`
- `translation_review`
- `voice_review` (conditional only, not a primary promised page)

## 4. Second Batch Only

The following can be considered only after the first batch is complete:

- My Projects, as a lightweight history list
- Settings, kept minimal
- weak-entry internal pages

## 5. Explicitly Out Of Scope

This phase does **not** include:

- a standalone first-level review center
- failed job resume UI
- complex search and filtering
- login / registration
- database introduction
- multi-user support
- commercialization pages
- Skill-related pages
- turning Settings into a configuration center

## 6. Acceptance Standard For This Phase

This Web Console MVP is acceptable only if all of the following remain true:

- page structure stays aligned with the current Linux stable baseline
- the UI does not pretend to support capabilities that do not actually exist
- result downloads use whitelist stable keys only
- the frontend does not use absolute `project_dir` paths as its primary identifier
- the UI does not break the current boundaries:
  - `single-active-job`
  - `youtube_url only`
  - `process-backed`
  - no failed-job resume

## 7. One-Sentence Boundary

This MVP is a thinner, clearer console for the current accepted Linux baseline, not a new product stage.

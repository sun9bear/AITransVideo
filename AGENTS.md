# AutoDub-Jianying Pipeline

## Project goal
Build a Python workflow that outputs Jianying draft projects rather than rendered MP4.

## Core architecture rules
- TTS unit is SemanticBlock, not subtitle line.
- Alignment uses DSP first, rewrite loop second.
- Subtitle retiming is mathematical, not LLM-driven.
- Prefer minimal, testable, replaceable abstractions.

## Sprint 1 constraints
- No real external APIs
- Use mocks/stubs only
- Keep code lightweight
- main.py and pytest must run

## Review guidelines
When reviewing pull requests for this repository:

- Prioritize correctness, regressions, architectural drift, and missing tests over style or naming suggestions.
- Treat the following as non-negotiable architecture rules:
  - TTS unit must remain `SemanticBlock`, not subtitle line.
  - Alignment should stay DSP-first; rewrite loops are fallback logic, not the primary mechanism.
  - Subtitle retiming should stay mathematical/deterministic, not LLM-driven.
  - The pipeline target is Jianying draft output, not direct rendered MP4 as the main deliverable.
- Treat Sprint 1 constraints as hard requirements:
  - No real external APIs, network dependencies, or production service calls in the main path or tests.
  - Prefer mocks/stubs/fakes over live integrations.
  - Keep abstractions small, replaceable, and easy to test.
- Flag changes that make `main.py` harder to run, break CLI behavior, or risk `pytest` failing in a clean local environment.
- Flag changes that introduce heavyweight frameworks, unnecessary indirection, or tightly coupled abstractions without clear benefit.
- Flag missing or weak tests when behavior changes, especially around segmentation, alignment, retiming, draft generation, and pipeline orchestration.
- Be skeptical of suggestions that move logic from deterministic code into prompts/LLM calls unless there is a strong project-specific reason.
- Prefer actionable findings with file/behavior impact. Skip low-value style feedback unless it hides a real maintenance or correctness risk.

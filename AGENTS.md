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
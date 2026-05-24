from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SAMPLE_EXTRACTOR = REPO / "src" / "services" / "voice" / "sample_extractor.py"


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_voice_sample_observability_path_does_not_import_clone_providers() -> None:
    imports = _imports_of(SAMPLE_EXTRACTOR)
    banned_prefixes = (
        "services.tts",
        "services.smart_wiring",
        "gateway.voice_selection_api",
    )

    assert not any(
        imported == prefix or imported.startswith(prefix + ".")
        for imported in imports
        for prefix in banned_prefixes
    ), (
        "Voice sample extraction/scoring must stay a local artifact-selection step; "
        "clone provider calls belong to the explicit caller gate."
    )


def test_voice_sample_scoring_shadow_does_not_enable_behavior_flag() -> None:
    source = SAMPLE_EXTRACTOR.read_text(encoding="utf-8")

    assert "voice_sample_manifest_v1" in source
    assert "voice_sample_manifest_v2" in source
    assert "AVT_VOICE_SAMPLE_MANIFEST" in source
    assert "AVT_VOICE_SAMPLE_SCORING_SHADOW" in source
    assert 'env_flag("AVT_VOICE_SAMPLE_SCORING")' not in source
    assert "hard_reject_reasons" in source

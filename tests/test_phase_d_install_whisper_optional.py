"""Guard tests: Whisper alignment dependency stays opt-in.

Phase D rollout (2026-05-06) added ``faster-whisper`` as an optional
``[whisper]`` extra in pyproject.toml. The contract:

  - Default ``pip install .`` does NOT pull faster-whisper.
  - ``pip install .[whisper]`` does.
  - The app Dockerfile gates the optional install on
    ``ARG INSTALL_WHISPER=0`` (1 → install, 0/missing → skip).
  - docker-compose.yml app service exposes ``INSTALL_WHISPER`` build
    arg + persistent ``model_cache`` volume + ``HF_HOME`` env so
    pre-warmed model weights survive container recreation.

Tests are AST-/string-level checks against the source files; no
Docker / pip invocation. Catches accidental promotion of
faster-whisper into default deps and the three matching production
config landmines we hit on rollout.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# pyproject.toml: faster-whisper is in [whisper] extra, NOT default deps
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_faster_whisper_is_not_in_default_dependencies():
    """``pip install .`` must NOT pull faster-whisper. Default
    deployments stay lean (~500 MB ctranslate2 + onnxruntime + tokenizer
    add up). Promotion to default would silently inflate every
    deployment image — gate stays opt-in via [whisper] extra."""
    cfg = _load_pyproject()
    deps = cfg.get("project", {}).get("dependencies", []) or []
    flat = " ".join(deps).lower()
    assert "faster-whisper" not in flat and "faster_whisper" not in flat, (
        f"faster-whisper leaked into default project.dependencies: {deps}. "
        "Move it back to [project.optional-dependencies] whisper."
    )


def test_whisper_optional_extra_pins_faster_whisper():
    """Sanity: the [whisper] extra IS defined and pins faster-whisper.
    Without this, ``pip install .[whisper]`` would silently no-op."""
    cfg = _load_pyproject()
    extras = cfg.get("project", {}).get("optional-dependencies", {}) or {}
    assert "whisper" in extras, (
        "Expected a [project.optional-dependencies].whisper extra in "
        "pyproject.toml so the app Dockerfile's INSTALL_WHISPER=1 branch "
        "has something to install."
    )
    whisper_deps = " ".join(extras["whisper"]).lower()
    assert "faster-whisper" in whisper_deps, (
        f"[whisper] extra exists but does not pin faster-whisper: "
        f"{extras['whisper']}"
    )


# ---------------------------------------------------------------------------
# app Dockerfile: INSTALL_WHISPER build arg + conditional install
# ---------------------------------------------------------------------------


def test_app_dockerfile_has_install_whisper_build_arg_default_zero():
    """Default ``ARG INSTALL_WHISPER=0`` means a vanilla ``docker compose
    build app`` produces a lean image (no faster-whisper). Deployments
    must explicitly opt in via ``--build-arg INSTALL_WHISPER=1``."""
    text = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # ARG line should default to 0 — anyone changing default to 1 needs
    # to also accept the +500 MB image weight tax for all tenants.
    assert re.search(r"^\s*ARG\s+INSTALL_WHISPER\s*=\s*0\s*$", text, re.M), (
        "Dockerfile must declare ``ARG INSTALL_WHISPER=0`` so default "
        "builds are lean. Found Dockerfile content does not match."
    )


def test_app_dockerfile_conditionally_installs_whisper_extra():
    """The RUN block that installs project deps must guard the
    ``.[whisper]`` install behind ``$INSTALL_WHISPER == 1``. A bare
    ``pip install .[whisper]`` would defeat the whole opt-in."""
    text = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Crude but stable pattern: shell `if [ "$INSTALL_WHISPER" = "1" ]`
    # somewhere near a `pip install .[whisper]` invocation.
    assert "INSTALL_WHISPER" in text and ".[whisper]" in text, (
        "Dockerfile should reference both INSTALL_WHISPER and .[whisper] "
        "so the conditional install is wired up."
    )
    # Stronger structural check: the .[whisper] install must NOT appear
    # outside an `if [ "$INSTALL_WHISPER"` guard — i.e. an unconditional
    # `pip install .[whisper]` should fail this test.
    bare_install = re.search(
        r'^\s*(?:RUN\s+)?pip install [^\n]*\.\[whisper\]', text, re.M,
    )
    assert bare_install is None, (
        f"Dockerfile contains an UNCONDITIONAL `pip install .[whisper]` "
        f"at line: {bare_install.group(0)!r}. Wrap it inside `if "
        '[ "$INSTALL_WHISPER" = "1" ]; then ... fi` so it stays opt-in.'
    )


def test_app_dockerfile_sets_hf_home_for_persistent_cache():
    """``HF_HOME`` env must point under ``/opt/aivideotrans/model_cache/``
    so when docker-compose.yml bind-mounts that path, pre-warmed model
    weights survive rebuilds. Without HF_HOME set, faster-whisper falls
    back to the ephemeral container default (``/root/.cache/huggingface``)."""
    text = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r"HF_HOME\s*=\s*/opt/aivideotrans/model_cache/", text,
    ), (
        "Dockerfile must set HF_HOME=/opt/aivideotrans/model_cache/... so "
        "docker-compose.yml's model_cache bind mount actually catches "
        "the model downloads. Otherwise rebuild loses ~466MB of small "
        "model weights every time."
    )


# ---------------------------------------------------------------------------
# docker-compose.yml: app service exposes the build arg + the volume
# ---------------------------------------------------------------------------


def test_compose_app_exposes_install_whisper_build_arg():
    """``docker-compose.yml`` app service must thread through the
    INSTALL_WHISPER build arg, not just rely on the Dockerfile default."""
    text = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Look for build args block under app: that includes INSTALL_WHISPER
    # We don't try to fully parse YAML; a substring + neighbor check
    # gives enough confidence.
    assert "INSTALL_WHISPER" in text, (
        "docker-compose.yml must declare INSTALL_WHISPER under app.build.args "
        "so deployments can opt in via env (.env's INSTALL_WHISPER=1)."
    )


def test_compose_app_mounts_model_cache_volume():
    """app service must bind-mount a host directory at
    ``/opt/aivideotrans/model_cache`` so pre-warmed Whisper model
    weights survive ``docker compose up -d --force-recreate app``."""
    text = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/opt/aivideotrans/model_cache" in text, (
        "docker-compose.yml must mount a host volume at "
        "/opt/aivideotrans/model_cache (target side). Without this, every "
        "container rebuild re-downloads ~466MB of `small` model weights."
    )
    # Also ensure the source is parameterized via AIVIDEOTRANS_ROOT
    # rather than hardcoded — keeps the path consistent with the
    # other code/data mounts.
    assert (
        "${AIVIDEOTRANS_ROOT}/data/model_cache" in text
        or "$AIVIDEOTRANS_ROOT/data/model_cache" in text
    ), (
        "model_cache source path should use ${AIVIDEOTRANS_ROOT}/data/"
        "model_cache so it lives next to other persistent data dirs."
    )

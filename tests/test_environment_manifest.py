"""Environment manifest tests.

Verify that:
1. A root-level Python dependency manifest (pyproject.toml) exists.
2. The Dockerfile installs from the committed manifest, not inline pip install.
3. A dev/test requirements file exists for pip-based workflows.
"""

from __future__ import annotations

from pathlib import Path

# Resolve repo root relative to this test file.
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_root_python_manifest_exists():
    """pyproject.toml must exist at the repo root."""
    assert (REPO_ROOT / "pyproject.toml").exists(), (
        "Missing pyproject.toml at repo root. "
        "Root-level Python dependencies must be declared in a committed manifest."
    )


def test_pyproject_declares_python_version():
    """pyproject.toml must declare a requires-python constraint."""
    toml_path = REPO_ROOT / "pyproject.toml"
    if not toml_path.exists():
        raise AssertionError("pyproject.toml does not exist — cannot check python version")
    text = toml_path.read_text(encoding="utf-8")
    assert "requires-python" in text, (
        "pyproject.toml must contain a requires-python field."
    )


def test_pyproject_declares_dependencies():
    """pyproject.toml must have a [project] dependencies list."""
    toml_path = REPO_ROOT / "pyproject.toml"
    if not toml_path.exists():
        raise AssertionError("pyproject.toml does not exist")
    text = toml_path.read_text(encoding="utf-8")
    assert "dependencies" in text, (
        "pyproject.toml must declare runtime dependencies."
    )


def test_dockerfile_installs_from_committed_manifest():
    """Dockerfile must NOT use inline pip install for app dependencies."""
    dockerfile_path = REPO_ROOT / "Dockerfile"
    if not dockerfile_path.exists():
        raise AssertionError("Dockerfile does not exist")
    text = dockerfile_path.read_text(encoding="utf-8")
    # The old pattern: "pip install --no-cache-dir \" followed by package names
    assert "pip install --no-cache-dir \\" not in text, (
        "Dockerfile still uses inline 'pip install --no-cache-dir \\' for app dependencies. "
        "It should install from pyproject.toml or requirements.txt instead."
    )


def test_requirements_dev_installs_root_project():
    """requirements-dev.txt must install the root project with dev extras.

    A valid dev/test entry point must do more than list pytest — it must
    also pull in the root project's runtime dependencies.  The canonical
    way is ``-e .[dev]`` (or ``.[dev]``) which tells pip to install the
    local project in editable mode with the [dev] optional-dependencies.
    """
    req_path = REPO_ROOT / "requirements-dev.txt"
    assert req_path.exists(), (
        "Missing requirements-dev.txt. "
        "Provide a pip-compatible entry point for dev/test dependencies."
    )
    text = req_path.read_text(encoding="utf-8")
    # Strip comments and blank lines to get effective lines.
    effective_lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Must contain a line that installs the root project (e.g. "-e .[dev]",
    # ".[dev]", "-e .", or ".").
    installs_root = any(
        line.startswith("-e .") or line.startswith("-e.[") or line == "." or line.startswith(".[")
        for line in effective_lines
    )
    assert installs_root, (
        "requirements-dev.txt does not install the root project. "
        "It should contain a line like '-e .[dev]' so that "
        "'pip install -r requirements-dev.txt' installs both "
        "runtime dependencies (from pyproject.toml) and dev extras."
    )

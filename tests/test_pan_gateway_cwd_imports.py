"""Regression guard for Gateway production import paths.

CodeX 2026-05-18 P0: Gateway's Dockerfile is `WORKDIR /opt/gateway` +
`python main.py`. With cwd=/opt/gateway, only BARE imports
(`config`, `pan.backup_executor`, etc.) resolve. `gateway.config` and
`gateway.pan.X` fail with ModuleNotFoundError in production — but
pytest's auto-rootdir-on-sys.path masks this in tests.

Two complementary checks:
  1. AST scan: every pan/* + dispatcher source file's import
     statements use bare names, never `gateway.X`.
  2. Subprocess smoke: cwd=gateway/, `python -c` loads each pan
     module + the dispatcher. Catches anything AST might miss
     (e.g. dynamic imports via __import__).

Future PRs that reintroduce `from gateway.X import Y` will fail both.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


_GATEWAY_DIR = Path(__file__).resolve().parent.parent / 'gateway'


# --- AST scan ---


def _gateway_import_targets() -> list[Path]:
    """Files that must use bare imports — every pan/* + dispatcher."""
    pan_dir = _GATEWAY_DIR / 'pan'
    return [
        _GATEWAY_DIR / 'background_task_executors.py',
        *sorted(pan_dir.glob('*.py')),
    ]


def test_pan_modules_use_bare_imports_only():
    """No `from gateway.X import ...` or `import gateway.X` in any pan/*
    or dispatcher source. Gateway's cwd is /opt/gateway in production;
    the `gateway` package is NOT visible from there."""
    offenders: list[str] = []
    for f in _gateway_import_targets():
        if not f.is_file():
            continue
        tree = ast.parse(f.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith('gateway.'):
                    offenders.append(
                        f'  {f.relative_to(_GATEWAY_DIR.parent)}:{node.lineno} '
                        f'→ from {node.module}'
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('gateway.'):
                        offenders.append(
                            f'  {f.relative_to(_GATEWAY_DIR.parent)}:'
                            f'{node.lineno} → import {alias.name}'
                        )
    assert not offenders, (
        "Gateway production code must use BARE imports (config, "
        "pan.X, project_cleanup, storage.X). Production cwd is "
        "/opt/gateway — `gateway.X` is unresolvable there. Offenders:\n"
        + "\n".join(offenders)
    )


# --- subprocess smoke ---


def test_gateway_cwd_import_smoke():
    """Spawn a subprocess with cwd=gateway/, `python -c` imports every
    pan-related module the dispatcher might reach. If any module's
    top-level imports use `gateway.X`, this fails with
    ModuleNotFoundError — exactly the production failure mode.

    A stub `database` module is injected so we don't need real PG to
    load the dispatcher (which does `from database import async_session`
    at module load time).
    """
    script = (
        "import sys, types\n"
        "from unittest.mock import MagicMock\n"
        "_fake_db = types.ModuleType('database')\n"
        "_fake_db.engine = MagicMock()\n"
        "_fake_db.async_session = MagicMock()\n"
        "_fake_db.get_db = MagicMock()\n"
        "sys.modules['database'] = _fake_db\n"
        "\n"
        "import background_task_executors  # noqa: F401\n"
        "import pan  # noqa: F401\n"
        "import pan.backup_executor  # noqa: F401\n"
        "import pan.restore_executor  # noqa: F401\n"
        "import pan.residue_cleanup  # noqa: F401\n"
        "import pan._safe_paths  # noqa: F401\n"
        "import pan._lock_keys  # noqa: F401\n"
        "import pan.token_crypto  # noqa: F401\n"
        "import pan.manifest  # noqa: F401\n"
        "import pan.baidu_pan_client  # noqa: F401\n"
        "import pan.provider_protocol  # noqa: F401\n"
        "import pan.status_mutator  # noqa: F401\n"
        "import pan.auth  # noqa: F401\n"
        "import pan.admin_api  # noqa: F401\n"
        "import pan.archive_scanner  # noqa: F401\n"
        "import pan.stale_reaper  # noqa: F401\n"
        "import pan.orphan_cleanup  # noqa: F401\n"
        "import pan.scheduler  # noqa: F401\n"
        "\n"
        "from background_task_executors import TASK_EXECUTORS\n"
        "assert 'pan_backup' in TASK_EXECUTORS\n"
        "assert 'pan_restore' in TASK_EXECUTORS\n"
        "assert 'pan_residue_cleanup' in TASK_EXECUTORS\n"
        "print('IMPORTS_OK')\n"
    )

    # Inherit parent env (Windows asyncio needs SystemRoot etc.) but
    # strip PYTHONPATH so production-cwd resolution can't be masked by
    # a test sys.path entry leaking in.
    import os
    child_env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=str(_GATEWAY_DIR),
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )

    assert proc.returncode == 0, (
        f"Gateway cwd=/opt/gateway import smoke FAILED — "
        f"production runtime would also fail:\n"
        f"STDOUT: {proc.stdout}\n"
        f"STDERR: {proc.stderr}"
    )
    assert 'IMPORTS_OK' in proc.stdout

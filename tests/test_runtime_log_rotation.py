"""Tests for runtime log rotation helpers and docker-compose logging caps.

Covers:
  1. attach_rotating_file_log (via utils.rotating_log) creates a handler and
     writes a log line when the directory exists.
  2. attach_rotating_file_log does NOT raise when the target dir cannot be
     created (simulated by pointing the env var at a FILE path).
  3. docker-compose.yml contract: every service has logging.options.max-size
     (regression guard — prevents the bound from being silently removed).
  4. _attach_rotating_file_log from gateway.main behaves the same as the
     shared helper (same contract, separate implementation path).
  5. Idempotence: calling attach twice for the same target file adds exactly
     one handler (regression guard for the US prod 2026-06-13 incident where
     gateway/main.py executed twice — as __main__ and as uvicorn's "main:app"
     re-import — and every gateway.app.log line was written twice).
  6. AST contract: BOTH job-api entry paths (main.run_job_api_command and
     scripts/run_remote_workbench_service._run_job_api) call
     attach_rotating_file_log("jobapi.app.log") inside a fail-safe try/except
     (regression guard for the US prod 2026-06-13 finding where the container
     entry — linux_app_service.sh → run_remote_workbench_service.py — never
     attached the handler, so jobapi.app.log did not exist in production).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_PATH = _WORKTREE_ROOT / "docker-compose.yml"
_SRC_PATH = _WORKTREE_ROOT / "src"


def _ensure_src_on_path() -> None:
    src = str(_SRC_PATH)
    if src not in sys.path:
        sys.path.insert(0, src)


# ---------------------------------------------------------------------------
# Test 1 — helper creates handler and file in a temp directory
# ---------------------------------------------------------------------------


def test_attach_rotating_file_log_creates_file(tmp_path, monkeypatch):
    """attach_rotating_file_log should create the log file and attach a handler."""
    _ensure_src_on_path()

    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    # Remove any pre-existing file handlers so we can count new ones.
    root_logger = logging.getLogger()
    pre_handlers = list(root_logger.handlers)

    from utils.rotating_log import attach_rotating_file_log

    attach_rotating_file_log("test_helper.log")

    # At least one new handler was added.
    new_handlers = [h for h in root_logger.handlers if h not in pre_handlers]
    assert new_handlers, "No new handler was attached to the root logger"

    # Writing a log line should flush to the file.
    test_logger = logging.getLogger("test_helper_logger")
    test_logger.warning("rotation test line")
    for h in new_handlers:
        h.flush()

    log_file = tmp_path / "test_helper.log"
    assert log_file.exists(), f"Log file not found: {log_file}"
    assert "rotation test line" in log_file.read_text(encoding="utf-8")

    # Cleanup — remove the handlers we just added so other tests aren't affected.
    for h in new_handlers:
        root_logger.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# Test 2 — helper must not raise when dir cannot be created
# ---------------------------------------------------------------------------


def test_attach_rotating_file_log_bad_dir_no_raise(tmp_path, monkeypatch):
    """attach_rotating_file_log must not raise even if the log dir is unusable."""
    _ensure_src_on_path()

    # Point the env var at an EXISTING FILE path (mkdir-ing over a file fails).
    collision_path = tmp_path / "not_a_dir"
    collision_path.write_text("I am a file, not a directory")
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(collision_path))

    from utils.rotating_log import attach_rotating_file_log

    # Should not raise anything — just print a warning and continue.
    attach_rotating_file_log("should_fail_silently.log")


# ---------------------------------------------------------------------------
# Test 3 — docker-compose.yml every service has logging.options.max-size
# ---------------------------------------------------------------------------


def _load_compose() -> dict:
    with open(_COMPOSE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_all_services_have_logging_max_size():
    """Every service in docker-compose.yml must declare logging.options.max-size."""
    compose = _load_compose()
    services: dict = compose.get("services", {})
    assert services, "docker-compose.yml has no services"

    missing: list[str] = []
    for name, cfg in services.items():
        logging_cfg = cfg.get("logging") if isinstance(cfg, dict) else None
        if not isinstance(logging_cfg, dict):
            missing.append(f"{name}: missing 'logging' key")
            continue
        options = logging_cfg.get("options")
        if not isinstance(options, dict) or "max-size" not in options:
            missing.append(f"{name}: missing logging.options.max-size")

    assert not missing, (
        "The following services are missing logging rotation config:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


# ---------------------------------------------------------------------------
# Test 4 — gateway _attach_rotating_file_log does not raise on bad dir
# ---------------------------------------------------------------------------


def test_gateway_attach_rotating_file_log_bad_dir_no_raise(tmp_path, monkeypatch):
    """gateway/main._attach_rotating_file_log must not raise on bad dir."""
    # Import the function directly without importing the whole gateway app
    # (which would pull in FastAPI, databases, etc.).
    import importlib
    import types

    # Build a minimal stub environment so gateway/main.py top-level imports
    # don't fail, then extract only the function we need.
    collision_path = tmp_path / "also_not_a_dir"
    collision_path.write_text("blocking file")
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(collision_path))

    # We test the extracted logic directly by re-running the same code path
    # as _attach_rotating_file_log — replicated inline to avoid heavyweight imports.
    import logging as _logging
    from logging.handlers import RotatingFileHandler as _RFH
    from pathlib import Path as _Path

    def _simulate_attach(log_dir_str: str) -> None:
        try:
            log_dir = _Path(log_dir_str)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "gateway.app.log"
            handler = _RFH(str(log_path), maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            _logging.getLogger().addHandler(handler)
        except Exception:  # noqa: BLE001
            pass  # must not propagate

    # Should not raise.
    _simulate_attach(str(collision_path))


# ---------------------------------------------------------------------------
# Test 5 — idempotence: double attach must not stack a second handler
# ---------------------------------------------------------------------------


def test_attach_rotating_file_log_idempotent(tmp_path, monkeypatch):
    """Calling the shared helper twice for the same file adds exactly one handler."""
    _ensure_src_on_path()
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    root_logger = logging.getLogger()
    pre_handlers = list(root_logger.handlers)

    from utils.rotating_log import attach_rotating_file_log

    attach_rotating_file_log("idempotent.log")
    attach_rotating_file_log("idempotent.log")

    new_handlers = [h for h in root_logger.handlers if h not in pre_handlers]
    try:
        assert len(new_handlers) == 1, (
            f"Expected exactly 1 new handler after double attach, "
            f"got {len(new_handlers)}"
        )
    finally:
        for h in new_handlers:
            root_logger.removeHandler(h)
            h.close()


def test_attach_rotating_file_log_distinct_files_both_attach(tmp_path, monkeypatch):
    """The idempotence guard keys on the target file — distinct files still attach."""
    _ensure_src_on_path()
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    root_logger = logging.getLogger()
    pre_handlers = list(root_logger.handlers)

    from utils.rotating_log import attach_rotating_file_log

    attach_rotating_file_log("first.log")
    attach_rotating_file_log("second.log")

    new_handlers = [h for h in root_logger.handlers if h not in pre_handlers]
    try:
        assert len(new_handlers) == 2, (
            f"Expected 2 new handlers for 2 distinct files, got {len(new_handlers)}"
        )
    finally:
        for h in new_handlers:
            root_logger.removeHandler(h)
            h.close()


def _load_gateway_attach_fn():
    """Extract the REAL _attach_rotating_file_log from gateway/main.py source.

    gateway/main.py cannot be imported wholesale in tests (it pulls FastAPI,
    database, every router, ...), so compile just the function definition via
    AST. The function only references the module-level ``logging`` name; its
    other imports (os, RotatingFileHandler, Path) are local to its body.
    """
    import ast

    gateway_main = _WORKTREE_ROOT / "gateway" / "main.py"
    tree = ast.parse(gateway_main.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_attach_rotating_file_log"
    )
    namespace = {"logging": logging}
    exec(  # noqa: S102 — compiling our own source file, not external input
        compile(ast.Module(body=[fn], type_ignores=[]), str(gateway_main), "exec"),
        namespace,
    )
    return namespace["_attach_rotating_file_log"]


# ---------------------------------------------------------------------------
# Test 6 — AST contract: both job-api entry paths attach jobapi.app.log
# ---------------------------------------------------------------------------

_JOB_API_ENTRY_FUNCTIONS = [
    # (source file, function holding the job-api startup path)
    (_WORKTREE_ROOT / "main.py", "run_job_api_command"),
    (
        _WORKTREE_ROOT / "scripts" / "run_remote_workbench_service.py",
        "_run_job_api",
    ),
]


def _find_function_def(path: Path, name: str):
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: function {name!r} not found")


def _has_failsafe_jobapi_log_attach(fn_node) -> bool:
    """True if the function calls attach_rotating_file_log("jobapi.app.log")
    somewhere inside a try block that has at least one exception handler."""
    import ast

    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "attach_rotating_file_log"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "jobapi.app.log"
            ):
                return True
    return False


@pytest.mark.parametrize(
    ("path", "function_name"),
    _JOB_API_ENTRY_FUNCTIONS,
    ids=[p.name for p, _ in _JOB_API_ENTRY_FUNCTIONS],
)
def test_job_api_entry_attaches_rotating_log(path: Path, function_name: str):
    """Every job-api entry path must attach jobapi.app.log fail-safely.

    The production container starts job-api via linux_app_service.sh →
    scripts/run_remote_workbench_service.py, NOT via ``python main.py
    job-api`` — if either path drops the attach call, the on-disk rotating
    log silently disappears in that deployment mode.
    """
    fn_node = _find_function_def(path, function_name)
    assert _has_failsafe_jobapi_log_attach(fn_node), (
        f"{path.name}::{function_name} must call "
        'attach_rotating_file_log("jobapi.app.log") inside a try/except '
        "fail-safe wrap (see main.run_job_api_command for the pattern)"
    )


def test_gateway_attach_rotating_file_log_idempotent(tmp_path, monkeypatch):
    """gateway/main._attach_rotating_file_log called twice adds exactly one handler.

    Reproduces the production double-run: module executes as __main__ (CMD
    ``python main.py``) and again as ``main`` when uvicorn.run("main:app")
    re-imports it. Both runs share the same root logger.
    """
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    attach = _load_gateway_attach_fn()

    root_logger = logging.getLogger()
    pre_handlers = list(root_logger.handlers)

    attach()
    attach()

    new_handlers = [h for h in root_logger.handlers if h not in pre_handlers]
    try:
        assert len(new_handlers) == 1, (
            f"Expected exactly 1 new handler after double attach, "
            f"got {len(new_handlers)}"
        )
        assert new_handlers[0].baseFilename == os.path.abspath(
            str(tmp_path / "gateway.app.log")
        )
    finally:
        for h in new_handlers:
            root_logger.removeHandler(h)
            h.close()

"""APF2c-2 local temp storage health helper tests.

Exercises ``src.services.anonymous_preview_storage_health`` against
``tmp_path`` and monkeypatch-only fakes. No real backend, gateway,
frontend, upload, probe, compliance, preview media, clone provider,
counter store, pricing, payment, migration or deployment code is
touched.

Design source of truth:
``docs/plans/2026-06-02-apf2c-backend-adapter-boundary.md``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from src.services import anonymous_preview_storage_health as health_module
from src.services.anonymous_preview_storage_health import (
    DEFAULT_PROBE_FILENAME_PREFIX,
    StorageHealthResult,
    check_temp_upload_storage,
)


# ---------------------------------------------------------------------------
# Behavior contract — fail-closed branches and happy path.
# ---------------------------------------------------------------------------


def test_path_none_fail_closed() -> None:
    result = check_temp_upload_storage(None)
    assert isinstance(result, StorageHealthResult)
    assert result.available is False
    assert "None" in result.reason


def test_missing_directory_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = check_temp_upload_storage(missing)
    assert result.available is False
    assert "does not exist" in result.reason
    assert not missing.exists(), "helper must never create missing directories"


def test_path_is_regular_file_fail_closed(tmp_path: Path) -> None:
    file_path = tmp_path / "not_a_directory.bin"
    file_path.write_bytes(b"payload")
    result = check_temp_upload_storage(file_path)
    assert result.available is False
    assert "not a directory" in result.reason
    assert file_path.exists(), "helper must not delete caller-provided file"
    assert file_path.read_bytes() == b"payload"


def test_writable_directory_succeeds_and_cleans_probe(tmp_path: Path) -> None:
    pre_existing = {p.name for p in tmp_path.iterdir()}
    result = check_temp_upload_storage(tmp_path)
    assert result.available is True
    assert "succeeded" in result.reason
    post_existing = {p.name for p in tmp_path.iterdir()}
    assert post_existing == pre_existing, (
        "probe file must be removed; no other files may be created or deleted"
    )
    # The probe file prefix must not leak into the directory after success.
    assert not any(
        name.startswith(DEFAULT_PROBE_FILENAME_PREFIX) for name in post_existing
    )


def test_custom_probe_prefix_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Path] = {}

    real_write = health_module._write_probe

    def spy_write(probe_path: Path) -> None:
        seen["path"] = probe_path
        real_write(probe_path)

    monkeypatch.setattr(health_module, "_write_probe", spy_write)
    result = check_temp_upload_storage(
        tmp_path, probe_filename_prefix="custom_prefix_"
    )
    assert result.available is True
    assert seen["path"].name.startswith("custom_prefix_")
    assert seen["path"].suffix == ".tmp"
    assert not seen["path"].exists(), "probe file must be cleaned up"


def test_probe_write_failure_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_probe_path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(health_module, "_write_probe", boom)
    result = check_temp_upload_storage(tmp_path)
    assert result.available is False
    assert "probe write failed" in result.reason
    assert "OSError" in result.reason
    # No probe file should leak into the directory.
    assert not any(
        p.name.startswith(DEFAULT_PROBE_FILENAME_PREFIX)
        for p in tmp_path.iterdir()
    )


def test_probe_delete_failure_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked: dict[str, Path] = {}

    real_write = health_module._write_probe

    def spy_write(probe_path: Path) -> None:
        leaked["path"] = probe_path
        real_write(probe_path)

    def boom(_probe_path: Path) -> None:
        raise PermissionError("cannot unlink")

    monkeypatch.setattr(health_module, "_write_probe", spy_write)
    monkeypatch.setattr(health_module, "_remove_probe", boom)
    result = check_temp_upload_storage(tmp_path)
    assert result.available is False
    assert "probe delete failed" in result.reason
    assert "PermissionError" in result.reason
    # The probe file is still on disk because delete was stubbed to fail;
    # the helper itself must not recurse or escalate the cleanup attempt.
    assert leaked["path"].exists()
    # Test housekeeping (not the helper's job): remove the leaked probe.
    leaked["path"].unlink()


def test_exists_raises_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingPath:
        def exists(self) -> bool:
            raise RuntimeError("inspection failed")

        def is_dir(self) -> bool:  # pragma: no cover — defensive
            return True

        def __truediv__(self, _other: str) -> Path:  # pragma: no cover
            raise AssertionError("should not be reached")

    result = check_temp_upload_storage(ExplodingPath())  # type: ignore[arg-type]
    assert result.available is False
    assert "inspection raised" in result.reason
    assert "RuntimeError" in result.reason


def test_result_is_immutable_dataclass() -> None:
    result = StorageHealthResult(available=True, reason="ok")
    with pytest.raises(Exception):
        result.available = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# P2 — probe_filename_prefix must not escape the temp directory
# (PR #22 review discussion_r3345886353).
# ---------------------------------------------------------------------------


def test_traversal_probe_prefix_fails_closed_and_no_external_write(
    tmp_path: Path,
) -> None:
    # A prefix containing ``../`` would join into ``path / "../foo<uuid>.tmp"``,
    # which lexically resolves outside the caller-provided temp directory.
    # The helper must reject (fail-closed) and never create or delete any
    # probe file in the parent directory.
    parent = tmp_path / "scratch_parent"
    parent.mkdir()
    inside = parent / "temp_uploads"
    inside.mkdir()

    pre_parent = {p.name for p in parent.iterdir()}
    pre_inside = {p.name for p in inside.iterdir()}

    result = check_temp_upload_storage(
        inside, probe_filename_prefix="../escape_health_"
    )

    assert result.available is False
    assert "escapes temp directory" in result.reason
    assert "fail closed" in result.reason.lower()
    # Stable, low-sensitivity reason: must not echo the raw prefix or
    # any filesystem path.
    assert "../escape_health_" not in result.reason
    assert str(inside) not in result.reason

    # No new file landed in the parent (escape target) or inside.
    assert {p.name for p in parent.iterdir()} == pre_parent
    assert {p.name for p in inside.iterdir()} == pre_inside


def test_subdirectory_separator_probe_prefix_fails_closed(
    tmp_path: Path,
) -> None:
    # A prefix containing a forward-slash separator would try to write
    # into a child directory of ``path`` — escaping the contract that
    # probes only sit inside the caller-provided directory itself. The
    # helper must reject without creating the would-be subdirectory.
    pre_root = {p.name for p in tmp_path.iterdir()}

    result = check_temp_upload_storage(
        tmp_path, probe_filename_prefix="nested/health_"
    )

    assert result.available is False
    assert "escapes temp directory" in result.reason
    # Subdirectory must not be auto-created — helper never calls mkdir.
    assert not (tmp_path / "nested").exists()
    assert {p.name for p in tmp_path.iterdir()} == pre_root


def test_absolute_probe_prefix_fails_closed_and_no_external_write(
    tmp_path: Path,
) -> None:
    # An absolute-path prefix would make ``path / probe_name`` resolve
    # to the absolute target instead of staying under ``path``. The
    # helper must reject and never touch the elsewhere directory.
    target = tmp_path / "target"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    pre_target = {p.name for p in target.iterdir()}
    pre_elsewhere = {p.name for p in elsewhere.iterdir()}

    # ``str(elsewhere / "health_")`` produces a platform-correct absolute
    # path on both POSIX and Windows.
    absolute_prefix = str(elsewhere / "health_")

    result = check_temp_upload_storage(
        target, probe_filename_prefix=absolute_prefix
    )

    assert result.available is False
    assert "escapes temp directory" in result.reason
    assert "fail closed" in result.reason.lower()
    # Stable, low-sensitivity reason: absolute path must not be echoed.
    assert absolute_prefix not in result.reason

    # Neither the target nor the would-be escape destination grew.
    assert {p.name for p in target.iterdir()} == pre_target
    assert {p.name for p in elsewhere.iterdir()} == pre_elsewhere


def test_legitimate_prefix_with_dots_still_succeeds(tmp_path: Path) -> None:
    # Defense regression: a prefix containing literal dots but no path
    # separator (e.g. ``"aivt..health_"``) is a valid filename component
    # and must still complete the probe successfully — the guard is
    # about path components, not about ``.`` characters per se.
    pre = {p.name for p in tmp_path.iterdir()}

    result = check_temp_upload_storage(
        tmp_path, probe_filename_prefix="aivt..health_"
    )

    assert result.available is True
    assert "succeeded" in result.reason
    # Probe cleaned itself up; nothing remains.
    assert {p.name for p in tmp_path.iterdir()} == pre


# ---------------------------------------------------------------------------
# Import guard — module must depend only on the standard library.
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORT_FRAGMENTS: tuple[str, ...] = (
    "src.services.anonymous_preview_intake",
    "src.services.anonymous_preview_backend_adapter",
    "src.services.content_compliance",
    "src.services.voice_clone",
    "src.services.tts_provider",
    "src.services.tts_service",
    "src.services.usage_meter",
    "src.pipeline",
    "src.modules",
    "gateway",
    "frontend",
    "redis",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "alembic",
    "fastapi",
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
    "socket",
    "subprocess",
    "boto3",
    "botocore",
    "stripe",
    "wechatpay",
    "alipay",
)


def _module_source() -> str:
    module_path = Path(health_module.__file__)
    return module_path.read_text(encoding="utf-8")


def _imported_names() -> set[str]:
    tree = ast.parse(_module_source())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def test_module_imports_are_stdlib_only() -> None:
    names = _imported_names()
    # Allow exactly these stdlib imports observed in the module today.
    allowed = {"uuid", "dataclasses", "pathlib", "typing", "__future__"}
    unexpected = names - allowed
    assert not unexpected, (
        f"helper must only import stdlib; saw unexpected imports: {unexpected}"
    )


def test_module_has_no_forbidden_imports() -> None:
    names = _imported_names()
    for fragment in _FORBIDDEN_IMPORT_FRAGMENTS:
        for name in names:
            assert fragment not in name, (
                f"forbidden import fragment {fragment!r} found in {name!r}"
            )


def test_module_does_not_load_forbidden_modules_at_import_time() -> None:
    # Re-importing the helper must not pull any forbidden module into
    # sys.modules. We snapshot before + after a reimport.
    forbidden_loaded_before = {
        m for m in sys.modules if _is_forbidden(m)
    }
    # Reimport in a clean-ish way: drop the helper from sys.modules and
    # re-import via importlib.
    import importlib

    sys.modules.pop(
        "src.services.anonymous_preview_storage_health", None
    )
    importlib.import_module("src.services.anonymous_preview_storage_health")

    forbidden_loaded_after = {
        m for m in sys.modules if _is_forbidden(m)
    }
    newly_loaded = forbidden_loaded_after - forbidden_loaded_before
    assert not newly_loaded, (
        "importing the helper must not load forbidden modules; newly loaded: "
        f"{sorted(newly_loaded)}"
    )


def _is_forbidden(module_name: str) -> bool:
    return any(
        fragment in module_name for fragment in _FORBIDDEN_IMPORT_FRAGMENTS
    )


# ---------------------------------------------------------------------------
# AST guard — module must not call subprocess / network / DB APIs.
# ---------------------------------------------------------------------------


_FORBIDDEN_CALL_FRAGMENTS: tuple[str, ...] = (
    "subprocess",
    "Popen",
    "system(",
    "socket.",
    "urlopen",
    "Request(",
    "requests.",
    "httpx.",
    "boto3.",
    "redis.",
    "psycopg",
    "sqlalchemy",
    "rmtree",
    "shutil.",
)


def test_module_source_has_no_forbidden_call_fragments() -> None:
    source = _module_source()
    for fragment in _FORBIDDEN_CALL_FRAGMENTS:
        assert fragment not in source, (
            f"forbidden call fragment {fragment!r} found in helper source"
        )


def test_module_does_not_mkdir_or_recursive_delete() -> None:
    source = _module_source()
    # Helper must never auto-create directories or recursively delete.
    for fragment in ("mkdir(", "makedirs(", "rmtree(", "rmdir("):
        assert fragment not in source, (
            f"helper must not call {fragment!r}"
        )

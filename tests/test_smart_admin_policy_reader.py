"""Smart admin policy loader cleanup (2026-05-18).

== Background ==

src/pipeline/process.py used to do::

    from admin_settings import load_settings as _load_admin_settings

inside the smart inline branch to read three admin policy toggles:
``smart_auto_clone_enabled`` / ``smart_reuse_user_voice_enabled`` /
``smart_pause_on_possible_user_voice_match``.

The problem: ``admin_settings`` resolves only inside the GATEWAY
container (gateway/admin_settings.py is the FastAPI router module,
on PYTHONPATH only via the gateway image's WORKDIR). The APP
container's ``services.admin_settings`` is a different module —
``from admin_settings import ...`` raises ``ModuleNotFoundError``
on every fresh-clone Smart job in app container, falls through
the try/except, and uses the same defaults this test pins.

Real incident: job_14989c5e9ec44bdebc5f3f5d6111db54 (2026-05-18
Jensen Huang interview) logged this warning at S2 voice review
time. Functionally harmless — defaults match admin UI defaults —
but misleading audit noise.

== Fix ==

Switch to ``services.admin_settings.read_admin_setting`` — the
canonical app-side fail-safe reader. Reads the SAME on-disk JSON
file the gateway's ``save_settings`` writes
(``{AIVIDEOTRANS_CONFIG_DIR}/admin_settings.json``, bind-mounted),
no pydantic dep, no module-not-found risk.

== This test ==

1. Source-level: no ``from admin_settings import`` in process.py
   (catches regression to the gateway-only import pattern).
2. Behavior: ``read_admin_setting`` returns the right typed values
   when JSON has them, returns defaults when JSON is missing or
   the field is absent — and NEVER raises.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PROCESS_PY = _SRC / "pipeline" / "process.py"


class TestNoGatewayOnlyAdminSettingsImport:
    """Pin that process.py never imports the gateway-only admin_settings."""

    def test_no_bare_admin_settings_import_in_process_py(self):
        source = _PROCESS_PY.read_text(encoding="utf-8")
        # Catch BOTH ``from admin_settings import X`` and
        # ``import admin_settings`` — but allow the qualified
        # ``services.admin_settings`` form which IS app-safe.
        bare_import = re.search(
            r"^\s*(?:from\s+admin_settings\s+import|import\s+admin_settings\s*$)",
            source,
            flags=re.MULTILINE,
        )
        assert bare_import is None, (
            "process.py runs inside the APP container, which does NOT have "
            "gateway/admin_settings.py on PYTHONPATH. Use "
            "``services.admin_settings.read_admin_setting`` (or another "
            "qualified import under ``services.``) instead. Offending "
            f"match:\n{bare_import.group(0) if bare_import else ''}"
        )

    def test_uses_read_admin_setting_for_smart_policy(self):
        """The smart inline branch must consume the three admin toggles
        via read_admin_setting; without this anchor a future cleanup
        might delete the read entirely and silently regress to the
        always-default code path."""
        source = _PROCESS_PY.read_text(encoding="utf-8")
        # Find the smart inline branch — anchored on the unique
        # _smart_admin_clone_enabled / _smart_admin_reuse_enabled vars.
        for field in (
            "smart_auto_clone_enabled",
            "smart_reuse_user_voice_enabled",
            "smart_pause_on_possible_user_voice_match",
        ):
            assert f'read_admin_setting(\n                            "{field}"' in source \
                or f'read_admin_setting("{field}"' in source, (
                f"Smart inline branch must call ``read_admin_setting('{field}', "
                f"default=...)``. If you renamed the helper or moved the read, "
                f"update this test."
            )


class TestReadAdminSettingBehavior:
    """End-to-end behavior of the helper now used by process.py."""

    def test_returns_default_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        from services.admin_settings import read_admin_setting

        assert read_admin_setting("smart_auto_clone_enabled", default=True) is True
        assert read_admin_setting("smart_reuse_user_voice_enabled", default=True) is True
        assert read_admin_setting(
            "smart_pause_on_possible_user_voice_match", default=False
        ) is False

    def test_returns_default_when_field_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"unrelated_field": "x"}), encoding="utf-8"
        )

        from services.admin_settings import read_admin_setting

        # Defaults match what process.py uses
        assert read_admin_setting("smart_auto_clone_enabled", default=True) is True
        assert read_admin_setting(
            "smart_pause_on_possible_user_voice_match", default=False
        ) is False

    def test_returns_persisted_value_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({
                "smart_auto_clone_enabled": False,
                "smart_reuse_user_voice_enabled": False,
                "smart_pause_on_possible_user_voice_match": True,
            }),
            encoding="utf-8",
        )

        from services.admin_settings import read_admin_setting

        # Admin set all three to non-default — verify pickup
        assert read_admin_setting("smart_auto_clone_enabled", default=True) is False
        assert read_admin_setting("smart_reuse_user_voice_enabled", default=True) is False
        assert read_admin_setting(
            "smart_pause_on_possible_user_voice_match", default=False
        ) is True

    def test_never_raises_on_corrupt_json(self, tmp_path, monkeypatch):
        """Admin file can be partially-written or hand-edited; helper must
        not crash the pipeline."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            "{not valid json", encoding="utf-8"
        )

        from services.admin_settings import read_admin_setting

        # Corrupt JSON → default
        assert read_admin_setting("smart_auto_clone_enabled", default=True) is True

    def test_never_raises_on_non_dict_root(self, tmp_path, monkeypatch):
        """JSON file with list/scalar root → default (not exception)."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            "[1, 2, 3]", encoding="utf-8"
        )

        from services.admin_settings import read_admin_setting

        assert read_admin_setting("smart_auto_clone_enabled", default=True) is True

"""PR-A lockstep + construction-site guards for the multilingual language fields.

Plan: docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md §5 Phase 1
(steps 1, 2, 4). Pure file-reads + lightweight imports — no DB, no network.

Verifies:

* **Lockstep** — services.language_registry defaults == gateway ``Job`` model
  server_defaults == alembic 036 server_defaults == Job API ``JobRecord``
  defaults (en / zh-CN / en->zh-CN).
* **Migration 036 contract** — revision chain (down_revision = 035), three
  additive NOT NULL columns, downgrade drops all three.
* **JobRecord round-trip** — to_dict / from_dict carry the fields; missing keys
  fall back to the GA baseline.
* **Construction-site AST guards** — every ``Job(...)`` constructor in
  job_intercept.py (create-path + copy_as_new) and the ``metering_snapshot``
  dict literal explicitly carry the language fields, so a future field-by-field
  edit that forgets them fails CI (feedback_copy_as_new_invariants). The
  create-path uses the default-pair constant; copy_as_new copies from the
  source row.
* **Anonymous lane** — the create-payload whitelist stays language-field-free,
  and a payload with no language fields locks to the GA default pair.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

# Imports resolve via tests/conftest.py (adds src/ and gateway/ to sys.path).
from services.language_registry import (
    DEFAULT_LANGUAGE_PAIR,
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    SUPPORTED_LANGUAGE_PAIRS,
    resolve_language_pair,
)
from services.jobs.models import JobRecord
from models import Job
from anonymous_preview_payload_spec import ANONYMOUS_PREVIEW_PAYLOAD_SPEC


REPO_ROOT = Path(__file__).resolve().parents[1]

LANG_FIELDS = ("source_language", "target_language", "language_pair")
EXPECTED_DEFAULTS = {
    "source_language": "en",
    "target_language": "zh-CN",
    "language_pair": "en->zh-CN",
}

JOB_INTERCEPT_REL = "gateway/job_intercept.py"
MIGRATION_REL = "gateway/alembic/versions/036_job_language_fields.py"


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _parse(rel_path: str) -> ast.AST:
    return ast.parse(_read(rel_path), filename=str(REPO_ROOT / rel_path))


# =====================================================================
# §1. Lockstep — registry defaults are the single source of truth.
# =====================================================================


def test_registry_defaults_are_ga_baseline() -> None:
    assert DEFAULT_SOURCE_LANGUAGE == EXPECTED_DEFAULTS["source_language"]
    assert DEFAULT_TARGET_LANGUAGE == EXPECTED_DEFAULTS["target_language"]
    assert DEFAULT_LANGUAGE_PAIR == EXPECTED_DEFAULTS["language_pair"]


# =====================================================================
# §2. Gateway Job model columns.
# =====================================================================


def _server_default_value(col) -> str:
    """Extract the server_default literal as a bare string.

    Handles both plain-string (``server_default="en"``) and ``text("'en'")``
    forms — strips surrounding single quotes either way.
    """
    sd = col.server_default
    assert sd is not None
    arg = sd.arg
    text = getattr(arg, "text", arg)
    return str(text).strip().strip("'")


def test_gateway_job_model_has_language_columns() -> None:
    cols = Job.__table__.columns
    for field_name in LANG_FIELDS:
        assert field_name in cols, f"Job model missing column {field_name}"
        col = cols[field_name]
        assert col.nullable is False, f"{field_name} must be NOT NULL"
        assert _server_default_value(col) == EXPECTED_DEFAULTS[field_name]


# =====================================================================
# §3. JobRecord (Job API) defaults + round-trip.
# =====================================================================


def _make_job_record(**overrides) -> JobRecord:
    base = dict(
        job_id="job_test",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/v",
        output_target="both",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="queued",
        current_stage=None,
        progress_message=None,
        created_at="2026-06-13T00:00:00Z",
        updated_at="2026-06-13T00:00:00Z",
    )
    base.update(overrides)
    return JobRecord(**base)


def test_jobrecord_default_language_fields() -> None:
    rec = _make_job_record()
    assert rec.source_language == "en"
    assert rec.target_language == "zh-CN"
    assert rec.language_pair == "en->zh-CN"


def test_jobrecord_to_from_dict_roundtrip() -> None:
    rec = _make_job_record(
        source_language="zh-CN", target_language="en", language_pair="zh-CN->en"
    )
    payload = rec.to_dict()
    assert payload["source_language"] == "zh-CN"
    assert payload["target_language"] == "en"
    assert payload["language_pair"] == "zh-CN->en"

    rec2 = JobRecord.from_dict(payload)
    assert rec2.source_language == "zh-CN"
    assert rec2.target_language == "en"
    assert rec2.language_pair == "zh-CN->en"


def test_jobrecord_from_dict_missing_keys_default_to_baseline() -> None:
    payload = _make_job_record().to_dict()
    for field_name in LANG_FIELDS:
        payload.pop(field_name, None)
    rec = JobRecord.from_dict(payload)
    assert rec.source_language == "en"
    assert rec.target_language == "zh-CN"
    assert rec.language_pair == "en->zh-CN"


def test_jobrecord_defaults_match_registry() -> None:
    rec = _make_job_record()
    assert rec.source_language == DEFAULT_SOURCE_LANGUAGE
    assert rec.target_language == DEFAULT_TARGET_LANGUAGE
    assert rec.language_pair == DEFAULT_LANGUAGE_PAIR


# =====================================================================
# §4. Migration 036 contract.
# =====================================================================


def _load_migration_module():
    path = REPO_ROOT / MIGRATION_REL
    spec = importlib.util.spec_from_file_location("migration_036_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_036_revision_chain() -> None:
    mod = _load_migration_module()
    assert mod.revision == "036_job_language_fields"
    assert mod.down_revision == "035_anonymous_preview"


def test_migration_036_adds_three_notnull_columns() -> None:
    src = _read(MIGRATION_REL)
    for field_name, default in EXPECTED_DEFAULTS.items():
        assert f'"{field_name}"' in src, f"migration missing column {field_name}"
        assert f"'{default}'" in src, f"migration missing server_default {default!r}"
    # All three columns are NOT NULL.
    assert src.count("nullable=False") >= 3


def test_migration_036_downgrade_drops_all_columns() -> None:
    src = _read(MIGRATION_REL)
    for field_name in LANG_FIELDS:
        assert f'drop_column("jobs", "{field_name}")' in src


def _module_down_revision(path: Path):
    """AST-parse a migration file's module-level ``down_revision`` value.

    Robust against quote style and ``=`` vs annotated ``:`` assignment, unlike a
    raw substring match (codex P2). Returns the literal value or ``None``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "down_revision":
                return value.value if isinstance(value, ast.Constant) else None
    return None


def test_migration_036_is_the_only_child_of_035() -> None:
    """036 must chain off 035 and nothing else may claim 035 as its parent
    (single linear head — no fork). AST-parsed, not substring-matched."""
    versions_dir = REPO_ROOT / "gateway" / "alembic" / "versions"
    children = [
        path.name
        for path in versions_dir.glob("*.py")
        if _module_down_revision(path) == "035_anonymous_preview"
    ]
    assert children == ["036_job_language_fields.py"], children


# =====================================================================
# §5. Construction-site AST guards (job_intercept.py).
# =====================================================================


def _job_constructor_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Job"
    ]


def test_job_intercept_imports_registry_defaults() -> None:
    tree = _parse(JOB_INTERCEPT_REL)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "services.language_registry":
            imported |= {alias.name for alias in node.names}
    assert {
        "DEFAULT_SOURCE_LANGUAGE",
        "DEFAULT_TARGET_LANGUAGE",
        "DEFAULT_LANGUAGE_PAIR",
    } <= imported


def test_two_job_constructor_calls_present() -> None:
    """Pins the two known field-by-field Job(...) construction sites
    (create-path + copy_as_new). If this count changes, the next test still
    guarantees any new constructor carries the language fields."""
    calls = _job_constructor_calls(_parse(JOB_INTERCEPT_REL))
    assert len(calls) == 2, f"expected exactly 2 Job(...) calls, found {len(calls)}"


def test_every_job_constructor_has_language_fields() -> None:
    calls = _job_constructor_calls(_parse(JOB_INTERCEPT_REL))
    assert calls, "no Job(...) constructor calls found"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords if kw.arg is not None}
        for field_name in LANG_FIELDS:
            assert field_name in kwargs, (
                f"Job(...) at line {call.lineno} is missing kwarg {field_name}"
            )


def _kw_value(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _classify_job_calls(tree: ast.AST) -> tuple[list[ast.Call], list[ast.Call]]:
    """Split Job(...) calls into (create_path, copy_as_new).

    Discriminator: only the copy_as_new constructor sets ``copy_of_job_id``.
    Binding each value assertion to the *specific* constructor (codex P2) — so a
    future swap of create-path vs copy semantics cannot pass.
    """
    create_calls: list[ast.Call] = []
    copy_calls: list[ast.Call] = []
    for call in _job_constructor_calls(tree):
        kwargs = {kw.arg for kw in call.keywords if kw.arg is not None}
        (copy_calls if "copy_of_job_id" in kwargs else create_calls).append(call)
    return create_calls, copy_calls


def test_create_path_sets_all_three_to_registry_defaults() -> None:
    """create-path Job() must set ALL THREE language kwargs to the registry
    default constants (not just language_pair)."""
    create_calls, _ = _classify_job_calls(_parse(JOB_INTERCEPT_REL))
    assert len(create_calls) == 1, f"expected 1 create-path Job(), found {len(create_calls)}"
    call = create_calls[0]
    expected_const = {
        "source_language": "DEFAULT_SOURCE_LANGUAGE",
        "target_language": "DEFAULT_TARGET_LANGUAGE",
        "language_pair": "DEFAULT_LANGUAGE_PAIR",
    }
    for field_name, const_name in expected_const.items():
        value = _kw_value(call, field_name)
        assert isinstance(value, ast.Name) and value.id == const_name, (
            f"create-path Job() {field_name} must be {const_name}"
        )


def test_copy_as_new_copies_all_three_from_source_row() -> None:
    """copy_as_new Job() must copy ALL THREE language kwargs verbatim from the
    source row (``source_<row>.<field>``) — feedback_copy_as_new_invariants."""
    _, copy_calls = _classify_job_calls(_parse(JOB_INTERCEPT_REL))
    assert len(copy_calls) == 1, f"expected 1 copy_as_new Job(), found {len(copy_calls)}"
    call = copy_calls[0]
    for field_name in LANG_FIELDS:
        value = _kw_value(call, field_name)
        assert (
            isinstance(value, ast.Attribute)
            and value.attr == field_name
            and isinstance(value.value, ast.Name)
        ), f"copy_as_new Job() {field_name} must copy source row's {field_name}"


def _metering_snapshot_dict_literals(tree: ast.AST) -> list[ast.Dict]:
    dicts: list[ast.Dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "metering_snapshot":
                    dicts.append(node.value)
    return dicts


def test_metering_snapshot_literal_has_language_pair() -> None:
    dicts = _metering_snapshot_dict_literals(_parse(JOB_INTERCEPT_REL))
    assert dicts, "no `*.metering_snapshot = {...}` dict literal found"
    for dict_node in dicts:
        keys = {
            key.value
            for key in dict_node.keys
            if isinstance(key, ast.Constant)
        }
        assert "language_pair" in keys, (
            "metering_snapshot dict literal is missing the language_pair key"
        )


# =====================================================================
# §6. Anonymous lane stays language-field-free + locks default pair.
# =====================================================================


def test_anonymous_preview_spec_has_no_language_fields() -> None:
    for field_name in LANG_FIELDS:
        assert field_name not in ANONYMOUS_PREVIEW_PAYLOAD_SPEC


def test_anonymous_payload_locks_default_pair() -> None:
    """The anonymous lane never carries language fields (whitelist above), so a
    JobRecord built without them falls back to the GA default and resolves to
    en->zh-CN (Phase 1 step 4: 匿名 job 恒解析 en->zh-CN)."""
    rec = _make_job_record()  # no language overrides — mimics anonymous payload
    assert rec.language_pair == DEFAULT_LANGUAGE_PAIR
    assert (
        resolve_language_pair(rec.source_language, rec.target_language)
        is SUPPORTED_LANGUAGE_PAIRS[DEFAULT_LANGUAGE_PAIR]
    )

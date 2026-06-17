"""Guards for payment order reconciliation migration after production head."""
from __future__ import annotations

import ast
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT
    / "gateway"
    / "alembic"
    / "versions"
    / "041_payment_order_last_reconciled_at.py"
)


def _revision_literal(src: str) -> str:
    match = re.search(r'revision:\s*str\s*=\s*"([^"]+)"', src)
    assert match is not None, "migration 041 revision literal not found"
    return match.group(1)


def _module_down_revision(path: Path):
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


def test_migration_041_revision_id_fits_alembic_version_column() -> None:
    """Production alembic_version.version_num is VARCHAR(32)."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    revision = _revision_literal(src)

    assert revision == "041_payment_order_reconcile"
    assert len(revision) <= 32


def test_migration_041_revises_production_head() -> None:
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "040_anon_claim_owner"' in src


def test_alembic_chain_matches_current_production_head() -> None:
    """Keep GitHub migration history aligned with the US production DB."""
    versions_dir = _REPO_ROOT / "gateway" / "alembic" / "versions"
    expected_edges = {
        "035_anonymous_preview": ["036_job_language_fields.py"],
        "036_job_language_fields": ["037_smart_clone_reservations.py"],
        "037_smart_clone_reservations": ["038_smart_clone_created_at_index.py"],
        "038_smart_clone_created_at_index": ["039_smart_clone_carryover.py"],
        "039_smart_clone_carryover": ["040_anonymous_preview_claim_owner.py"],
        "040_anon_claim_owner": ["041_payment_order_last_reconciled_at.py"],
    }
    for parent, expected_children in expected_edges.items():
        children = [
            path.name
            for path in versions_dir.glob("*.py")
            if _module_down_revision(path) == parent
        ]
        assert children == expected_children, (parent, children)

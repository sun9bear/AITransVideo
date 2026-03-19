from __future__ import annotations

"""Legacy compatibility shim for the extracted editor output package."""

from modules.output.editor.editor_package_models import (
    ALIGNMENT_METHOD_LABELS,
    AlignedSegment,
    ProjectOutput,
    ProjectOutputResult,
)
from modules.output.editor.editor_package_writer import EditorPackageWriter


class ProjectOutputWriter(EditorPackageWriter):
    """Compatibility wrapper around the extracted editor package writer."""

from __future__ import annotations

from modules.output.editor.editor_package_models import ProjectOutput, ProjectOutputResult
from modules.output.editor.editor_package_writer import EditorPackageWriter


class EditorPackageBackend:
    """Explicit editor-output backend over the extracted editor package writer."""

    def __init__(self, writer: EditorPackageWriter | None = None) -> None:
        self.writer = writer or EditorPackageWriter()

    def write(self, output: ProjectOutput) -> ProjectOutputResult:
        return self.writer.write(output)

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.enums import OutputTarget
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.publish.publish_models import PublishResult


@dataclass(slots=True)
class OutputRequest:
    targets: list[OutputTarget] = field(default_factory=lambda: [OutputTarget.EDITOR])
    burn_subtitles: bool = False
    mix_original_audio: bool = False
    output_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.targets:
            self.targets = [OutputTarget.EDITOR]
        self.targets = [self._coerce_target(target) for target in self.targets]
        if self.output_dir is not None:
            normalized_output_dir = str(Path(self.output_dir).resolve(strict=False)).strip()
            self.output_dir = normalized_output_dir or None

    def expanded_targets(self) -> tuple[OutputTarget, ...]:
        expanded: list[OutputTarget] = []
        for target in self.targets:
            if target == OutputTarget.BOTH:
                expanded.extend([OutputTarget.EDITOR, OutputTarget.PUBLISH])
                continue
            if target not in expanded:
                expanded.append(target)
        return tuple(expanded)

    @staticmethod
    def _coerce_target(target: OutputTarget | str) -> OutputTarget:
        if isinstance(target, OutputTarget):
            return target
        return OutputTarget(str(target).strip().lower())


@dataclass(slots=True)
class OutputBundleResult:
    editor_result: ProjectOutputResult | None = None
    publish_result: PublishResult | None = None
    manifest_path: str | None = None

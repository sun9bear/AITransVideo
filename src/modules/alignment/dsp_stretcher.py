import shutil
from pathlib import Path

from core.exceptions import AlignmentError


class DSPStretcher:
    def fit_to_duration(
        self,
        input_audio_path: str,
        target_duration_ms: int,
        output_suffix: str = "_aligned",
    ) -> str:
        if target_duration_ms <= 0:
            raise AlignmentError("Target duration must be positive for DSP fitting.")

        source_path = Path(input_audio_path)
        if not source_path.exists():
            raise AlignmentError(f"Input audio does not exist: {input_audio_path}")

        output_path = source_path.with_name(f"{source_path.stem}{output_suffix}{source_path.suffix}")
        shutil.copyfile(source_path, output_path)
        return str(output_path)

from pathlib import Path

from core.exceptions import DraftError
from core.models import SemanticBlock
from modules.draft.schema import DraftMaterial


class MaterialMapper:
    """Map processed blocks into internal draft material records."""

    def map_audio_materials(self, blocks: list[SemanticBlock]) -> list[DraftMaterial]:
        materials: list[DraftMaterial] = []

        for position, block in enumerate(blocks, start=1):
            source_audio_path = block.aligned_audio_path or block.tts_audio_path
            if not source_audio_path:
                raise DraftError(f"Missing aligned audio for block {block.block_id}.")

            source_path = Path(source_audio_path)
            suffix = source_path.suffix or ".wav"
            file_name = f"{block.block_id}{suffix}"
            materials.append(
                DraftMaterial(
                    material_id=f"audio_material_{position:04d}",
                    block_id=block.block_id,
                    source_audio_path=str(source_path),
                    relative_material_path=f"materials/{file_name}",
                    start_ms=block.first_start_ms,
                    duration_ms=block.actual_audio_duration_ms or block.target_duration_ms,
                    speaker_id=block.speaker_id,
                    speaker_name=block.speaker_name,
                )
            )

        return materials

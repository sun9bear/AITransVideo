from core.exceptions import DraftError
from modules.draft.schema import DraftProject


class DraftSchemaValidator:
    """Validate internal draft schema consistency before serialization."""

    def validate(self, project: DraftProject) -> None:
        material_ids = {material.material_id for material in project.materials}
        unique_block_ids = {material.block_id for material in project.materials}
        audio_items = [
            item
            for track in project.timeline.audio_tracks
            for item in track.items
        ]
        caption_items = [
            item
            for track in project.timeline.caption_tracks
            for item in track.items
        ]

        if project.material_count != len(project.materials):
            raise DraftError("Draft material_count does not match materials length.")
        if project.caption_count != len(caption_items):
            raise DraftError("Draft caption_count does not match caption item count.")
        if project.block_count != len(unique_block_ids):
            raise DraftError("Draft block_count does not match unique material block count.")
        if len(audio_items) != len(project.materials):
            raise DraftError("Draft audio track items do not match material count.")

        for audio_item in audio_items:
            if audio_item.material_id not in material_ids:
                raise DraftError(f"Audio item references missing material: {audio_item.material_id}")

        computed_duration_ms = max(
            [item.end_ms for item in audio_items] + [item.end_ms for item in caption_items],
            default=0,
        )
        if project.timeline.duration_ms != computed_duration_ms:
            raise DraftError("Draft timeline.duration_ms does not match track content.")

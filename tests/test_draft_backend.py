from pathlib import Path

from core.enums import StageStatus
from core.models import SemanticBlock, SubtitleLine
from modules.draft.caption_retiming import CaptionRetimer
from modules.draft.draft_writer import DraftWriteResult, DraftWriter
from modules.output.editor.draft_backend import DraftBackend
from modules.workflow.draft_stage_runner import DraftStageRunner, DraftStageRunnerConfig
from services.state_manager import StateManager


def _build_translated_lines() -> list[SubtitleLine]:
    return [
        SubtitleLine(1, 0, 800, "speaker_host", "Host", "Welcome back.", "CN:Welcome"),
        SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "Draft scaffold.", "back"),
    ]


def _build_aligned_blocks(source_audio_path: Path) -> list[SemanticBlock]:
    return [
        SemanticBlock(
            block_id="block_0001",
            speaker_id="speaker_host",
            speaker_name="Host",
            original_srt_indices=[1, 2],
            first_start_ms=0,
            last_end_ms=1_700,
            target_duration_ms=1_700,
            merged_cn_text="CN:Welcomeback",
            actual_audio_duration_ms=1_700,
            aligned_audio_path=str(source_audio_path),
            final_cn_lines=["CN:Welcome", "back"],
        )
    ]


def test_draft_backend_writes_draft_scaffold_as_editor_subcapability(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned_block.wav"
    source_audio_path.write_bytes(b"RIFFdraft")
    backend = DraftBackend(
        caption_retimer=CaptionRetimer(),
        draft_writer=DraftWriter(output_root_dir=str(tmp_path / "output")),
    )

    result = backend.write(
        project_id="draft_backend_demo",
        translated_lines=_build_translated_lines(),
        aligned_blocks=_build_aligned_blocks(source_audio_path),
        stage_snapshot={"alignment": {"status": StageStatus.DONE.value}},
    )
    restored_result = backend.load_existing_result("draft_backend_demo")

    assert Path(result.draft_content_path).exists()
    assert Path(result.draft_meta_info_path).exists()
    assert result.export_path is not None
    assert Path(result.export_path).exists()
    assert result.block_count == 1
    assert result.caption_count == 2
    assert result.material_count == 1
    assert restored_result is not None
    assert restored_result.draft_content_path == result.draft_content_path
    assert restored_result.caption_count == result.caption_count


def test_draft_stage_runner_uses_draft_backend_for_fresh_write(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned_block.wav"
    source_audio_path.write_bytes(b"RIFFdraft")
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    state_manager.set_project("draft_runner_backend_demo")
    state_manager.set_stage("ingestion", StageStatus.DONE, {"input_hash": "draft-backend-input-hash"})

    class StubDraftBackend:
        def __init__(self, output_dir: Path) -> None:
            self.output_dir = output_dir
            self.write_calls: list[dict[str, object]] = []

        def load_existing_result(self, project_id: str) -> DraftWriteResult | None:
            del project_id
            return None

        def write(
            self,
            *,
            project_id: str,
            translated_lines: list[SubtitleLine],
            aligned_blocks: list[SemanticBlock],
            stage_snapshot: dict[str, object] | None = None,
        ) -> DraftWriteResult:
            self.write_calls.append(
                {
                    "project_id": project_id,
                    "translated_lines": translated_lines,
                    "aligned_blocks": aligned_blocks,
                    "stage_snapshot": stage_snapshot,
                }
            )
            draft_dir = self.output_dir / project_id / "draft"
            material_dir = draft_dir / "materials"
            draft_dir.mkdir(parents=True, exist_ok=True)
            material_dir.mkdir(parents=True, exist_ok=True)
            draft_content_path = draft_dir / "draft_content.json"
            draft_meta_info_path = draft_dir / "draft_meta_info.json"
            export_path = draft_dir / "jianying_like_export.json"
            draft_content_path.write_text("{}", encoding="utf-8")
            draft_meta_info_path.write_text("{}", encoding="utf-8")
            export_path.write_text("{}", encoding="utf-8")
            return DraftWriteResult(
                draft_dir=str(draft_dir),
                draft_content_path=str(draft_content_path),
                draft_meta_info_path=str(draft_meta_info_path),
                material_dir=str(material_dir),
                block_count=len(aligned_blocks),
                caption_count=len(translated_lines),
                material_count=len(aligned_blocks),
                export_path=str(export_path),
            )

    stub_backend = StubDraftBackend(tmp_path / "output")
    runner = DraftStageRunner(
        caption_retimer=CaptionRetimer(),
        draft_writer=DraftWriter(output_root_dir=str(tmp_path / "unused_output")),
        state_manager=state_manager,
        config=DraftStageRunnerConfig(project_id="draft_runner_backend_demo"),
        draft_backend=stub_backend,
    )

    result = runner.run(_build_translated_lines(), _build_aligned_blocks(source_audio_path))
    draft_stage = state_manager.get_stage("draft")

    assert len(stub_backend.write_calls) == 1
    assert stub_backend.write_calls[0]["project_id"] == "draft_runner_backend_demo"
    captured_stage_snapshot = stub_backend.write_calls[0]["stage_snapshot"]
    assert isinstance(captured_stage_snapshot, dict)
    assert captured_stage_snapshot["ingestion"]["status"] == StageStatus.DONE.value
    assert captured_stage_snapshot["draft"]["status"] == StageStatus.RUNNING.value
    assert Path(result.draft_content_path).exists()
    assert draft_stage is not None
    assert draft_stage["status"] == StageStatus.DONE.value
    assert draft_stage["payload"]["execution_mode"] == "fresh_write"
    assert draft_stage["payload"]["skipped"] is False

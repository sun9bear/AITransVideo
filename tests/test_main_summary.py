import json
from pathlib import Path
from types import SimpleNamespace

import main
from core.enums import OutputTarget
from modules.output.editor.editor_package_models import ProjectOutputResult
from modules.output.output_models import OutputBundleResult, OutputRequest
from modules.output.publish.publish_models import PublishResult
from services.cache_manager import CacheManager
from services.state_manager import StateManager


def _write_json(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_build_run_summary_surfaces_manifest_and_publish_paths(tmp_path: Path) -> None:
    draft_content_path = _write_json(
        tmp_path / "draft" / "draft_content.json",
        {
            "timeline": {
                "duration_ms": 900,
                "audio_tracks": [{"items": [{"id": "audio_1"}]}],
                "caption_tracks": [{"items": [{"id": "caption_1"}]}],
            }
        },
    )
    draft_meta_info_path = _write_json(
        tmp_path / "draft" / "draft_meta_info.json",
        {"summary": {"segment_count": 1, "needs_review_count": 0}},
    )
    export_path = _write_json(
        tmp_path / "draft" / "jianying_like_export.json",
        {
            "export_target": "jianying_like_export",
            "timeline": {"audio_tracks": [{}], "caption_tracks": [{}]},
            "materials": {"audio_materials": [{}]},
        },
    )
    state_manager = StateManager(str(tmp_path / "project_state.json"))
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    cache_manager.save(
        {
            "entries": {},
            "metrics": {"hits": 1, "misses": 0},
            "last_lookup": {"namespace": "translation", "result": "hit"},
        }
    )

    result = SimpleNamespace(
        draft_dir=str(tmp_path / "draft"),
        draft_content_path=draft_content_path,
        draft_meta_info_path=draft_meta_info_path,
        export_path=export_path,
        stage_snapshot={
            "media_understanding": {"status": "done", "payload": {"source_kind": "local_video"}},
            "translation": {"status": "done", "payload": {"provider_mode": "mock"}},
            "alignment": {"status": "done", "payload": {"provider_mode": "mock", "provider_name": "mock_tts"}},
            "draft": {"status": "done", "payload": {"execution_mode": "fresh_write"}},
        },
    )
    output_bundle = OutputBundleResult(
        editor_result=ProjectOutputResult(
            dubbed_audio_path=str(tmp_path / "output" / "dubbed_audio_complete.wav"),
            ambient_audio_path=str(tmp_path / "output" / "ambient_audio.wav"),
            segments_dir=str(tmp_path / "output" / "segments"),
            segment_count=1,
            subtitles_path=str(tmp_path / "output" / "subtitles.srt"),
            subtitles_en_path=str(tmp_path / "output" / "subtitles_en.srt"),
            subtitles_bilingual_path=str(tmp_path / "output" / "subtitles_bilingual.srt"),
            background_sounds_path=str(tmp_path / "output" / "background_sounds.txt"),
            alignment_report_path=str(tmp_path / "output" / "alignment_report.md"),
            needs_review_count=0,
        ),
        publish_result=PublishResult(
            project_id="summary-demo",
            dubbed_video_path=str(tmp_path / "publish" / "dubbed_video.mp4"),
            original_video_path=str(tmp_path / "source.mp4"),
            dubbed_audio_path=str(tmp_path / "output" / "dubbed_audio_complete.wav"),
        ),
        manifest_path=str(tmp_path / "manifest.json"),
    )
    _write_json(
        tmp_path / "manifest.json",
        {
            "source_info": {
                "source_kind": "local_video",
                "locator": str(tmp_path / "source.mp4"),
                "metadata": {
                    "video_title": "Summary Demo Video",
                },
            }
        },
    )

    summary = main.build_run_summary(
        result,
        state_manager,
        cache_manager,
        run_context={"input_path": str(tmp_path / "source.mp4"), "output_root": str(tmp_path)},
        output_bundle=output_bundle,
        output_request=OutputRequest(targets=[OutputTarget.BOTH]),
    )

    assert summary["output_summary"]["targets"] == ["editor", "publish"]
    assert summary["output_summary"]["manifest_path"] == str(tmp_path / "manifest.json")
    assert summary["output_summary"]["editor_segments_dir"] == str(tmp_path / "output" / "segments")
    assert summary["output_summary"]["publish_dubbed_video_path"] == str(tmp_path / "publish" / "dubbed_video.mp4")
    assert summary["output_summary"]["publish_original_video_path"] == str(tmp_path / "source.mp4")
    assert summary["result_summary"]["manifest_path"] == str(tmp_path / "manifest.json")
    assert summary["result_summary"]["publish_dubbed_video_path"] == str(tmp_path / "publish" / "dubbed_video.mp4")
    assert summary["source_context"] == {
        "source_kind": "local_video",
        "locator": str(tmp_path / "source.mp4"),
        "video_title": "Summary Demo Video",
    }
    assert summary["result_summary"]["source_context"] == summary["source_context"]
    assert summary["project_state_summary"]["overall_status"] == "done"
    assert summary["project_state_summary"]["latest_stage_name"] == "draft"
    assert summary["project_state_summary"]["completed_stage_count"] == 4
    assert summary["stage_execution_summary"]["draft"]["label"] == "Draft"
    assert summary["stage_execution_summary"]["draft"]["status_label"] == "Done"
    assert summary["stage_execution_summary"]["draft"]["summary"] == "fresh_write"


def test_print_run_summary_highlights_output_artifacts(capsys: object) -> None:
    summary = {
        "run_context": {"demo_kind": "local_video_authoritative_demo"},
        "draft_dir": "D:/demo/draft",
        "draft_content_path": "D:/demo/draft/draft_content.json",
        "draft_meta_info_path": "D:/demo/draft/draft_meta_info.json",
        "export_path": "D:/demo/draft/jianying_like_export.json",
        "draft_summary": {"segment_count": 1},
        "timeline_summary": {"duration_ms": 900},
        "export_summary": {"export_target": "jianying_like_export"},
        "provider_mode_summary": {"tts_mode": "mock"},
        "source_context": {
            "source_kind": "local_video",
            "locator": "D:/demo/source.mp4",
            "video_title": "Summary Demo Video",
        },
        "output_summary": {
            "targets": ["editor", "publish"],
            "manifest_path": "D:/demo/manifest.json",
            "editor_draft_dir": "D:/demo/draft",
            "editor_export_path": "D:/demo/draft/jianying_like_export.json",
            "editor_dubbed_audio_path": "D:/demo/output/dubbed_audio_complete.wav",
            "editor_subtitles_path": "D:/demo/output/subtitles.srt",
            "editor_segments_dir": "D:/demo/output/segments",
            "editor_alignment_report_path": "D:/demo/output/alignment_report.md",
            "publish_dubbed_video_path": "D:/demo/publish/dubbed_video.mp4",
        },
        "result_summary": {
            "status": "success",
            "source_context": {
                "source_kind": "local_video",
                "locator": "D:/demo/source.mp4",
                "video_title": "Summary Demo Video",
            },
        },
        "stage_execution_summary": {"draft": {"status": "done"}},
        "project_state_summary": {"overall_status": "done", "latest_stage_name": "draft"},
        "project_state_snapshot": {"stages": {}},
        "cache_snapshot": {"metrics": {"hits": 0, "misses": 0}},
    }

    main._print_run_summary(summary)
    captured = capsys.readouterr()

    assert "Output artifacts:" in captured.out
    assert "Source context:" in captured.out
    assert "Manifest: D:/demo/manifest.json" in captured.out
    assert "Editor dubbed audio: D:/demo/output/dubbed_audio_complete.wav" in captured.out
    assert "Publish dubbed video: D:/demo/publish/dubbed_video.mp4" in captured.out
    assert "Project state summary:" in captured.out

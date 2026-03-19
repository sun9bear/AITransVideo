from services.control_panel import (
    _parse_workbench_cli_output,
    _sanitize_workbench_run,
    render_control_panel_html,
)


def test_render_control_panel_html_prefers_project_state_summary() -> None:
    html = render_control_panel_html()

    assert "const projectStateSummary = run.project_state_summary || {};" in html
    assert "const sourceContext = run.source_context || run.result_summary?.source_context || {};" in html
    assert "项目状态：" in html
    assert "最新阶段：" in html


def test_sanitize_workbench_run_keeps_project_state_summary() -> None:
    sanitized = _sanitize_workbench_run(
        {
            "run_kind": "default_mock_demo",
            "status": "success",
            "command": ["python", "main.py", "demo"],
            "project_state_summary": {
                "overall_status": "done",
                "overall_status_label": "Done",
                "latest_stage_name": "draft",
                "latest_stage_label": "Draft",
                "latest_stage_status": "done",
                "latest_stage_status_label": "Done",
                "stage_count": 4,
                "completed_stage_count": 4,
                "running_stage_count": 0,
                "failed_stage_count": 0,
                "stages": [
                    {
                        "name": "draft",
                        "label": "Draft",
                        "status": "done",
                        "status_label": "Done",
                        "execution_mode": "fresh_write",
                        "summary": "fresh_write",
                        "artifact_count": 0,
                        "updated_at": "2026-03-18T00:00:00+00:00",
                    }
                ],
            },
            "stage_execution_summary": {
                "draft": {
                    "status": "done",
                    "execution_mode": "fresh_write",
                    "literal_text_layer_produced": True,
                }
            },
            "source_context": {
                "source_kind": "local_video",
                "locator": "D:/demo/source.mp4",
                "video_title": "Demo Source",
            },
        }
    )

    assert sanitized is not None
    assert sanitized["source_context"] == {
        "source_kind": "local_video",
        "locator": "D:/demo/source.mp4",
        "video_title": "Demo Source",
    }
    assert sanitized["project_state_summary"]["overall_status"] == "done"
    assert sanitized["project_state_summary"]["latest_stage_name"] == "draft"
    assert sanitized["project_state_summary"]["stages"] == [
        {
            "name": "draft",
            "label": "Draft",
            "status": "done",
            "status_label": "Done",
            "execution_mode": "fresh_write",
            "summary": "fresh_write",
        }
    ]
    assert sanitized["stage_execution_summary"]["draft"]["status"] == "done"
    assert sanitized["stage_execution_summary"]["draft"]["execution_mode"] == "fresh_write"


def test_sanitize_workbench_run_backfills_project_state_summary_from_stage_execution_summary() -> None:
    sanitized = _sanitize_workbench_run(
        {
            "run_kind": "default_mock_demo",
            "status": "success",
            "command": ["python", "main.py", "demo"],
            "result_summary": {
                "source_context": {
                    "source_kind": "local_audio",
                    "locator": "D:/demo/input.wav",
                    "video_title": "Audio Demo",
                }
            },
            "stage_execution_summary": {
                "draft": {
                    "status": "done",
                    "execution_mode": "fresh_write",
                }
            },
        }
    )

    assert sanitized is not None
    assert sanitized["source_context"] == {
        "source_kind": "local_audio",
        "locator": "D:/demo/input.wav",
        "video_title": "Audio Demo",
    }
    assert sanitized["project_state_summary"]["overall_status"] == "done"
    assert sanitized["project_state_summary"]["latest_stage_name"] == "draft"
    assert sanitized["project_state_summary"]["stages"] == [
        {
            "name": "draft",
            "label": "Draft",
            "status": "done",
            "status_label": "Done",
            "execution_mode": "fresh_write",
            "summary": "fresh_write",
        }
    ]


def test_parse_workbench_cli_output_reads_project_state_summary() -> None:
    stdout = "\n".join(
        [
            "Draft scaffold written to: D:/demo/draft",
            "Provider mode summary:",
            "{'tts_mode': 'mock'}",
            "Source context:",
            "{'source_kind': 'local_video', 'locator': 'D:/demo/source.mp4', 'video_title': 'Demo Source'}",
            "Run result summary:",
            "{'status': 'success'}",
            "Project state summary:",
            "{'overall_status': 'done', 'latest_stage_name': 'draft'}",
            "Stage execution summary:",
            "{'draft': {'status': 'done', 'execution_mode': 'fresh_write'}}",
        ]
    )

    parsed = _parse_workbench_cli_output(stdout)

    assert parsed["draft_path"] == "D:/demo/draft"
    assert parsed["source_context"]["source_kind"] == "local_video"
    assert parsed["source_context"]["locator"] == "D:/demo/source.mp4"
    assert parsed["project_state_summary"]["overall_status"] == "done"
    assert parsed["project_state_summary"]["latest_stage_name"] == "draft"
    assert parsed["stage_execution_summary"]["draft"]["status"] == "done"

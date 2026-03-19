import json
from pathlib import Path

from core.enums import StageStatus
from services.state_manager import StateManager


def test_state_manager_save_writes_valid_json_atomically(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    manager = StateManager(str(state_path))
    state = {
        "project_id": "atomic_demo",
        "stages": {
            "ingestion": {
                "status": "done",
                "started_at": "2026-03-12T00:00:00+00:00",
                "finished_at": "2026-03-12T00:00:01+00:00",
                "updated_at": "2026-03-12T00:00:01+00:00",
                "error_message": None,
                "payload": {"line_count": 2},
            }
        },
    }

    manager.save(state)

    assert json.loads(state_path.read_text(encoding="utf-8")) == state


def test_state_manager_reads_and_writes_stage_state(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    manager = StateManager(str(state_path))

    empty_state = manager.load()
    assert empty_state == {"project_id": None, "stages": {}}

    manager.set_project("demo_project")
    manager.set_stage("ingestion", StageStatus.RUNNING, {"line_count": 2})
    manager.set_stage("ingestion", StageStatus.DONE, {"line_count": 2})
    manager.set_stage("translation", StageStatus.FAILED, {"batch_count": 1}, error_message="boom")

    loaded_state = manager.load()
    ingestion_stage = loaded_state["stages"]["ingestion"]
    translation_stage = loaded_state["stages"]["translation"]

    assert loaded_state["project_id"] == "demo_project"
    assert ingestion_stage["status"] == "done"
    assert ingestion_stage["payload"]["line_count"] == 2
    assert ingestion_stage["started_at"] is not None
    assert ingestion_stage["finished_at"] is not None
    assert ingestion_stage["updated_at"] is not None
    assert ingestion_stage["error_message"] is None
    assert translation_stage["status"] == "failed"
    assert translation_stage["error_message"] == "boom"
    assert translation_stage["payload"] == {"batch_count": 1}


def test_state_manager_clears_failure_fields_when_stage_is_rerun(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    manager = StateManager(str(state_path))

    failed_stage = manager.set_stage("media_understanding", StageStatus.FAILED, error_message="backend crashed")
    rerun_stage = manager.set_stage("media_understanding", StageStatus.RUNNING, {"attempt": 2})

    assert failed_stage["status"] == StageStatus.FAILED.value
    assert failed_stage["error_message"] == "backend crashed"
    assert failed_stage["finished_at"] is not None
    assert rerun_stage["status"] == StageStatus.RUNNING.value
    assert rerun_stage["error_message"] is None
    assert rerun_stage["finished_at"] is None
    assert rerun_stage["started_at"] is not None
    assert rerun_stage["payload"] == {"attempt": 2}


def test_state_manager_clears_finished_at_when_done_stage_reenters_running(tmp_path: Path) -> None:
    state_path = tmp_path / "project_state.json"
    manager = StateManager(str(state_path))

    manager.set_stage("translation", StageStatus.RUNNING, {"batch_count": 1})
    done_stage = manager.set_stage("translation", StageStatus.DONE, {"batch_count": 1})
    rerun_stage = manager.set_stage("translation", StageStatus.RUNNING, {"batch_count": 2})
    completed_stage = manager.set_stage("translation", StageStatus.DONE, {"batch_count": 2})

    assert done_stage["status"] == StageStatus.DONE.value
    assert done_stage["finished_at"] is not None
    assert rerun_stage["status"] == StageStatus.RUNNING.value
    assert rerun_stage["finished_at"] is None
    assert rerun_stage["error_message"] is None
    assert rerun_stage["payload"] == {"batch_count": 2}
    assert completed_stage["status"] == StageStatus.DONE.value
    assert completed_stage["finished_at"] is not None
    assert completed_stage["error_message"] is None

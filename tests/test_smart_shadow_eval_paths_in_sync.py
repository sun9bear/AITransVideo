"""ARTIFACT_PATHS 与 fixture 同步守卫：每条 entry 在 post_phase_full fixture 中至少有 1 个真实文件。"""
import importlib.util
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_collector.py")
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"
           / "projects" / "test_pid_001" / "job_post_phase_full")


def _load_artifact_paths():
    spec = importlib.util.spec_from_file_location("collector", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ARTIFACT_PATHS


def test_every_artifact_path_exists_in_full_fixture():
    paths = _load_artifact_paths()
    project_dir_paths = {k: v for k, v in paths.items()
                         if not v.startswith("{job_id}")}
    missing = []
    for name, rel in project_dir_paths.items():
        if not (FIXTURE / rel).exists():
            missing.append((name, rel))
    assert not missing, (
        f"ARTIFACT_PATHS entries missing in fixture: {missing}. "
        "If you added a path constant, add a corresponding fixture file."
    )

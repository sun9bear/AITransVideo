from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.jobs.models import JobRecord
from services.jobs.process_runner import (
    ProcessJobRunner,
    _parse_project_dir_from_line,
    _resolve_job_project_dir,
)
from services.jobs.store import JobStore


def _write_process_project(project_dir: Path, *, youtube_url: str) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "download_metadata.json").write_text(
        json.dumps(
            {
                "url": youtube_url,
                "video_title": project_dir.name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (project_dir / "project_state.json").write_text(
        json.dumps({"project_id": project_dir.name, "stages": {}}),
        encoding="utf-8",
    )
    return project_dir


def _make_job(**overrides) -> JobRecord:
    base = {
        "job_id": "job_test001",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.example/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "created_at": "2026-03-31T00:00:00Z",
        "updated_at": "2026-03-31T00:00:00Z",
    }
    base.update(overrides)
    return JobRecord.from_dict(base)


def _make_runner(tmp_path: Path) -> ProcessJobRunner:
    store = JobStore(tmp_path / "jobs")
    return ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=MagicMock(),
        run_timeout_seconds=5,
    )


# ===================================================================
# _parse_project_dir_from_line
# ===================================================================


def test_parse_project_dir_from_posix_log_line_preserves_linux_path_text() -> None:
    project_dir = _parse_project_dir_from_line(
        "[S6] Done /opt/aivideotrans/data/projects/demo/output",
        Path("D:/workspace/app"),
    )

    assert project_dir == "/opt/aivideotrans/data/projects/demo"


def test_parse_project_dir_from_windows_log_line() -> None:
    project_dir = _parse_project_dir_from_line(
        "[S6] Done D:\\workspace\\projects\\my_video\\output",
        Path("D:/workspace"),
    )

    assert project_dir is not None
    assert "my_video" in project_dir


def test_parse_project_dir_returns_none_for_no_path() -> None:
    assert _parse_project_dir_from_line("[S3] Translating...", Path(".")) is None


# ===================================================================
# _resolve_stage_from_log_line — progress_message isolation
#
# 2026-04-20 bleed bug: Python logger.warning output from in-pipeline
# DSP code ("segment 172: atempo stretch ratio=0.33x ...") landed in
# JobRecord.progress_message because _resolve_stage_from_log_line
# hoisted *every* non-stage-prefixed line to progress_message. The
# user saw our internal debug log in the workspace card header.
#
# Fix: non-prefixed lines preserve the last stage-derived message
# verbatim. They still land in the event log (admin LogViewer) via
# store.append_event(), but don't bubble up to the worker header.
# ===================================================================


class TestResolveStageMessageIsolation:
    """Rules the module contract pins:
    1. ``[SN]`` or ``[RESUME/SN]`` line → update both stage + message
    2. ``[download] X`` line → update ingestion stage + download message
    3. Any other line → preserve previous (stage, message) tuple
    """

    def _call(self, *, line: str, current_stage, current_message):
        from services.jobs.process_runner import _resolve_stage_from_log_line
        return _resolve_stage_from_log_line(
            line=line,
            current_stage=current_stage,
            current_message=current_message,
        )

    def test_s_prefix_line_updates_message(self) -> None:
        stage, msg = self._call(
            line="[S3] 翻译文本...",
            current_stage="draft", current_message="old message",
        )
        assert msg == "翻译文本..."
        assert stage == "translation_review"  # via STAGE_CODE_MAP["S3"]

    def test_resume_prefix_line_updates_message(self) -> None:
        stage, msg = self._call(
            line="[RESUME/S6] 合成配音视频/字幕...",
            current_stage="draft", current_message="old",
        )
        assert msg == "合成配音视频/字幕..."
        assert stage == "legacy_process_output"

    def test_non_prefix_log_line_does_not_pollute_progress_message(self) -> None:
        """Regression for the 2026-04-20 UX bleed. A Python
        ``logger.warning`` message coming through the merged stdout
        stream must NOT overwrite progress_message — it would render
        in the user's workspace card, exposing internal debug wording
        (English + repr format) to end users."""
        bleed = (
            "segment 172: atempo stretch ratio=0.33x "
            "(actual=147ms → slot=440ms) exceeds the quality-safe "
            "[0.5x, 2.0x] window; output wav is valid at target "
            "duration but audio quality degrades — user reviews in "
            "test-playback UI"
        )
        stage, msg = self._call(
            line=bleed,
            current_stage="legacy_process_output",
            current_message="合成配音视频/字幕...",
        )
        assert msg == "合成配音视频/字幕...", (
            f"progress_message was polluted by non-prefixed log line: "
            f"{msg!r}"
        )
        assert stage == "legacy_process_output"

    def test_raw_third_party_provider_log_does_not_pollute(self) -> None:
        """[MiniMax] / [cosyvoice] logs from TTS providers also bleed
        in practice — same guard covers them."""
        stage, msg = self._call(
            line="[MiniMax] voice=moss_abc, confidence=high, source=explicit",
            current_stage="draft", current_message="生成TTS音频...",
        )
        assert msg == "生成TTS音频..."
        assert stage == "draft"


# ===================================================================
# Stage log regex — must recognise both classic [SN] and γ [RESUME/SN]
#
# D33 fix (2026-04-20): γ publish-only resume path prints ``[RESUME/S5]``
# and ``[RESUME/S6]`` prefixes. The old regex only matched bare `[SN]`,
# so γ log lines never advanced the stepper — user saw "step 1 输入准备
# 待开始" throughout the entire commit γ run even though alignment +
# publish were executing.
# ===================================================================


class TestStageLogPatternResumePrefix:
    def test_classic_s5_still_matches(self) -> None:
        from services.jobs.process_runner import STAGE_LOG_PATTERN
        m = STAGE_LOG_PATTERN.match("[S5] 对齐时间轴...")
        assert m is not None
        assert m.group(1) == "S5"

    def test_resume_s5_matches_and_captures_stage_code(self) -> None:
        """γ's ``[RESUME/S5] 跳过对齐...`` must produce the same stage
        code as the classic variant so STAGE_CODE_MAP routes it to the
        same public stage."""
        from services.jobs.process_runner import STAGE_LOG_PATTERN
        m = STAGE_LOG_PATTERN.match("[RESUME/S5] 跳过对齐（γ 路径）：...")
        assert m is not None, "STAGE_LOG_PATTERN failed to match [RESUME/S5]"
        assert m.group(1) == "S5", (
            f"expected inner group 'S5', got {m.group(1)!r}"
        )

    def test_resume_s6_matches(self) -> None:
        from services.jobs.process_runner import STAGE_LOG_PATTERN
        m = STAGE_LOG_PATTERN.match("[RESUME/S6] 合成配音音频/配音视频...")
        assert m is not None
        assert m.group(1) == "S6"

    def test_stage_code_map_s5_routes_to_draft_not_voice_selection(self) -> None:
        """Public stage for S5 must be STAGE_DRAFT (草稿与配音 = step 8
        in the stepper), not STAGE_VOICE_SELECTION_REVIEW. The S5 prefix
        is printed during alignment by both the classic pipeline
        ([S5] 对齐时间轴...) and γ ([RESUME/S5] 跳过对齐...)."""
        from services.jobs.process_runner import STAGE_CODE_MAP
        from services.jobs.models import STAGE_DRAFT, STAGE_VOICE_SELECTION_REVIEW
        assert STAGE_CODE_MAP["S5"] == STAGE_DRAFT, (
            f"S5 must map to STAGE_DRAFT (alignment), got "
            f"{STAGE_CODE_MAP['S5']!r}"
        )
        assert STAGE_CODE_MAP["S5"] != STAGE_VOICE_SELECTION_REVIEW, (
            "S5 was previously misrouted to voice_selection_review "
            "(gate stage, no [SN] log emission) — this regression guard "
            "pins the correct semantic mapping"
        )


# ===================================================================
# _resolve_job_project_dir
# ===================================================================


def test_resolve_job_project_dir_finds_project_under_data_projects_root(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    expected_project_dir = _write_process_project(
        tmp_path / "data" / "projects" / "demo_project",
        youtube_url="https://youtube.example/watch?v=data-project-root",
    )

    resolved_project_dir = _resolve_job_project_dir(
        project_root=app_root,
        source_ref="https://youtube.example/watch?v=data-project-root",
        preferred_project_dir=None,
    )

    assert resolved_project_dir == expected_project_dir.resolve(strict=False)


def test_resolve_job_project_dir_prefers_preferred_project_dir(tmp_path: Path) -> None:
    preferred = tmp_path / "preferred_dir"
    preferred.mkdir()
    (preferred / "dummy.txt").write_text("exists")

    resolved = _resolve_job_project_dir(
        project_root=tmp_path,
        source_ref="https://youtube.example/watch?v=irrelevant",
        preferred_project_dir=str(preferred),
    )

    assert resolved == preferred.resolve(strict=False)


def test_resolve_job_project_dir_returns_none_when_nothing_found(tmp_path: Path) -> None:
    resolved = _resolve_job_project_dir(
        project_root=tmp_path,
        source_ref="https://youtube.example/watch?v=nonexistent",
        preferred_project_dir=None,
    )

    assert resolved is None


# ===================================================================
# _resolve_job_project_dir — workspace_dir priority tests
# ===================================================================


class TestFinalizeProjectDirResolution:
    """Verify _resolve_job_project_dir priority: project_dir > workspace_dir > legacy search."""

    def test_project_dir_wins_over_workspace_dir(self, tmp_path: Path):
        project_dir = tmp_path / "project_actual"
        project_dir.mkdir()
        workspace_dir = tmp_path / "projects" / "42" / "job_abc"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=irrelevant",
            preferred_project_dir=str(project_dir),
            workspace_dir=str(workspace_dir),
        )

        assert resolved == project_dir.resolve(strict=False)

    def test_workspace_dir_used_when_project_dir_missing(self, tmp_path: Path):
        workspace_dir = tmp_path / "projects" / "42" / "job_xyz"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=irrelevant",
            preferred_project_dir=None,
            workspace_dir=str(workspace_dir),
        )

        assert resolved == workspace_dir.resolve(strict=False)

    def test_workspace_dir_prevents_fallback_to_same_url_legacy_dir(self, tmp_path: Path):
        """Even if a legacy dir has matching URL, workspace_dir should win."""
        # Set up legacy dir with matching URL
        legacy_dir = _write_process_project(
            tmp_path / "projects" / "old_slug",
            youtube_url="https://youtube.example/watch?v=same-url",
        )
        # Set up workspace dir
        workspace_dir = tmp_path / "projects" / "42" / "job_new"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=same-url",
            preferred_project_dir=None,
            workspace_dir=str(workspace_dir),
        )

        # Must return workspace, not legacy
        assert resolved == workspace_dir.resolve(strict=False)
        assert resolved != legacy_dir.resolve(strict=False)

    def test_legacy_search_used_when_both_dirs_missing(self, tmp_path: Path):
        """Legacy fallback only triggers when project_dir and workspace_dir are both None."""
        app_root = tmp_path / "app"
        app_root.mkdir()
        legacy_dir = _write_process_project(
            tmp_path / "app" / "projects" / "legacy_project",
            youtube_url="https://youtube.example/watch?v=legacy",
        )

        resolved = _resolve_job_project_dir(
            project_root=app_root,
            source_ref="https://youtube.example/watch?v=legacy",
            preferred_project_dir=None,
            workspace_dir=None,
        )

        assert resolved == legacy_dir.resolve(strict=False)

    def test_nonexistent_workspace_dir_falls_through_to_legacy(self, tmp_path: Path):
        """If workspace_dir path doesn't exist on disk, fall through to legacy search."""
        app_root = tmp_path / "app"
        app_root.mkdir()
        legacy_dir = _write_process_project(
            tmp_path / "app" / "projects" / "legacy_project",
            youtube_url="https://youtube.example/watch?v=fallthrough",
        )

        resolved = _resolve_job_project_dir(
            project_root=app_root,
            source_ref="https://youtube.example/watch?v=fallthrough",
            preferred_project_dir=None,
            workspace_dir="/nonexistent/workspace/path",
        )

        assert resolved == legacy_dir.resolve(strict=False)


# ===================================================================
# _build_command — source type / ref / workspace_dir
# ===================================================================


class TestBuildCommand:
    def test_youtube_url_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="youtube_url", source_ref="https://yt.com/v=abc")
        cmd = runner._build_command(job, continue_existing=False)

        assert "--source-type" in cmd
        assert cmd[cmd.index("--source-type") + 1] == "youtube_url"
        assert "--source-ref" in cmd
        assert cmd[cmd.index("--source-ref") + 1] == "https://yt.com/v=abc"
        # No longer as positional arg after "process"
        assert cmd[3] == "process"
        assert cmd[4] == "--source-type"

    def test_local_video_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="local_video", source_ref="/uploads/42/video.mp4")
        cmd = runner._build_command(job, continue_existing=False)

        assert cmd[cmd.index("--source-type") + 1] == "local_video"
        assert cmd[cmd.index("--source-ref") + 1] == "/uploads/42/video.mp4"

    def test_local_audio_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="local_audio", source_ref="D:/input.wav")
        cmd = runner._build_command(job, continue_existing=False)

        assert cmd[cmd.index("--source-type") + 1] == "local_audio"
        assert cmd[cmd.index("--source-ref") + 1] == "D:/input.wav"

    def test_new_job_with_workspace_dir_passes_project_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(workspace_dir="projects/42/job_test001")
        cmd = runner._build_command(job, continue_existing=False)

        assert "--project-dir" in cmd
        assert cmd[cmd.index("--project-dir") + 1] == "projects/42/job_test001"

    def test_new_job_without_workspace_dir_omits_project_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job()
        cmd = runner._build_command(job, continue_existing=False)

        assert "--project-dir" not in cmd

    def test_continue_prefers_project_dir_over_workspace_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir="/resolved/actual/dir",
            workspace_dir="projects/42/job_test001",
        )
        cmd = runner._build_command(job, continue_existing=True)

        assert cmd[cmd.index("--project-dir") + 1] == "/resolved/actual/dir"

    def test_continue_falls_back_to_workspace_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(workspace_dir="projects/42/job_test001")
        # project_dir is None
        cmd = runner._build_command(job, continue_existing=True)

        assert "--project-dir" in cmd
        assert cmd[cmd.index("--project-dir") + 1] == "projects/42/job_test001"

    def test_start_preserves_alignment_current_stage_in_continue_mode(
        self, tmp_path: Path,
    ):
        """Step 2 Layer 1 — runner.start() had unconditionally reset
        current_stage to STAGE_INGESTION, which silently broke
        commit-triggered alignment resumes: submit_job_from_existing_project_dir
        set job.current_stage='alignment' but runner immediately overwrote it
        before _build_command could read it.

        Now runner must preserve current_stage when continue_existing=True
        AND the stage is in the resumable allowlist (alignment today).
        """
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir=str(tmp_path / "proj"),
            current_stage="alignment",
        )
        # Stub out the subprocess + monitor to keep start() focused on the
        # state transition we care about.
        runner._popen_factory = MagicMock(
            return_value=MagicMock(stdout=iter([]), wait=MagicMock(return_value=0))
        )
        runner._monitor_process = MagicMock()  # type: ignore[method-assign]

        runner.start(job, continue_existing=True)

        saved = runner.store.require_job(job.job_id)
        assert saved.current_stage == "alignment", (
            f"start() overwrote current_stage to {saved.current_stage!r}; "
            "commit-triggered alignment resume would never reach the "
            "pipeline's resume branch"
        )

    def test_start_still_resets_current_stage_for_non_allowlist_stages(
        self, tmp_path: Path,
    ):
        """Regression guard on the allowlist: a job with an unexpected
        current_stage (legacy 'media_understanding' etc.) in continue mode
        must still get reset to INGESTION so the pipeline runs the full
        flow — we only recognise 'alignment' as a resume entry point."""
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir=str(tmp_path / "proj"),
            current_stage="media_understanding",
        )
        runner._popen_factory = MagicMock(
            return_value=MagicMock(stdout=iter([]), wait=MagicMock(return_value=0))
        )
        runner._monitor_process = MagicMock()  # type: ignore[method-assign]

        runner.start(job, continue_existing=True)

        saved = runner.store.require_job(job.job_id)
        assert saved.current_stage == "ingestion"

    def test_start_resets_current_stage_when_not_continue_existing(
        self, tmp_path: Path,
    ):
        """Fresh jobs (continue_existing=False) always start at INGESTION
        regardless of a pre-set current_stage on the record."""
        runner = _make_runner(tmp_path)
        job = _make_job(current_stage="alignment")
        runner._popen_factory = MagicMock(
            return_value=MagicMock(stdout=iter([]), wait=MagicMock(return_value=0))
        )
        runner._monitor_process = MagicMock()  # type: ignore[method-assign]

        runner.start(job, continue_existing=False)

        saved = runner.store.require_job(job.job_id)
        assert saved.current_stage == "ingestion"

    def test_continue_with_alignment_current_stage_forwards_resume_from(
        self, tmp_path: Path,
    ):
        """Step 2 Layer 2 — commit copy_as_new / overwrite sets
        ``current_stage='alignment'`` on the JobRecord before calling
        runner.start. The subprocess command must carry that intent via
        ``--resume-from alignment`` so ProcessPipeline.run can branch
        directly into alignment+publish."""
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir="/some/project",
            current_stage="alignment",
        )
        cmd = runner._build_command(job, continue_existing=True)

        assert "--resume-from" in cmd
        assert cmd[cmd.index("--resume-from") + 1] == "alignment"

    def test_continue_without_alignment_stage_omits_resume_from(
        self, tmp_path: Path,
    ):
        """Only allowlisted start stages trigger --resume-from. Regular
        continue (e.g. after waiting_for_review) does not carry the flag."""
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir="/some/project",
            current_stage="translation_review",
        )
        cmd = runner._build_command(job, continue_existing=True)

        assert "--resume-from" not in cmd

    def test_new_job_never_forwards_resume_from(self, tmp_path: Path):
        """Fresh jobs (continue_existing=False) must not carry
        --resume-from even if current_stage happens to be set."""
        runner = _make_runner(tmp_path)
        job = _make_job(current_stage="alignment")
        cmd = runner._build_command(job, continue_existing=False)

        assert "--resume-from" not in cmd

    def test_preserves_job_id_and_other_flags(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(voice_a="voice-001", voice_b="voice-002")
        # Set transcription_method directly (from_dict doesn't parse it)
        job.transcription_method = "gemini"
        cmd = runner._build_command(job, continue_existing=False)

        assert "--job-id" in cmd
        assert "--voice-a" in cmd
        assert cmd[cmd.index("--voice-a") + 1] == "voice-001"
        assert "--voice-b" in cmd
        assert cmd[cmd.index("--voice-b") + 1] == "voice-002"
        assert "--transcription-method" in cmd
        assert cmd[cmd.index("--transcription-method") + 1] == "gemini"
        assert "--wait-for-review" in cmd


# ===================================================================
# _record_line — stdout must not overwrite JobRecord.project_dir identity
#
# 2026-04-19 incident recap: copy_as_new's target carried some source
# absolute paths inside download_metadata.json / project_state.json
# (path-rewrite was only applied to translation/segments.json). γ resume
# read those JSONs, their absolute paths surfaced in subprocess stdout,
# and _record_line parsed the last path on every log line and saved it
# as JobRecord.project_dir. A subsequent enter_editing + commit overwrite
# on the "copy" then operated on source — destroying source output.
#
# Data-source fix (copy_service path rewrite) stops the leak; these
# tests lock in the control-plane fix: once JobRecord.project_dir is
# set, stdout / review-marker payloads may NOT redefine it. Identity
# is write-once by the submit path, observation-only in log parsers.
# ===================================================================


class TestRecordLineIdentityGuard:
    def _make_runner_with_job(self, tmp_path: Path, *, project_dir: str | None):
        runner = _make_runner(tmp_path)
        job = _make_job(project_dir=project_dir)
        runner.store.save_job(job)
        return runner, job

    def test_stdout_path_may_not_overwrite_existing_project_dir(
        self, tmp_path: Path,
    ) -> None:
        """Regression for the 2026-04-19 copy-as-new pollution chain:
        a log line that mentions a *different* project_dir must NOT
        rewrite JobRecord.project_dir when the record already has one."""
        runner, job = self._make_runner_with_job(
            tmp_path,
            project_dir="/opt/aivideotrans/app/projects/user1/job_correct",
        )
        poisoned_line = (
            "[S6] 完成：输出目录 "
            "/opt/aivideotrans/app/projects/user1/job_SOMEONE_ELSE/output"
        )

        runner._record_line(job.job_id, poisoned_line)

        after = runner.store.require_job(job.job_id)
        assert after.project_dir == (
            "/opt/aivideotrans/app/projects/user1/job_correct"
        ), (
            "stdout log line redefined JobRecord.project_dir — identity "
            "field must be write-once by submit/copy_service, not by "
            "arbitrary stdout scraping"
        )

    def test_stdout_path_fills_missing_project_dir_when_none(
        self, tmp_path: Path,
    ) -> None:
        """Historical feature that must stay working: if the record
        was submitted without an explicit project_dir (pipeline picks
        one from video_title), the first stdout path mention seeds it."""
        runner, job = self._make_runner_with_job(tmp_path, project_dir=None)
        line = (
            "[S0] 项目目录："
            "/opt/aivideotrans/app/projects/user1/job_bootstrap/output"
        )

        runner._record_line(job.job_id, line)

        after = runner.store.require_job(job.job_id)
        assert after.project_dir is not None
        assert "job_bootstrap" in after.project_dir

    def test_review_marker_payload_may_not_overwrite_existing_project_dir(
        self, tmp_path: Path,
    ) -> None:
        """Same rule for [WEB_REVIEW] markers: the structured payload
        carries a project_dir field that was the original backdoor. Once
        identity is set, marker payload must not redefine it — only
        first-time fill-in is allowed."""
        runner, job = self._make_runner_with_job(
            tmp_path,
            project_dir="/opt/aivideotrans/app/projects/user1/job_correct",
        )
        marker_payload = {
            "stage": "voice_selection_review",
            "project_dir": "/opt/aivideotrans/app/projects/user1/job_HIJACK",
            "message": "please review",
        }
        marker_line = f"[WEB_REVIEW] {json.dumps(marker_payload)}"

        runner._record_line(job.job_id, marker_line)

        after = runner.store.require_job(job.job_id)
        assert after.project_dir == (
            "/opt/aivideotrans/app/projects/user1/job_correct"
        ), (
            "[WEB_REVIEW] marker payload redefined JobRecord.project_dir "
            "— identity must be immutable once set"
        )

    def test_review_marker_fills_missing_project_dir_when_none(
        self, tmp_path: Path,
    ) -> None:
        """Preserve the legacy bootstrap path for review markers too:
        if JobRecord.project_dir is None, marker payload seeds it."""
        runner, job = self._make_runner_with_job(tmp_path, project_dir=None)
        marker_payload = {
            "stage": "voice_selection_review",
            "project_dir": "/opt/aivideotrans/app/projects/user1/job_from_marker",
            "message": "please review",
        }
        marker_line = f"[WEB_REVIEW] {json.dumps(marker_payload)}"

        runner._record_line(job.job_id, marker_line)

        after = runner.store.require_job(job.job_id)
        assert after.project_dir is not None
        assert "job_from_marker" in after.project_dir

    def test_parse_is_skipped_when_project_dir_already_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Polish cleanup (2026-04-20): stdout path parsing is narrowed
        to the bootstrap-only path. When ``JobRecord.project_dir`` is
        already set (post-Phase-1 default: gateway submits / copy
        commits pre-populate it), ``_parse_project_dir_from_line`` is
        skipped entirely — saves CPU on hot log paths and silences the
        identity-guard warning log noise that would otherwise fire for
        every stray path in the stream."""
        import services.jobs.process_runner as prm
        runner, job = self._make_runner_with_job(
            tmp_path,
            project_dir="/opt/aivideotrans/app/projects/user1/job_correct",
        )
        call_count = {"n": 0}
        real_parse = prm._parse_project_dir_from_line

        def _counting_parse(line: str, root):
            call_count["n"] += 1
            return real_parse(line, root)
        monkeypatch.setattr(prm, "_parse_project_dir_from_line", _counting_parse)

        runner._record_line(
            job.job_id,
            "[S6] 输出目录 /opt/aivideotrans/app/projects/user1/job_other/output",
        )

        assert call_count["n"] == 0, (
            f"_parse_project_dir_from_line was called {call_count['n']}x "
            "despite JobRecord.project_dir already being set — short-"
            "circuit broken, CPU + warning-log waste restored"
        )


class TestRecordLineParserFillInStillWorks:
    """Complementary guard: bootstrap path must still call the parser
    when project_dir is None. Separate class so the monkeypatch from
    the identity-guard test doesn't leak into these cases."""

    def _make_runner_with_job(self, tmp_path: Path, *, project_dir: str | None):
        runner = _make_runner(tmp_path)
        job = _make_job(project_dir=project_dir)
        runner.store.save_job(job)
        return runner, job

    def test_parse_is_invoked_when_project_dir_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bootstrap path: fresh submit without pre-set project_dir
        must still parse the log line to discover the pipeline-derived
        slug."""
        import services.jobs.process_runner as prm
        runner, job = self._make_runner_with_job(tmp_path, project_dir=None)
        call_count = {"n": 0}
        real_parse = prm._parse_project_dir_from_line

        def _counting_parse(line: str, root):
            call_count["n"] += 1
            return real_parse(line, root)
        monkeypatch.setattr(prm, "_parse_project_dir_from_line", _counting_parse)

        runner._record_line(
            job.job_id,
            "[S0] 项目目录：/opt/aivideotrans/app/projects/user1/job_bootstrap/output",
        )

        assert call_count["n"] == 1, (
            f"parse called {call_count['n']}x when project_dir was None; "
            "bootstrap path broken — fresh submits would never discover "
            "their pipeline-derived project_dir"
        )

"""Dataclass models for Jianying draft request and result (Task J1).

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.1

validation_status valid values (JianyingDraftResult.validation_status):
- "ok"                      — draft generated successfully and passed validator.
- "skipped_no_engine"       — pyJianYingDraft not installed; no draft generated.
- "skipped_disabled"        — feature flag off (AVT_ENABLE_JIANYING_DRAFT != 1).
- "skipped_service_mode"    — service_mode != "studio".
- "skipped_missing_input"   — required input file missing (e.g. no source video).
- "failed"                  — generation attempted but raised an error.

JianyingDraftRequest.user_draft_root (K11 / plan §11):
If user_draft_root is provided, the writer emits absolute material paths
({user_draft_root}<sep><draft_name><sep>materials<sep><filename>) so Jianying
can load them directly after the user unzips into that root. If None, falls
back to relative paths (legacy phase 1 PoC behavior; user must manually
"link media" in Jianying).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class JianyingDraftRequest:
    """Request to generate an editable Jianying draft project.

    All fields correspond to assets from ProjectOutput and EditorPackageWriter.
    Defaults for width and height represent the common 1080p project size.
    """

    project_id: str
    project_title: str
    source_video_path: str
    dubbed_audio_path: str
    subtitle_path: str
    output_dir: str
    ambient_audio_path: str | None = None
    width: int = 1920
    height: int = 1080
    user_draft_root: str | None = None


@dataclass(slots=True)
class JianyingDraftResult:
    """Result of a Jianying draft generation attempt.

    Paths point to files and directories created in output_dir. manifest_path
    may be None if no manifest was created (e.g. skipped generations).
    validation_status indicates success or the reason generation was skipped or failed.
    """

    draft_dir: str
    draft_zip_path: str
    draft_content_path: str
    draft_meta_info_path: str
    manifest_path: str | None
    compatibility_report_path: str
    validation_status: str

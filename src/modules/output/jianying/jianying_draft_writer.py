"""Jianying draft writer: turns a JianyingDraftRequest into a draft directory
and portable zip (Task J2).

Optional dependency: pyJianYingDraft (0.2.6+).  This module can be imported on
any environment even if the library is not installed.  Only constructing
_PyJianYingDraftAdapter will raise JianyingEngineUnavailable if absent.

Install when needed:
    pip install pyJianYingDraft

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.2
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from pathlib import Path

from modules.output.jianying.jianying_draft_models import (
    JianyingDraftRequest,
    JianyingDraftResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class JianyingEngineUnavailable(Exception):
    """Raised when pyJianYingDraft cannot be imported.

    The backend (J4) catches this and translates it to
    validation_status='skipped_no_engine'.
    """


# ---------------------------------------------------------------------------
# Path sanitization helper (also used by tests)
# ---------------------------------------------------------------------------


def _sanitize_draft_name(project_id: str) -> str:
    """Replace filesystem-unsafe characters with underscores.

    Characters replaced: forward slash, backslash, colon.
    """
    for ch in ("/", "\\", ":"):
        project_id = project_id.replace(ch, "_")
    return project_id


# ---------------------------------------------------------------------------
# Private adapter
# ---------------------------------------------------------------------------


class _PyJianYingDraftAdapter:
    """Thin wrapper around pyJianYingDraft.ScriptFile.

    Phase 1 only — may be replaced by an internal writer later.
    Encapsulates the library calls so future swap is just replacing this class.
    """

    def __init__(self, width: int, height: int, fps: int = 30) -> None:
        try:
            import pyJianYingDraft as _pjy  # noqa: PLC0415
        except ImportError as exc:
            raise JianyingEngineUnavailable(
                "pyJianYingDraft is not installed. "
                "Install with: pip install pyJianYingDraft"
            ) from exc
        self._pjy = _pjy
        self._width = width
        self._height = height
        self._fps = fps
        self._script: _pjy.ScriptFile | None = None  # type: ignore[name-defined]
        self._draft_folder_path: str | None = None
        self._draft_name: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def prepare(self, draft_folder_path: str, draft_name: str) -> None:
        """Create (or replace) the draft folder and initialise ScriptFile."""
        pjy = self._pjy
        os.makedirs(draft_folder_path, exist_ok=True)
        folder = pjy.DraftFolder(draft_folder_path)
        self._script = folder.create_draft(
            draft_name=draft_name,
            width=self._width,
            height=self._height,
            fps=self._fps,
            allow_replace=True,  # idempotent: overwrites previous draft
        )
        self._draft_folder_path = draft_folder_path
        self._draft_name = draft_name

    # ------------------------------------------------------------------
    # Track helpers
    # ------------------------------------------------------------------

    def add_video(self, path: str, track_name: str = "video_main") -> None:
        """Add a video track with a single segment spanning the full material."""
        pjy = self._pjy
        script = self._script
        assert script is not None, "call prepare() first"

        material = pjy.VideoMaterial(path)
        duration = material.export_json().get("duration", 0)
        if duration <= 0:
            logger.warning("video material duration=0 for %s, skipping", path)
            return

        script.add_track(pjy.TrackType.video, track_name=track_name)
        seg = pjy.VideoSegment(
            material=material,
            target_timerange=pjy.Timerange(0, duration),
        )
        script.add_segment(seg, track_name=track_name)
        logger.debug("added video track %r, duration=%.2fs", track_name, duration / pjy.SEC)

    def add_audio(
        self,
        path: str,
        track_name: str,
        volume: float = 1.0,
    ) -> None:
        """Add an audio track with a single segment spanning the full material."""
        pjy = self._pjy
        script = self._script
        assert script is not None, "call prepare() first"

        material = pjy.AudioMaterial(path)
        duration = material.export_json().get("duration", 0)
        if duration <= 0:
            logger.warning("audio material duration=0 for %s, skipping", path)
            return

        script.add_track(pjy.TrackType.audio, track_name=track_name)
        seg = pjy.AudioSegment(
            material=material,
            target_timerange=pjy.Timerange(0, duration),
            volume=volume,
        )
        script.add_segment(seg, track_name=track_name)
        logger.debug(
            "added audio track %r, duration=%.2fs, volume=%.2f",
            track_name,
            duration / pjy.SEC,
            volume,
        )

    def import_srt(self, srt_path: str, track_name: str = "zh_subtitle") -> None:
        """Add a text track and import SRT cues into it."""
        pjy = self._pjy
        script = self._script
        assert script is not None, "call prepare() first"

        script.add_track(pjy.TrackType.text, track_name=track_name)
        script.import_srt(srt_path, track_name=track_name)
        logger.debug("imported SRT into track %r", track_name)

    def save(self, draft_dir: str, draft_name: str) -> tuple[str, str]:
        """Save draft to disk.

        Returns (draft_content_path, draft_meta_info_path) as absolute strings.
        """
        script = self._script
        assert script is not None, "call prepare() first"
        script.save()

        content_path = os.path.join(draft_dir, "draft_content.json")
        meta_path = os.path.join(draft_dir, "draft_meta_info.json")
        return content_path, meta_path


# ---------------------------------------------------------------------------
# Public writer
# ---------------------------------------------------------------------------


class JianyingDraftWriter:
    """Generates a Jianying-openable draft directory + zip from a
    JianyingDraftRequest.

    Material files are copied into <draft>/materials/ so the zip is
    self-contained and portable.
    """

    def __init__(self, *, fps: int = 30) -> None:
        self._fps = fps

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def write(self, request: JianyingDraftRequest) -> JianyingDraftResult:
        """Generate the draft directory and zip.

        Raises:
            JianyingEngineUnavailable: if pyJianYingDraft is not installed.
            FileNotFoundError: if request.subtitle_path does not exist.
            Any other exception from pyJianYingDraft or file I/O propagates
            unchanged; the backend (J4) translates it to validation_status='failed'.
        """
        # --- 1. Resolve directories ---
        draft_name = _sanitize_draft_name(request.project_id)
        draft_parent = os.path.join(request.output_dir, "jianying", "draft")
        draft_dir = os.path.join(draft_parent, draft_name)
        materials_dir = os.path.join(draft_dir, "materials")
        exports_dir = os.path.join(request.output_dir, "jianying", "exports")

        # --- 2. Create exports dir; draft_parent is created by adapter.prepare() ---
        os.makedirs(exports_dir, exist_ok=True)

        # --- 3. Validate subtitle (fatal if missing) ---
        if not os.path.isfile(request.subtitle_path):
            raise FileNotFoundError(
                f"subtitle file not found: {request.subtitle_path!r}. "
                "Subtitle is the core deliverable — cannot generate draft without it."
            )

        # --- 4. Build adapter first (prepare() removes + recreates draft_dir,
        #         so materials must be copied AFTER this call) ---
        adapter = _PyJianYingDraftAdapter(
            width=request.width,
            height=request.height,
            fps=self._fps,
        )
        adapter.prepare(draft_parent, draft_name)

        # --- 5. Create materials dir and copy materials ---
        # Materials dir is inside draft_dir which was just freshly created.
        os.makedirs(materials_dir, exist_ok=True)

        video_dest: str | None = self._copy_material(
            request.source_video_path, materials_dir, "source_video"
        )
        audio_dest: str | None = self._copy_material(
            request.dubbed_audio_path, materials_dir, "dubbed_audio"
        )
        ambient_dest: str | None = None
        if request.ambient_audio_path:
            ambient_dest = self._copy_material(
                request.ambient_audio_path, materials_dir, "ambient_audio"
            )

        if video_dest is not None:
            try:
                adapter.add_video(video_dest, track_name="video_main")
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not add video track: %s", exc)

        if audio_dest is not None:
            try:
                adapter.add_audio(audio_dest, track_name="dubbed_audio", volume=1.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not add dubbed_audio track: %s", exc)

        if ambient_dest is not None:
            try:
                adapter.add_audio(ambient_dest, track_name="ambient", volume=0.3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not add ambient track: %s", exc)

        # SRT path is NOT copied to materials/ — pyJianYingDraft inlines cues
        # into draft_content.json at import time; the file is not referenced afterward.
        adapter.import_srt(request.subtitle_path, track_name="zh_subtitle")

        # --- 6. Save draft ---
        draft_content_path, draft_meta_info_path = adapter.save(draft_dir, draft_name)

        # --- 7. Post-process draft_content.json: replace absolute material paths
        #        with relative paths starting with "materials/" ---
        self._make_material_paths_relative(draft_content_path, materials_dir)

        # --- 7b. Post-process draft_content.json: ensure video material has
        #        local_material_id and media_path set so Jianying treats it as
        #        a local file. pyJianYingDraft 0.2.6 leaves these empty for
        #        VideoMaterial (causes "素材下载失败" in Jianying), but
        #        populates them for AudioMaterial. ---
        self._ensure_video_local_material_fields(draft_content_path)

        # --- 8. Post-process draft_meta_info.json: fill draft_name / draft_root_path ---
        self._fill_meta_info(draft_meta_info_path, draft_name, draft_dir)

        # --- 9. Build portable zip ---
        zip_path = os.path.join(
            exports_dir,
            f"jianying_draft_{draft_name}.zip",
        )
        self._build_zip(draft_dir, zip_path)

        return JianyingDraftResult(
            draft_dir=draft_dir,
            draft_zip_path=zip_path,
            draft_content_path=draft_content_path,
            draft_meta_info_path=draft_meta_info_path,
            manifest_path=None,
            compatibility_report_path="",
            validation_status="ok",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _copy_material(
        src_path: str,
        materials_dir: str,
        stem: str,
    ) -> str | None:
        """Copy *src_path* into *materials_dir* as <stem><original_ext>.

        Returns the destination path on success, or None if src_path does not
        exist (caller decides whether that's fatal).
        """
        if not os.path.isfile(src_path):
            logger.warning("material file not found, skipping: %s", src_path)
            return None

        suffix = Path(src_path).suffix  # e.g. ".mp4", ".wav"
        dest_name = f"{stem}{suffix}"
        dest_path = os.path.join(materials_dir, dest_name)
        shutil.copy2(src_path, dest_path)
        logger.debug("copied %s -> %s", src_path, dest_path)
        return dest_path

    @staticmethod
    def _make_material_paths_relative(
        draft_content_path: str,
        materials_dir: str,
    ) -> None:
        """Walk draft_content.json and replace absolute material paths with
        relative paths starting with 'materials/'.

        Only dict entries where key == 'path' and value starts with the absolute
        materials_dir prefix are rewritten.
        """
        content_text = Path(draft_content_path).read_text(encoding="utf-8")
        data = json.loads(content_text)

        # Normalise to forward slashes for comparison
        mat_prefix = materials_dir.replace("\\", "/")
        # Also accept the trailing separator stripped
        mat_prefix_no_slash = mat_prefix.rstrip("/")

        changed = False

        def _walk(obj: object) -> object:
            nonlocal changed
            if isinstance(obj, dict):
                for key in list(obj.keys()):
                    if key == "path" and isinstance(obj[key], str):
                        val_norm = obj[key].replace("\\", "/")
                        if val_norm.startswith(mat_prefix_no_slash + "/") or \
                                val_norm == mat_prefix_no_slash:
                            # Extract filename and build relative form
                            filename = os.path.basename(obj[key])
                            obj[key] = f"materials/{filename}"
                            changed = True
                    else:
                        _walk(obj[key])
                return obj
            if isinstance(obj, list):
                for item in obj:
                    _walk(item)
            return obj

        _walk(data)

        if changed:
            # Atomic-ish: write to a temp file, then rename
            tmp_path = draft_content_path + ".tmp"
            Path(tmp_path).write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, draft_content_path)
            logger.debug(
                "post-processed draft_content.json: absolute material paths -> relative"
            )

    @staticmethod
    def _ensure_video_local_material_fields(draft_content_path: str) -> None:
        """Ensure each video material has local_material_id and media_path
        populated. Jianying uses these fields to determine whether a material
        is local; an empty local_material_id makes it treat the material as
        a remote download, which surfaces in the UI as "素材下载失败,点击重试".

        pyJianYingDraft 0.2.6 fills these for AudioMaterial but not for
        VideoMaterial — likely an oversight in the library. Without this
        post-process patch the video track is unusable in the generated draft.
        """
        content_text = Path(draft_content_path).read_text(encoding="utf-8")
        data = json.loads(content_text)

        changed = False
        for video in data.get("materials", {}).get("videos", []):
            if not isinstance(video, dict):
                continue
            material_id = video.get("material_id") or video.get("id")
            if material_id and not video.get("local_material_id"):
                video["local_material_id"] = material_id
                changed = True
            path = video.get("path", "") or ""
            if path and not video.get("media_path"):
                video["media_path"] = path
                changed = True

        if changed:
            tmp_path = draft_content_path + ".tmp"
            Path(tmp_path).write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, draft_content_path)
            logger.debug(
                "post-processed draft_content.json: filled video local_material_id / media_path"
            )

    @staticmethod
    def _fill_meta_info(
        draft_meta_info_path: str,
        draft_name: str,
        draft_dir: str,
    ) -> None:
        """Fill draft_name and draft_root_path in draft_meta_info.json."""
        meta_text = Path(draft_meta_info_path).read_text(encoding="utf-8")
        meta = json.loads(meta_text)

        meta["draft_name"] = draft_name
        meta["draft_root_path"] = draft_dir

        tmp_path = draft_meta_info_path + ".tmp"
        Path(tmp_path).write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, draft_meta_info_path)
        logger.debug("updated draft_meta_info.json: draft_name=%r", draft_name)

    @staticmethod
    def _build_zip(draft_dir: str, zip_path: str) -> None:
        """Create a portable zip containing all files in *draft_dir*.

        The zip contains entries relative to the draft folder root, e.g.:
            draft_content.json
            draft_meta_info.json
            materials/dubbed_audio.wav
        so that the user can unzip to any location and open it as a Jianying draft.
        """
        draft_path = Path(draft_dir)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in draft_path.rglob("*"):
                if file_path.is_file():
                    # arcname = path relative to the draft dir (no leading dir)
                    arcname = file_path.relative_to(draft_path)
                    zf.write(str(file_path), str(arcname))
        logger.debug(
            "built zip: %s (%d bytes)",
            zip_path,
            os.path.getsize(zip_path),
        )

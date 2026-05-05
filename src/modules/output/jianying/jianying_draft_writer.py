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

import datetime as _dt
import json
import logging
import os
import re
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
# Friendly zip-name composition (2026-05-04)
# ---------------------------------------------------------------------------
#
# Old scheme: ``jianying_draft_{project_id}.zip`` → user sees a 36-char hex blob.
# New scheme: ``{title}_{date}.zip`` → uses ``request.project_title`` (which the
# runner pre-populates from ``job.display_name or job.job_id``).
#
# This is purely a rename of the user-facing zip + the folder name Windows'
# built-in unzip target produces. The ``draft_name`` passed to pyJianYingDraft
# (its internal project identifier on the user's filesystem after import)
# stays as the project_id — pyJianYingDraft 0.2.6's Unicode handling for that
# field is unverified, so we keep blast radius small.

# Windows-illegal: < > : " / \ | ? * + control chars (0x00-0x1F, 0x7F).
# Linux-illegal: NUL only (covered by control-char range). Other chars
# (incl. CJK, ASCII spaces, parens, brackets, ASCII punctuation) are kept.
_FILENAME_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

# Unicode char-count budget. CJK in UTF-8 is up to 4 bytes/char; 80 chars =
# 320 bytes, well under the 255-byte filename limit and Windows 260-char path.
# (The whole zip filename adds the date suffix `_YYYY-MM-DD.zip` = 15 chars,
# so the basename total stays ≤ 95 chars + extension.)
_NAME_MAX_CHARS = 80


def _sanitize_zip_basename(name: str) -> str:
    """Sanitize a user-facing string for use inside a zip filename.

    Steps:
      1. Strip Windows-illegal chars + control chars (replace with empty).
      2. Strip leading/trailing whitespace and dots (Windows trips on trailing
         dots; both platforms hate leading whitespace).
      3. Cap to ``_NAME_MAX_CHARS`` characters.

    Spaces inside the name are PRESERVED (per UX choice 2026-05-04: feels more
    natural for human-language titles, both Windows and Linux accept them).

    Returns ``""`` if nothing usable remains — callers should fall through to
    a backup name source.
    """
    if not name:
        return ""
    cleaned = _FILENAME_ILLEGAL_RE.sub("", name)
    cleaned = cleaned.strip(" .\t\r\n")
    if len(cleaned) > _NAME_MAX_CHARS:
        cleaned = cleaned[:_NAME_MAX_CHARS].rstrip(" .")
    return cleaned


def _resolve_zip_basename(
    *,
    project_title: str | None,
    project_id: str,
    today_utc: _dt.date | None = None,
) -> str:
    """Compose the zip basename ``{name}_{YYYY-MM-DD}`` with sanitization.

    Name resolution priority:
      1. ``project_title`` — runner pre-populates this from
         ``job.display_name or job.job_id``. The display_name is what the
         user sees in the workspace list (e.g. the Chinese title set via the
         pencil-edit icon); falling through to job_id is a defensive bottom.
      2. ``project_id`` — last resort if project_title sanitizes to empty
         (e.g. user title was nothing but illegal chars). Should be very rare.
      3. The literal string ``"draft"`` — paranoia fallback for the case
         where even project_id is somehow empty.

    Date is UTC ``YYYY-MM-DD`` (the server runs UTC; the date label is for
    human reference, not strict scheduling, so dropping the time is fine).

    Caller must append ``.zip`` and is responsible for collision suffixes.
    """
    name = _sanitize_zip_basename(project_title or "")
    if not name:
        name = _sanitize_zip_basename(project_id)
    if not name:
        name = "draft"
    if today_utc is None:
        today_utc = _dt.datetime.now(_dt.timezone.utc).date()
    return f"{name}_{today_utc.strftime('%Y-%m-%d')}"


def _resolve_zip_path_with_collision(exports_dir: str, basename: str) -> str:
    """Append ``_2``, ``_3``... if ``{basename}.zip`` already exists.

    Caller writes the returned path. Same-day re-publish (e.g. Studio
    edit→commit overwrite) won't clobber a previous zip the user may still
    be downloading. Hard cap at 999 to prevent runaway loops on edge cases.
    """
    candidate = os.path.join(exports_dir, f"{basename}.zip")
    if not os.path.exists(candidate):
        return candidate
    for counter in range(2, 1000):
        candidate = os.path.join(exports_dir, f"{basename}_{counter}.zip")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        f"could not find a non-colliding zip name for {basename!r} "
        f"under {exports_dir!r} after 999 attempts"
    )


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

        # --- 7. Resolve user-friendly zip basename (2026-05-04 rename) ---
        # Composed once here so the same value drives:
        #   (a) the zip filename in exports_dir
        #   (b) the unzip-target folder prefix in absolute-path JSON rewriting
        # Windows' built-in unzip names the extracted folder after the zip
        # stem, so JSON paths must agree with the zip basename or Jianying
        # reports missing media (the K11 invariant).
        zip_basename = _resolve_zip_basename(
            project_title=request.project_title,
            project_id=request.project_id,
        )
        zip_path = _resolve_zip_path_with_collision(exports_dir, zip_basename)
        # Final stem actually used (collision counter may have bumped it):
        unzip_folder_name = Path(zip_path).stem

        # --- 7a. Post-process draft_content.json: embed absolute or relative paths
        #         depending on whether user_draft_root was supplied. ---
        if request.user_draft_root:
            self._make_material_paths_absolute(
                draft_content_path,
                request.user_draft_root,
                unzip_folder_name,
            )
        else:
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
    def _make_material_paths_absolute(
        draft_content_path: str,
        user_draft_root: str,
        unzip_folder_name: str,
    ) -> None:
        """Rewrite every materials.{videos,audios}[*].path to an absolute path under
        the user's local drafts root. Format:
          {user_draft_root}/{unzip_folder_name}/materials/{filename}

        ``unzip_folder_name`` MUST equal the zip stem the writer just chose —
        Windows' built-in unzip creates a folder named after the zip file,
        and Jianying reads back these absolute paths verbatim. Mismatch =
        "素材下载失败,点击重试" in the Jianying UI.

        Pre-2026-05-04 contract: the writer hard-coded ``jianying_draft_{draft_name}``
        as both the zip stem and the folder prefix here. The 2026-05-04 rename
        switched the zip basename to ``{title}_{date}`` (with collision suffix)
        and now the writer passes the resolved stem in directly — no prefix
        synthesis happens inside this function any more.

        Respects the path separator style of user_draft_root (Windows backslash
        vs Unix forward-slash).

        Also rewrites video.media_path to match (Jianying uses both).
        """
        sep = "\\" if "\\" in user_draft_root else "/"
        # Strip trailing separator from user_draft_root
        root = user_draft_root.rstrip("\\/")
        base = sep.join([root, unzip_folder_name, "materials"])

        content_text = Path(draft_content_path).read_text(encoding="utf-8")
        data = json.loads(content_text)
        changed = False

        for kind in ("videos", "audios"):
            for m in data.get("materials", {}).get(kind, []):
                if not isinstance(m, dict):
                    continue
                current = m.get("path", "")
                if not current:
                    continue
                # Only rewrite if it's a relative materials/ path or some other path;
                # extract the filename and rebuild as absolute.
                current_norm = current.replace("\\", "/")
                if current_norm.startswith("materials/"):
                    filename = current_norm[len("materials/"):]
                else:
                    # Already absolute or some other form; extract basename and rebuild
                    filename = os.path.basename(current_norm)
                new_path = sep.join([base, filename])
                m["path"] = new_path
                if "media_path" in m:
                    m["media_path"] = new_path
                changed = True

        if changed:
            tmp_path = draft_content_path + ".tmp"
            Path(tmp_path).write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, draft_content_path)
            logger.debug(
                "post-processed draft_content.json with absolute material paths "
                "rooted at %s",
                base,
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

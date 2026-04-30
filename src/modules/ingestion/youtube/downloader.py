from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse

import yt_dlp
from services import config_loader


class DownloadError(Exception):
    pass


# --- URL 安全校验 ---

_ALLOWED_DOMAINS: set[str] = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "music.youtube.com",
    "bilibili.com",
    "www.bilibili.com",
}


def validate_video_url(url: str) -> None:
    """校验 URL 协议和域名白名单，防止 SSRF。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DownloadError(
            f"不支持的 URL 协议: {parsed.scheme!r}，仅允许 http/https"
        )
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise DownloadError("URL 缺少域名")
    # 匹配域名本身或其父域
    if not any(
        hostname == d or hostname.endswith("." + d) for d in _ALLOWED_DOMAINS
    ):
        raise DownloadError(
            f"不允许的视频源域名: {hostname}，"
            f"仅支持: {', '.join(sorted(_ALLOWED_DOMAINS))}"
        )


@dataclass(slots=True)
class DownloadRequest:
    url: str
    output_dir: str
    skip_if_exists: bool = True
    cookies_from_browser: str | None = None
    cookie_file: str | None = None
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5


@dataclass(slots=True)
class DownloadResult:
    video_path: str
    audio_path: str
    video_title: str
    duration_ms: int
    url: str
    description: str = ""
    language: str = ""


def load_youtube_download_config() -> dict[str, object]:
    local_config = config_loader.load_project_local_config()
    cookies_from_browser, _ = config_loader.resolve_text_value(
        config=local_config,
        config_key_paths=(("youtube", "cookies_from_browser"),),
    )
    cookie_file, _ = config_loader.resolve_path_value(
        config=local_config,
        config_key_paths=(("youtube", "cookie_file"),),
    )
    max_retries, _ = config_loader.resolve_int_value(
        config=local_config,
        config_key_paths=(("youtube", "max_retries"),),
        default=2,
    )
    retry_backoff_seconds, _ = config_loader.resolve_float_value(
        config=local_config,
        config_key_paths=(("youtube", "retry_backoff_seconds"),),
        default=1.5,
    )
    return {
        "cookies_from_browser": cookies_from_browser,
        "cookie_file": cookie_file,
        "max_retries": max(0, max_retries),
        "retry_backoff_seconds": max(0.0, retry_backoff_seconds),
    }


class YouTubeDownloader:
    metadata_filename: str = "download_metadata.json"

    def download(self, request: DownloadRequest) -> DownloadResult:
        validate_video_url(request.url)
        output_root = Path(request.output_dir).resolve(strict=False)
        video_dir = output_root / "video"
        audio_dir = output_root / "audio"
        video_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)

        video_path = (video_dir / "original.mp4").resolve(strict=False)
        audio_path = (audio_dir / "original.wav").resolve(strict=False)
        metadata_path = self._metadata_path(output_root)

        if request.skip_if_exists and video_path.exists() and audio_path.exists():
            return self._read_cached_result(
                metadata_path=metadata_path,
                url=request.url,
                video_path=str(video_path),
                audio_path=str(audio_path),
            )

        if not metadata_path.exists():
            self._write_metadata(
                metadata_path,
                DownloadResult(
                    video_path=str(video_path),
                    audio_path=str(audio_path),
                    video_title=request.url,
                    duration_ms=0,
                    url=request.url,
                    description="",
                ),
            )

        (
            downloaded_video_path,
            video_title,
            duration_ms,
            description,
            language,
        ) = self._download_video(
            request.url,
            str(output_root),
            cookies_from_browser=request.cookies_from_browser,
            cookie_file=request.cookie_file,
            max_retries=max(0, int(request.max_retries)),
            retry_backoff_seconds=max(0.0, float(request.retry_backoff_seconds)),
        )
        print("[S0] 下载完成，准备提取音频...")
        extracted_audio_path = self._extract_audio(downloaded_video_path, str(output_root))
        result = DownloadResult(
            video_path=downloaded_video_path,
            audio_path=extracted_audio_path,
            video_title=video_title,
            duration_ms=duration_ms,
            url=request.url,
            description=description,
            language=language,
        )
        self._write_metadata(metadata_path, result)
        return result

    def _download_video(
        self,
        url: str,
        output_dir: str,
        *,
        cookies_from_browser: str | None = None,
        cookie_file: str | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
    ) -> tuple[str, str, int, str, str]:
        output_root = Path(output_dir).resolve(strict=False)
        video_dir = output_root / "video"
        video_dir.mkdir(parents=True, exist_ok=True)

        base_ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(video_dir / "original.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "nopart": False,
        }

        attempts = self._build_download_attempts(
            base_ydl_opts,
            cookies_from_browser=cookies_from_browser,
            cookie_file=cookie_file,
        )
        info: dict[str, object] | None = None
        errors: list[tuple[str, Exception]] = []
        total_rounds = max_retries + 1
        for retry_index in range(total_rounds):
            round_errors: list[tuple[str, Exception]] = []
            for attempt_label, ydl_opts in attempts:
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    break
                except Exception as exc:  # pragma: no cover - exercised via tests with mocked failures
                    round_errors.append((attempt_label, exc))
                    continue
            errors.extend(round_errors)
            if info is not None:
                break
            if retry_index >= max_retries or not self._should_retry_download(round_errors):
                break
            sleep_seconds = retry_backoff_seconds * (2**retry_index)
            print(
                "[S0] 下载中断，准备断点续传重试 "
                f"{retry_index + 2}/{total_rounds}"
                + (f"，{sleep_seconds:.1f}s 后继续..." if sleep_seconds > 0 else "...")
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        if info is None:
            raise DownloadError(self._format_download_failure(url, errors)) from errors[-1][1]

        if not isinstance(info, dict):
            raise DownloadError(f"yt-dlp returned invalid metadata for: {url}")

        video_path = self._resolve_downloaded_video_path(video_dir)
        video_title = str(info.get("title") or url)
        duration_ms = self._duration_to_ms(info.get("duration"))
        description = str(info.get("description") or "")
        language = str(info.get("language") or "").strip()
        return (
            str(video_path.resolve(strict=False)),
            video_title,
            duration_ms,
            description,
            language,
        )

    def _build_download_attempts(
        self,
        base_ydl_opts: dict[str, object],
        *,
        cookies_from_browser: str | None,
        cookie_file: str | None,
    ) -> list[tuple[str, dict[str, object]]]:
        attempts: list[tuple[str, dict[str, object]]] = []
        normalized_cookie_file = str(cookie_file or "").strip()
        normalized_browser = str(cookies_from_browser or "").strip()

        if normalized_cookie_file:
            cookie_path = Path(normalized_cookie_file).expanduser().resolve(strict=False)
            attempts.append(
                (
                    f"cookie_file:{cookie_path}",
                    {
                        **base_ydl_opts,
                        "cookiefile": str(cookie_path),
                    },
                )
            )

        if normalized_browser:
            attempts.append(
                (
                    f"cookies_from_browser:{normalized_browser}",
                    {
                        **base_ydl_opts,
                        "cookiesfrombrowser": (normalized_browser,),
                    },
                )
            )

        attempts.append(("anonymous", dict(base_ydl_opts)))
        return attempts

    def _should_retry_download(self, errors: list[tuple[str, Exception]]) -> bool:
        if not errors:
            return False

        lowered = " | ".join(f"{label}: {exc}" for label, exc in errors).lower()
        non_retryable_markers = (
            "could not copy chrome cookie database",
            "sign in to confirm you're not a bot",
            "unsupported url",
            "unsupported url scheme",
            "invalid url",
        )
        if any(marker in lowered for marker in non_retryable_markers):
            return False

        retryable_markers = (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "remote end closed connection",
            "temporarily unavailable",
            "network is unreachable",
            "network down",
            "429",
            "500",
            "502",
            "503",
            "504",
            "try again",
        )
        if any(marker in lowered for marker in retryable_markers):
            return True
        return True

    def _format_download_failure(
        self,
        url: str,
        errors: list[tuple[str, Exception]],
    ) -> str:
        if not errors:
            return f"Failed to download YouTube video: {url}"

        messages = [f"{label}: {exc}" for label, exc in errors]
        joined = " | ".join(messages)
        lowered = joined.lower()

        if "could not copy chrome cookie database" in lowered:
            return (
                f"Failed to download YouTube video: {url}. "
                "Chrome cookies could not be copied. Close Chrome completely or configure "
                "youtube.cookie_file with an exported cookies.txt file."
            )
        if "sign in to confirm you're not a bot" in lowered:
            return (
                f"Failed to download YouTube video: {url}. "
                "YouTube requested authenticated cookies. Configure youtube.cookie_file or "
                "set youtube.cookies_from_browser to a browser session that is signed in."
            )
        return f"Failed to download YouTube video: {url}"

    def _extract_audio(self, video_path: str, output_dir: str) -> str:
        output_root = Path(output_dir).resolve(strict=False)
        audio_dir = output_root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        audio_path = audio_dir / "original.wav"
        print("[S0] 正在提取音频...")
        command = [
            "ffmpeg",
            "-i",
            str(Path(video_path).resolve(strict=False)),
            "-ac",
            "2",
            "-ar",
            "44100",
            "-sample_fmt",
            "s16",
            str(audio_path),
            "-y",
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DownloadError("ffmpeg was not found in PATH. Please install ffmpeg.") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise DownloadError(
                "ffmpeg failed to extract audio."
                + (f" stderr: {stderr}" if stderr else "")
            )

        if not audio_path.exists():
            raise DownloadError("ffmpeg reported success but audio/original.wav was not created.")
        print(f"[S0] 音频提取完成：{audio_path.resolve(strict=False)}")
        return str(audio_path.resolve(strict=False))

    def _metadata_path(self, output_root: Path) -> Path:
        return output_root / self.metadata_filename

    def _write_metadata(self, metadata_path: Path, result: DownloadResult) -> None:
        metadata_path.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_cached_result(
        self,
        *,
        metadata_path: Path,
        url: str,
        video_path: str,
        audio_path: str,
    ) -> DownloadResult:
        if metadata_path.exists():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DownloadError(f"Cached download metadata is invalid: {metadata_path}") from exc
            return DownloadResult(
                video_path=video_path,
                audio_path=audio_path,
                video_title=str(payload.get("video_title") or url),
                duration_ms=self._duration_to_ms(payload.get("duration_ms"), treat_as_seconds=False),
                url=str(payload.get("url") or url),
                description=str(payload.get("description") or ""),
                language=str(payload.get("language") or ""),
            )

        return DownloadResult(
            video_path=video_path,
            audio_path=audio_path,
            video_title=url,
            duration_ms=0,
            url=url,
            description="",
        )

    def _resolve_downloaded_video_path(self, video_dir: Path) -> Path:
        mp4_path = video_dir / "original.mp4"
        if mp4_path.exists():
            return mp4_path

        candidates = sorted(
            path
            for path in video_dir.glob("original.*")
            if path.is_file() and path.suffix.lower() not in {".json", ".part", ".ytdl"}
        )
        if candidates:
            return candidates[0]
        raise DownloadError(f"yt-dlp finished without producing a video file in {video_dir}")

    def _duration_to_ms(self, raw_duration: object, *, treat_as_seconds: bool = True) -> int:
        if raw_duration is None:
            return 0
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            return 0
        if treat_as_seconds:
            return int(duration * 1000)
        return int(duration)

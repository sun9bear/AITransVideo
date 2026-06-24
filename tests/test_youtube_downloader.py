from pathlib import Path
import json
from types import SimpleNamespace

import pytest

import modules.ingestion.youtube.downloader as downloader_module
from modules.ingestion.youtube.downloader import (
    DownloadError,
    DownloadRequest,
    DownloadResult,
    YouTubeDownloader,
)


def _write_file(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_youtube_downloader_downloads_video_and_extracts_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            calls["options"] = options

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            calls["url"] = url
            calls["download"] = download
            output_template = str(calls["options"]["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"mp4")
            return {
                "title": "Demo Video",
                "duration": 12,
                "description": "Full demo description from YouTube.",
            }

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        calls["ffmpeg_command"] = command
        assert command[0] == "ffmpeg"
        assert capture_output is True
        assert text is True
        assert check is False
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    result = YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=demo",
            output_dir=str(tmp_path / "project"),
        )
    )

    assert isinstance(result, DownloadResult)
    assert Path(result.video_path).name == "original.mp4"
    assert Path(result.audio_path).name == "original.wav"
    assert result.video_title == "Demo Video"
    assert result.duration_ms == 12_000
    assert result.url == "https://www.youtube.com/watch?v=demo"
    assert result.description == "Full demo description from YouTube."
    assert calls["download"] is True
    assert calls["options"]["continuedl"] is True
    assert calls["options"]["nopart"] is False
    assert calls["ffmpeg_command"][calls["ffmpeg_command"].index("-ac") + 1] == "2"
    assert calls["ffmpeg_command"][calls["ffmpeg_command"].index("-ar") + 1] == "44100"
    metadata = json.loads((tmp_path / "project" / "download_metadata.json").read_text(encoding="utf-8"))
    assert metadata["description"] == "Full demo description from YouTube."


def test_youtube_downloader_passes_browser_cookie_option_to_ytdlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            calls["options"] = options

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            output_template = str(calls["options"]["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"mp4")
            return {"title": "Cookie Video", "duration": 8}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=cookie-demo",
            output_dir=str(tmp_path / "project"),
            cookies_from_browser="chrome",
        )
    )

    assert calls["options"]["cookiesfrombrowser"] == ("chrome",)


def test_youtube_downloader_prefers_cookie_file_before_browser_cookies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    cookie_file = _write_file(tmp_path / "cookies.txt", b"cookies")

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            calls.append(options)

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            output_template = str(calls[-1]["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"mp4")
            return {"title": "Cookie File Video", "duration": 6}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=cookie-file-demo",
            output_dir=str(tmp_path / "project"),
            cookies_from_browser="chrome",
            cookie_file=str(cookie_file),
        )
    )

    assert str(cookie_file.resolve(strict=False)) == calls[0]["cookiefile"]
    assert "cookiesfrombrowser" not in calls[0]


def test_youtube_downloader_falls_back_to_hls_when_default_stream_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    cookie_file = _write_file(tmp_path / "cookies.txt", b"cookies")
    downloader = YouTubeDownloader()

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options
            calls.append(options)

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            if self.options["format"] == downloader.default_format:
                raise RuntimeError("HTTP Error 403: Forbidden")
            output_template = str(self.options["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"hls mp4")
            return {"title": "HLS Fallback Video", "duration": 11}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    result = downloader.download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=forbidden-stream",
            output_dir=str(tmp_path / "project"),
            cookie_file=str(cookie_file),
            max_retries=0,
        )
    )

    assert result.video_title == "HLS Fallback Video"
    assert result.duration_ms == 11_000
    assert [call["format"] for call in calls] == [
        downloader.default_format,
        downloader.hls_fallback_format,
    ]
    assert all(call["cookiefile"] == str(cookie_file.resolve(strict=False)) for call in calls)


def test_youtube_downloader_falls_back_to_anonymous_when_browser_cookie_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options
            calls.append(options)

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            if "cookiesfrombrowser" in self.options:
                raise RuntimeError("Could not copy Chrome cookie database")
            output_template = str(self.options["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"mp4")
            return {"title": "Anonymous Fallback", "duration": 7}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    result = YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=browser-cookie-fallback",
            output_dir=str(tmp_path / "project"),
            cookies_from_browser="chrome",
        )
    )

    assert result.video_title == "Anonymous Fallback"
    assert len(calls) == 2
    assert calls[0]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiesfrombrowser" not in calls[1]


def test_youtube_downloader_reports_actionable_cookie_error_when_all_attempts_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options

        def __enter__(self) -> "FailingYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            if "cookiesfrombrowser" in self.options:
                raise RuntimeError("Could not copy Chrome cookie database")
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FailingYoutubeDL)

    with pytest.raises(DownloadError, match="youtube.cookie_file"):
        YouTubeDownloader().download(
            DownloadRequest(
                url="https://www.youtube.com/watch?v=needs-cookies",
                output_dir=str(tmp_path / "project"),
                cookies_from_browser="chrome",
            )
        )


def test_youtube_downloader_uses_cached_files_when_skip_if_exists_is_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    video_path = _write_file(project_dir / "video" / "original.mp4", b"cached video")
    audio_path = _write_file(project_dir / "audio" / "original.wav", b"cached audio")
    downloader = YouTubeDownloader()
    downloader._write_metadata(
        downloader._metadata_path(project_dir),
        DownloadResult(
            video_path=str(video_path.resolve(strict=False)),
            audio_path=str(audio_path.resolve(strict=False)),
            video_title="Cached Video",
            duration_ms=45_000,
            url="https://www.youtube.com/watch?v=cached",
            description="Cached description",
        ),
    )

    def unexpected_ytdlp(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("yt-dlp should not be called when cache is valid")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", unexpected_ytdlp)

    result = downloader.download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=cached",
            output_dir=str(project_dir),
            skip_if_exists=True,
        )
    )

    assert result.video_title == "Cached Video"
    assert result.duration_ms == 45_000
    assert result.video_path == str(video_path.resolve(strict=False))
    assert result.audio_path == str(audio_path.resolve(strict=False))
    assert result.description == "Cached description"


def test_youtube_downloader_raises_download_error_when_ytdlp_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            del options

        def __enter__(self) -> "FailingYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            raise RuntimeError("network down")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FailingYoutubeDL)

    with pytest.raises(DownloadError, match="Failed to download YouTube video"):
        YouTubeDownloader().download(
            DownloadRequest(
                url="https://www.youtube.com/watch?v=broken",
                output_dir=str(tmp_path / "project"),
            )
        )

    metadata = json.loads((tmp_path / "project" / "download_metadata.json").read_text(encoding="utf-8"))
    assert metadata["url"] == "https://www.youtube.com/watch?v=broken"
    assert Path(metadata["video_path"]).name == "original.mp4"
    assert Path(metadata["audio_path"]).name == "original.wav"


def test_youtube_downloader_retries_network_failure_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"extract_info": 0, "sleep": []}

    class FlakyYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options

        def __enter__(self) -> "FlakyYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            calls["extract_info"] += 1
            if calls["extract_info"] == 1:
                raise RuntimeError("network down")
            output_template = str(self.options["outtmpl"])
            _write_file(Path(output_template.replace("%(ext)s", "mp4")), b"mp4")
            return {"title": "Retried Video", "duration": 9}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"wav")
        return SimpleNamespace(returncode=0, stderr="")

    def fake_sleep(seconds: float) -> None:
        calls["sleep"].append(seconds)

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FlakyYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)
    monkeypatch.setattr(downloader_module.time, "sleep", fake_sleep)

    result = YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=retry-me",
            output_dir=str(tmp_path / "project"),
            max_retries=2,
            retry_backoff_seconds=1.25,
        )
    )

    assert result.video_title == "Retried Video"
    assert calls["extract_info"] == 2
    assert calls["sleep"] == [1.25]


def test_youtube_downloader_reports_missing_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_path = _write_file(tmp_path / "project" / "video" / "original.mp4", b"video")

    def missing_ffmpeg(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(downloader_module.subprocess, "run", missing_ffmpeg)

    with pytest.raises(DownloadError, match="Please install ffmpeg"):
        YouTubeDownloader()._extract_audio(str(video_path), str(tmp_path / "project"))


def test_youtube_downloader_surfaces_ffmpeg_stderr_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_path = _write_file(tmp_path / "project" / "video" / "original.mp4", b"video")

    def failed_ffmpeg(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(returncode=1, stderr="conversion failed")

    monkeypatch.setattr(downloader_module.subprocess, "run", failed_ffmpeg)

    with pytest.raises(DownloadError, match="conversion failed"):
        YouTubeDownloader()._extract_audio(str(video_path), str(tmp_path / "project"))


def test_youtube_downloader_redownloads_when_skip_if_exists_is_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"extract_info": 0}
    project_dir = tmp_path / "project"
    _write_file(project_dir / "video" / "original.mp4", b"old video")
    _write_file(project_dir / "audio" / "original.wav", b"old audio")

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            self.options = options

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def extract_info(self, url: str, download: bool) -> dict[str, object]:
            del url, download
            calls["extract_info"] += 1
            _write_file(Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp4")), b"new video")
            return {"title": "Redownloaded", "duration": 5}

    def fake_run(command: list[str], capture_output: bool, text: bool, check: bool) -> SimpleNamespace:
        del capture_output, text, check
        _write_file(Path(command[-2]), b"new audio")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(downloader_module.subprocess, "run", fake_run)

    result = YouTubeDownloader().download(
        DownloadRequest(
            url="https://www.youtube.com/watch?v=refresh",
            output_dir=str(project_dir),
            skip_if_exists=False,
        )
    )

    assert calls["extract_info"] == 1
    assert result.video_title == "Redownloaded"
    assert result.duration_ms == 5_000

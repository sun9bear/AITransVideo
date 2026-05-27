from __future__ import annotations

from src.services.mainland_worker.worker.providers import real_cosyvoice


class _FakeResponse:
    def __init__(self, *, status_code: int = 206, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"x"


def test_worker_sample_size_probe_uses_range_get_not_head(monkeypatch) -> None:
    """Aliyun OSS GET presigned URLs reject HEAD; worker must probe with GET."""
    calls: list[tuple[str, str, dict[str, str] | None]] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url: str, *, headers: dict[str, str] | None = None):
            calls.append(("GET", url, headers))
            return _FakeResponse(headers={"content-range": "bytes 0-0/1024"})

        def head(self, url: str):  # pragma: no cover - should never be called
            raise AssertionError("HEAD must not be used for OSS GET presigned URLs")

    monkeypatch.setattr(real_cosyvoice.httpx, "Client", FakeClient)

    provider = real_cosyvoice.RealCosyvoiceProvider("dashscope-key")
    provider._validate_sample_size("https://oss.example/presigned")

    assert calls == [
        ("GET", "https://oss.example/presigned", {"Range": "bytes=0-0"})
    ]

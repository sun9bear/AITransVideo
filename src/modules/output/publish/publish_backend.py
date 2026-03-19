from __future__ import annotations

from modules.output.publish.publish_models import PublishRequest, PublishResult
from modules.output.publish.video_renderer import VideoRenderer


class PublishBackend:
    """Minimal publish backend that produces dubbed_video.mp4."""

    def __init__(self, renderer: VideoRenderer | None = None) -> None:
        self.renderer = renderer or VideoRenderer()

    def publish(self, request: PublishRequest) -> PublishResult:
        return self.renderer.render(request)

from __future__ import annotations

from http.server import ThreadingHTTPServer

from .constants import WEB_UI_DEFAULT_HOST, WEB_UI_DEFAULT_PORT, WEB_UI_TITLE
from .utils import _open_browser


def run_web_ui_server(
    *,
    host: str = WEB_UI_DEFAULT_HOST,
    port: int = WEB_UI_DEFAULT_PORT,
) -> None:
    server = create_web_ui_server(host=host, port=port)
    web_ui_url = f"http://{host}:{port}"
    print(f"{WEB_UI_TITLE} \u5df2\u542f\u52a8\uff1a{web_ui_url}")
    print(f"\u914d\u7f6e\u6587\u4ef6\uff1a{server.config_path}")  # type: ignore[attr-defined]

    # Start background cleanup thread for expired projects
    from .cleanup import start_cleanup_thread
    start_cleanup_thread()

    _open_browser(web_ui_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\u6b63\u5728\u505c\u6b62 Web UI\u3002")
    finally:
        server.server_close()


def create_web_ui_server(
    *,
    host: str = WEB_UI_DEFAULT_HOST,
    port: int = WEB_UI_DEFAULT_PORT,
    job_manager: object | None = None,
) -> ThreadingHTTPServer:
    from .handler import _build_web_ui_handler

    handler_class = _build_web_ui_handler()

    if job_manager is None:
        # Late import: JobAPIBackedJobManager lives outside of the 7 extracted
        # submodules (still in the monolithic web_ui.py or a future managers
        # submodule).  Import it at call time to avoid circular deps.
        from services.web_ui import JobAPIBackedJobManager  # type: ignore[attr-defined]
        job_manager = JobAPIBackedJobManager()

    server = ThreadingHTTPServer((host, port), handler_class)
    server.job_manager = job_manager  # type: ignore[attr-defined]
    server.config_path = str(job_manager.config_path)  # type: ignore[attr-defined]
    return server

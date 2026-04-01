from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlparse


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("REMOTE_WORKBENCH_CONFIG_PATH", "/opt/aivideotrans/app/remote_workbench.local.json")
)
DEFAULT_PROJECTS_DIR = Path(os.environ.get("AIVIDEOTRANS_PROJECTS_DIR", "/opt/aivideotrans/app/projects"))
DEFAULT_JOBS_DIR = Path(os.environ.get("AIVIDEOTRANS_JOBS_DIR", "/opt/aivideotrans/app/jobs"))
DEFAULT_RUNTIME_LOGS_DIR = Path(
    os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR", "/opt/aivideotrans/data/runtime_logs")
)
DEFAULT_AUTODUB_CONFIG_PATH = Path(
    os.environ.get("AUTODUB_LOCAL_CONFIG_PATH", "/opt/aivideotrans/app/autodub.local.json")
)
DEFAULT_JOB_API_HOST = "127.0.0.1"
DEFAULT_JOB_API_PORT = 8877
ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/linux_remote_workbench_preflight.py",
        description="Minimal Linux preflight and health checks for the remote-workbench baseline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("app-preflight", help="Validate runtime config and writable paths.")
    _add_common_path_args(preflight_parser)

    health_parser = subparsers.add_parser("app-health", help="Probe local Web UI and Job API listeners.")
    health_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to remote_workbench.local.json")
    health_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for both listeners before failing.",
    )

    args = parser.parse_args(argv)
    if args.command == "app-preflight":
        return run_app_preflight(args)
    return run_app_health(args)


def _add_common_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to remote_workbench.local.json")
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR), help="Path to projects/ bind mount")
    parser.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR), help="Path to jobs/ bind mount")
    parser.add_argument(
        "--runtime-logs-dir",
        default=str(DEFAULT_RUNTIME_LOGS_DIR),
        help="Path to runtime_logs/ bind mount",
    )
    parser.add_argument(
        "--autodub-config",
        default=str(DEFAULT_AUTODUB_CONFIG_PATH),
        help="Path to autodub.local.json (optional during P1).",
    )


def run_app_preflight(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Missing runtime config: {config_path}")

    runtime_config = load_runtime_config(config_path)

    projects_dir = Path(args.projects_dir)
    jobs_dir = Path(args.jobs_dir)
    runtime_logs_dir = Path(args.runtime_logs_dir)
    autodub_config_path = Path(args.autodub_config)

    for directory in (projects_dir, jobs_dir, runtime_logs_dir, runtime_config.runtime_logs_dir):
        _ensure_writable_directory(directory)

    if autodub_config_path.exists():
        print(f"Found autodub config: {autodub_config_path}")
    else:
        print(f"Optional autodub config not found: {autodub_config_path}")

    print(f"Runtime config: {runtime_config.path}")
    print(f"Job API binding: {runtime_config.job_api.base_url}")
    print(f"Projects dir writable: {projects_dir}")
    print(f"Jobs dir writable: {jobs_dir}")
    print(f"Runtime logs dir writable: {runtime_logs_dir}")
    return 0


def run_app_health(args: argparse.Namespace) -> int:
    runtime_config = load_runtime_config(Path(args.config))
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        job_api_ok = _probe_listener(runtime_config.job_api.host, runtime_config.job_api.port)
        if job_api_ok:
            print(
                "App health passed: "
                f"Job API {runtime_config.job_api.base_url}"
            )
            return 0
        time.sleep(0.5)

    raise SystemExit(
        "App health failed: Job API listener was not reachable on "
        f"{runtime_config.job_api.base_url} "
        f"within {args.timeout:.1f}s."
    )


def _ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe_path = path / ".write_test"
    try:
        probe_path.write_text("ok", encoding="utf-8")
    finally:
        if probe_path.exists():
            probe_path.unlink()


def _probe_listener(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class ServiceBinding:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class RuntimeConfig:
    def __init__(
        self,
        *,
        path: Path,
        job_api: ServiceBinding,
        runtime_logs_dir: Path,
    ) -> None:
        self.path = path
        self.job_api = job_api
        self.runtime_logs_dir = runtime_logs_dir


def load_runtime_config(config_path: Path) -> RuntimeConfig:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse runtime config {config_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"Runtime config {config_path} must contain a top-level JSON object.")

    config_root = config_path.parent
    job_api = _load_binding(
        payload.get("job_api"),
        section_name="job_api",
        default_host=DEFAULT_JOB_API_HOST,
        default_port=DEFAULT_JOB_API_PORT,
    )
    runtime_logs_dir = _load_runtime_logs_dir(payload.get("runtime_logs"), config_root=config_root)
    _validate_public_entry_section(payload.get("public_entry"))

    return RuntimeConfig(
        path=config_path,
        job_api=job_api,
        runtime_logs_dir=runtime_logs_dir,
    )


def _load_binding(
    section: object,
    *,
    section_name: str,
    default_host: str,
    default_port: int,
) -> ServiceBinding:
    resolved_section = _coerce_optional_dict(section, section_name)
    host = str(resolved_section.get("host") or default_host).strip()
    port = resolved_section.get("port", default_port)
    if host not in ALLOWED_LOCAL_HOSTS:
        raise SystemExit(f"{section_name}.host must stay on localhost; got {host!r}.")
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        raise SystemExit(f"{section_name}.port must be a positive integer.")
    return ServiceBinding(host=host, port=port)


def _load_runtime_logs_dir(section: object, *, config_root: Path) -> Path:
    resolved_section = _coerce_optional_dict(section, "runtime_logs")
    raw_directory = str(resolved_section.get("directory") or "/opt/aivideotrans/data/runtime_logs").strip()
    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        directory = (config_root / directory).resolve(strict=False)
    return directory


def _validate_public_entry_section(section: object) -> None:
    resolved_section = _coerce_optional_dict(section, "public_entry")
    https_url = str(resolved_section.get("https_url") or "").strip()
    site_host = str(resolved_section.get("site_host") or "").strip()
    if https_url:
        parsed = urlparse(https_url)
        if parsed.scheme and parsed.scheme != "https":
            raise SystemExit("public_entry.https_url must use https when provided.")
    if site_host and urlparse(f"https://{site_host}").hostname is None:
        raise SystemExit("public_entry.site_host must be a valid hostname when provided.")


def _coerce_optional_dict(section: object, section_name: str) -> dict[str, object]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SystemExit(f"{section_name} must be a JSON object when provided.")
    return dict(section)


if __name__ == "__main__":
    raise SystemExit(main())

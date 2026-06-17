from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


from services import config_loader
from services.control_panel import create_control_panel_server
from services.jobs import build_default_job_service, build_job_api_server
from services.public_entry_caddy import (
    CaddyPublicEntryError,
    check_caddy_public_entry,
    run_caddy_public_entry,
)
from services.remote_workbench_runtime import (
    DEFAULT_REMOTE_WORKBENCH_CONFIG_PATH,
    load_remote_workbench_runtime_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_remote_workbench_service.py",
        description="Run one local remote-workbench service in the foreground.",
    )
    parser.add_argument(
        "service",
        choices=("job-api", "control-panel", "public-entry"),
        help="Service name to run.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_REMOTE_WORKBENCH_CONFIG_PATH),
        help="Path to remote_workbench.local.json",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run public-entry preflight only and exit without starting the foreground service.",
    )
    args = parser.parse_args(argv)
    if args.check_only and args.service != "public-entry":
        parser.error("--check-only is only supported for service=public-entry")

    runtime_config = load_remote_workbench_runtime_config(args.config)
    try:
        if args.service == "job-api":
            return _run_job_api(runtime_config)
        if args.service == "public-entry":
            if args.check_only:
                return check_caddy_public_entry(runtime_config)
            return run_caddy_public_entry(runtime_config)
        return _run_control_panel(runtime_config)
    except CaddyPublicEntryError as exc:
        print(f"Public entry preflight failed: {exc}", file=sys.stderr)
        return 2


def _run_job_api(runtime_config) -> int:
    # Attach persistent rotating file log early so all job-api log lines
    # land in runtime_logs/jobapi.app.log (survives container recreate).
    # Same fail-safe wrap as ``main.run_job_api_command`` — the two job-api
    # entry paths must stay in lock-step (attach_rotating_file_log itself is
    # idempotent, so a double call is harmless).
    try:
        from utils.rotating_log import attach_rotating_file_log
        attach_rotating_file_log("jobapi.app.log")
    except Exception as _exc:  # noqa: BLE001
        print(f"[job-api] WARNING: rotating log attach failed: {_exc}", flush=True)

    service = build_default_job_service(project_root=PROJECT_ROOT)

    # Post-build wiring — idle-cancel callback, segment TTS caller,
    # cleanup background thread. The SAME helper is called from
    # ``main.run_job_api_command`` so the developer path stays in
    # lock-step. See services.jobs.runtime_wiring.
    from services.jobs.runtime_wiring import apply_runtime_wiring

    apply_runtime_wiring(service)

    server = build_job_api_server(
        service=service,
        host=runtime_config.job_api.host,
        port=runtime_config.job_api.port,
    )
    print(f"Job API started at {runtime_config.job_api.base_url}")
    print(f"Remote workbench config: {runtime_config.path}")
    print(f"Runtime logs dir: {runtime_config.runtime_logs_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Job API...")
    finally:
        server.server_close()
    return 0


def _run_control_panel(runtime_config) -> int:
    server = create_control_panel_server(
        host=runtime_config.control_panel.binding.host,
        port=runtime_config.control_panel.binding.port,
        config_path=config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH,
    )
    control_panel_url = runtime_config.control_panel.binding.base_url
    print(f"Control panel started at {control_panel_url}")
    print(f"Remote workbench config: {runtime_config.path}")
    print(f"Runtime logs dir: {runtime_config.runtime_logs_dir}")
    print(f"Local config: {server.config_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping control panel...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

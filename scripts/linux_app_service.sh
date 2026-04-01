#!/usr/bin/env bash
set -euo pipefail

config_path="${REMOTE_WORKBENCH_CONFIG_PATH:-/opt/aivideotrans/app/remote_workbench.local.json}"
runtime_logs_dir="${AIVIDEOTRANS_RUNTIME_LOGS_DIR:-/opt/aivideotrans/data/runtime_logs}"
job_api_log_path="${JOB_API_LOG_PATH:-${runtime_logs_dir}/job-api.stdout.log}"

mkdir -p "${runtime_logs_dir}"

python scripts/linux_remote_workbench_preflight.py app-preflight --config "${config_path}"

python scripts/run_remote_workbench_service.py job-api --config "${config_path}" >>"${job_api_log_path}" 2>&1 &
job_api_pid=$!

cleanup() {
    for pid in "${job_api_pid:-}"; do
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
            wait "${pid}" 2>/dev/null || true
        fi
    done
}

trap cleanup EXIT INT TERM

python scripts/linux_remote_workbench_preflight.py app-health --config "${config_path}" --timeout 20

echo "Linux app service started with config ${config_path}"
echo "Job API log: ${job_api_log_path}"

wait -n "${job_api_pid}"

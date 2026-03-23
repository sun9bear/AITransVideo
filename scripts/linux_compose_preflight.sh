#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
compose_file="${COMPOSE_FILE:-${project_root}/docker-compose.yml}"
env_file="${1:-/opt/aivideotrans/config/.env}"

if [[ ! -f "${env_file}" ]]; then
    echo "Missing env file: ${env_file}" >&2
    exit 1
fi

while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    line="${raw_line%$'\r'}"
    if [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    if [[ "${line}" != *=* ]]; then
        echo "Invalid env assignment in ${env_file}: ${line}" >&2
        exit 1
    fi

    env_key="${line%%=*}"
    env_value="${line#*=}"
    if [[ "${env_value}" == \"*\" && "${env_value}" == *\" ]]; then
        env_value="${env_value:1:${#env_value}-2}"
    elif [[ "${env_value}" == \'*\' && "${env_value}" == *\' ]]; then
        env_value="${env_value:1:${#env_value}-2}"
    fi
    export "${env_key}=${env_value}"
done < "${env_file}"

root_dir="${AIVIDEOTRANS_ROOT:-/opt/aivideotrans}"

mkdir -p \
    "${root_dir}/data/projects" \
    "${root_dir}/data/jobs" \
    "${root_dir}/data/runtime_logs" \
    "${root_dir}/caddy/data" \
    "${root_dir}/caddy/config"

required_files=(
    "${root_dir}/config/remote_workbench.local.json"
    "${root_dir}/caddy/Caddyfile"
)

for required_file in "${required_files[@]}"; do
    if [[ ! -f "${required_file}" ]]; then
        echo "Missing required file: ${required_file}" >&2
        exit 1
    fi
done

if [[ ! -f "${root_dir}/config/autodub.local.json" ]]; then
    echo "Warning: ${root_dir}/config/autodub.local.json is missing; real provider paths may not work yet." >&2
fi

docker compose --env-file "${env_file}" -f "${compose_file}" config -q
docker compose --env-file "${env_file}" -f "${compose_file}" run --rm --no-deps app \
    python scripts/linux_remote_workbench_preflight.py app-preflight
docker compose --env-file "${env_file}" -f "${compose_file}" run --rm --no-deps caddy \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

echo "Linux compose preflight passed."

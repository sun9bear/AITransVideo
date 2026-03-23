#!/usr/bin/env bash
set -euo pipefail

project_root="/opt/aivideotrans/app"
config_dir="${AIVIDEOTRANS_CONFIG_DIR:-/opt/aivideotrans/config}"

remote_workbench_source="${config_dir}/remote_workbench.local.json"
remote_workbench_target="${project_root}/remote_workbench.local.json"
autodub_source="${config_dir}/autodub.local.json"
autodub_target="${project_root}/autodub.local.json"

if [[ -f "${remote_workbench_source}" ]]; then
    ln -sfn "${remote_workbench_source}" "${remote_workbench_target}"
fi

if [[ -f "${autodub_source}" ]]; then
    ln -sfn "${autodub_source}" "${autodub_target}"
else
    rm -f "${autodub_target}"
fi

exec "$@"

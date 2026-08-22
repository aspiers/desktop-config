#!/bin/sh
# Install monitor-controller into its fixed, locked service environment.
set -eu

umask 077

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
home=${HOME:?HOME is required for the fixed systemd service path}
data_home=${XDG_DATA_HOME:-$home/.local/share}
service_data_home=$home/.local/share
install_root=$data_home/monitor-controller
venv=$install_root/venv
uv_bin=${UV_BIN:-uv}
python_bin=${MONITOR_CONTROLLER_PYTHON:-python3.13}

case $home:$data_home in
    /*:/*) ;;
    *)
        printf '%s\n' "HOME and XDG_DATA_HOME must be absolute paths" >&2
        exit 2
        ;;
esac
if [ "$data_home" != "$service_data_home" ]; then
    printf '%s\n' \
        "Unsupported XDG_DATA_HOME for fixed service path: $data_home" >&2
    printf '%s\n' \
        "monitor-controller-shadow.service requires $service_data_home" >&2
    exit 2
fi

for required in pyproject.toml uv.lock; do
    if [ ! -f "$project_root/$required" ]; then
        printf '%s\n' "Missing locked project input: $project_root/$required" >&2
        exit 2
    fi
done
if ! command -v "$uv_bin" >/dev/null 2>&1; then
    printf '%s\n' "Cannot find uv installer: $uv_bin" >&2
    exit 2
fi
if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf '%s\n' "Cannot find Python 3.13 interpreter: $python_bin" >&2
    exit 2
fi

mkdir -p "$install_root"
chmod 700 "$install_root"
exec 9>"$install_root/install.lock"
if ! flock 9; then
    printf '%s\n' "Cannot lock monitor-controller installation" >&2
    exit 1
fi

# --locked rejects metadata drift from uv.lock.  --no-editable makes the service
# independent of the Stow source tree after this command, while an explicit
# project reinstall ensures source-only updates are deployed even when the
# package version is unchanged.  --no-dev keeps the runtime minimal, and Python
# downloads are never implicit.  This installation step is not offline: uv may
# fetch locked build/runtime inputs that are absent from its cache.  The service
# itself starts the resulting venv directly and never runs uv or package setup.
env UV_PROJECT_ENVIRONMENT="$venv" \
    "$uv_bin" sync \
    --project "$project_root" \
    --locked \
    --no-dev \
    --no-editable \
    --reinstall-package monitor-controller \
    --no-python-downloads \
    --python "$python_bin"

if [ ! -x "$venv/bin/python" ]; then
    printf '%s\n' "Locked installation did not create $venv/bin/python" >&2
    exit 1
fi

# Isolated import proves that monitor_controller came from the fixed venv rather
# than the current directory, PYTHONPATH, or the Stow source checkout.
"$venv/bin/python" -I -c '
from pathlib import Path
import monitor_controller.shadow
module = Path(monitor_controller.shadow.__file__).resolve()
venv = Path(__import__("sys").prefix).resolve()
if not module.is_relative_to(venv):
    raise SystemExit(f"shadow module was not installed inside {venv}: {module}")
'

printf '%s\n' "Installed locked monitor-controller runtime: $venv"

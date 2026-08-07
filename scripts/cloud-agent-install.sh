#!/usr/bin/env bash
# Idempotent install for the V2R auto-posting program.
# Creates/refreshes a local virtualenv, installs pinned dependencies, and
# installs the package in editable mode so the `v2r-autopost` CLI is available.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

# Ensure the stdlib venv module is available (missing on some base images).
if ! "$PYTHON" -c "import ensurepip, venv" >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    PY_MINOR="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    sudo apt-get update -qq
    sudo apt-get install -y -qq "python${PY_MINOR}-venv" python3-pip
  else
    echo "error: python venv module unavailable and sudo not present" >&2
    exit 1
  fi
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e .

echo "install: done ($(python --version))"

#!/bin/sh
# Create the POSIX development environment without replacing existing user configuration.
set -eu

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf '%s\n' "Python 3.11 or newer is required; set PYTHON to its executable." >&2
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" --version 2>&1)
VERSION=${PYTHON_VERSION#Python }
MAJOR=${VERSION%%.*}
VERSION_REST=${VERSION#*.}
MINOR=${VERSION_REST%%.*}
case "$MAJOR" in ''|*[!0-9]*) printf '%s\n' "Could not determine Python version from: $PYTHON_VERSION" >&2; exit 1 ;; esac
case "$MINOR" in ''|*[!0-9]*) printf '%s\n' "Could not determine Python version from: $PYTHON_VERSION" >&2; exit 1 ;; esac
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]; }; then
    printf '%s\n' "Python 3.11 or newer is required; found $PYTHON_VERSION." >&2
    exit 1
fi

"$PYTHON" -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"

if [ -n "${SHORTFORM_STUDIO_HOME:-}" ]; then
    APP_HOME=$SHORTFORM_STUDIO_HOME
else
    APP_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}/shortform-content-studio
fi
mkdir -p "$APP_HOME"
if [ ! -f "$APP_HOME/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$APP_HOME/.env"
fi
printf 'Setup complete. Edit %s/.env, then run %s/run.sh\n' "$APP_HOME" "$SCRIPT_DIR"

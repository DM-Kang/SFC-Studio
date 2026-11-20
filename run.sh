#!/bin/sh
# Keep direct execution loopback-only; Compose uses its separately guarded port mapping.
set -eu
SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "Virtual environment missing; run ./setup.sh first." >&2
    exit 1
fi
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/.venv/bin/python" -m uvicorn shortform_studio.main:app --host 127.0.0.1 --port 8765

#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONPATH="$ROOT/pydeps${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON:-python3}" "$ROOT/server.py" "$@"

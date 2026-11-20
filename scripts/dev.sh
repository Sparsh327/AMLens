#!/usr/bin/env bash
# Start the API locally with auto-reload.
#   ./scripts/dev.sh            (extra args go to uvicorn, e.g. --port 8001)
# Runs through scripts/odbc.sh so the MS SQL health check can load its ODBC driver on macOS.
set -euo pipefail
exec "$(dirname "$0")/odbc.sh" uv run uvicorn amlens.api.main:app --reload "$@"

#!/usr/bin/env bash
# Run any command with the environment the MS ODBC driver needs.
#   ./scripts/odbc.sh uv run python scripts/load_mssql.py
#
# macOS + Homebrew: ODBC Driver 18 only supports OpenSSL 1.0/1.1/3.x and loads it from
# /opt/homebrew/opt/openssl, which Homebrew now points at OpenSSL 4. Pointing the dynamic
# loader at OpenSSL 3 fixes "OpenSSL library could not be loaded". No-op on Linux/CI.
set -euo pipefail

if [[ "$(uname)" == "Darwin" && -d /opt/homebrew/opt/openssl@3/lib ]]; then
  export DYLD_LIBRARY_PATH="/opt/homebrew/opt/openssl@3/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi

cd "$(dirname "$0")/.."
exec "$@"

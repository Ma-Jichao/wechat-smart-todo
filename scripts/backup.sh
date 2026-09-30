#!/usr/bin/env bash
# Same native backup workflow; use the deployment configuration for DB and paths.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec "$ROOT/venv/bin/python" "$ROOT/scripts/backup.py"

#!/usr/bin/env bash
# Build the wheel and verify it installs and imports in a clean venv.
set -euo pipefail
cd "$(dirname "$0")/.."

uv build
SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT

uv venv "$SMOKE_DIR/venv" --python 3.12
WHEEL="$(ls dist/cogindex-*.whl | sort | tail -1)"
uv pip install --python "$SMOKE_DIR/venv/bin/python" "$WHEEL"
"$SMOKE_DIR/venv/bin/python" - <<'EOF'
import cogindex
print("cogindex", cogindex.__version__, "imported OK from clean venv")
EOF

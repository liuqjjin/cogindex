#!/usr/bin/env bash
# Build the wheel and verify the current version installs in a clean venv.
set -euo pipefail
cd "$(dirname "$0")/.."

SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT

uv build --wheel --out-dir "$SMOKE_DIR/dist"
uv venv "$SMOKE_DIR/venv" --python 3.12
shopt -s nullglob
WHEELS=("$SMOKE_DIR"/dist/cogindex-*.whl)
if [[ "${#WHEELS[@]}" -ne 1 || ! -f "${WHEELS[0]}" ]]; then
  echo "expected exactly one cogindex wheel, found ${#WHEELS[@]}" >&2
  exit 1
fi
WHEEL="${WHEELS[0]}"
uv pip install --python "$SMOKE_DIR/venv/bin/python" "$WHEEL"
EXPECTED_VERSION="$(uv run python -c \
  'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
export EXPECTED_VERSION
"$SMOKE_DIR/venv/bin/python" - <<'EOF'
import os
from importlib.metadata import version

import cogindex

expected = os.environ["EXPECTED_VERSION"]
assert cogindex.__version__ == expected
assert version("cogindex") == expected
print("cogindex", expected, "imported OK from clean venv")
EOF

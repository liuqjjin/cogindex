#!/usr/bin/env bash
# Verify built package artifacts install and import in clean virtual environments.
set -euo pipefail
cd "$(dirname "$0")/.."

SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT

if [[ "$#" -eq 0 ]]; then
  uv build --wheel --out-dir "$SMOKE_DIR/dist"
  shopt -s nullglob
  ARTIFACTS=("$SMOKE_DIR"/dist/cogindex-*.whl)
else
  ARTIFACTS=("$@")
fi

if [[ "${#ARTIFACTS[@]}" -eq 0 ]]; then
  echo "expected at least one cogindex package artifact" >&2
  exit 1
fi

EXPECTED_VERSION="$(uv run python -c \
  'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
export EXPECTED_VERSION

for index in "${!ARTIFACTS[@]}"; do
  artifact="${ARTIFACTS[$index]}"
  if [[ ! -f "$artifact" ]]; then
    echo "package artifact not found: $artifact" >&2
    exit 1
  fi
  case "$(basename "$artifact")" in
    cogindex-*.whl|cogindex-*.tar.gz) ;;
    *)
      echo "unexpected package artifact: $artifact" >&2
      exit 1
      ;;
  esac

  if [[ "$artifact" == *.tar.gz ]] && tar -tzf "$artifact" | grep -E \
    '/(\.git|\.upstream|\.venv|dist)/' >/dev/null; then
    echo "source archive contains local or generated repository state: $artifact" >&2
    exit 1
  fi

  venv="$SMOKE_DIR/venv-$index"
  uv venv "$venv" --python 3.12
  uv pip install --python "$venv/bin/python" "$artifact"
  "$venv/bin/python" - <<'EOF'
import os
from importlib.metadata import version

import cogindex

expected = os.environ["EXPECTED_VERSION"]
assert cogindex.__version__ == expected
assert version("cogindex") == expected
print("cogindex", expected, "imported OK from clean venv")
EOF

  site_packages="$("$venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
  uv run pip-audit \
    --path "$site_packages" \
    --ignore-vuln PYSEC-2026-2447
done

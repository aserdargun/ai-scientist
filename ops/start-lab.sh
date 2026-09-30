#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  printf 'Projenin Python ortamı bulunamadı: %s/.venv\n' "$ROOT" >&2
  exit 1
fi
exec "$ROOT/.venv/bin/python" "$ROOT/ops/start_lab.py" "$@"

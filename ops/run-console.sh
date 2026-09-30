#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
UNIT="swapp-ai-scientist-console.service"
URL="http://127.0.0.1:8788"
PYTHON="$ROOT/.venv/bin/python"
SYSTEMCTL="$(command -v systemctl || true)"

if [[ -z "$SYSTEMCTL" ]]; then
  printf 'systemctl is required to inspect the console unit.\n' >&2
  exit 1
fi
if ! LOAD_STATE="$(systemctl --user show "$UNIT" --property=LoadState --value 2>&1)"; then
  printf 'Could not inspect the console unit safely: %s\n' "$LOAD_STATE" >&2
  exit 1
fi
if [[ "$LOAD_STATE" == "loaded" ]]; then
  ACTIVE_STATE="$(systemctl --user show "$UNIT" --property=ActiveState --value)"
  if [[ "$ACTIVE_STATE" == "active" ]]; then
    printf 'Console already running: %s\n' "$URL"
    systemctl --user --no-pager --full status "$UNIT" || true
    exit 0
  fi
  printf 'Console unit exists but is %s; refusing to restart it automatically.\n' "$ACTIVE_STATE" >&2
  systemctl --user --no-pager --full status "$UNIT" || true
  exit 1
elif [[ "$LOAD_STATE" != "not-found" ]]; then
  printf 'Console unit has unexpected load state %s; refusing to start.\n' "$LOAD_STATE" >&2
  exit 1
fi

if ! command -v systemd-run >/dev/null 2>&1; then
  printf 'systemd-run is required to apply console resource limits.\n' >&2
  exit 1
fi
if [[ -z "${XDG_RUNTIME_DIR:-}" || -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  printf 'Start this from the current user session with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS set.\n' >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  printf 'Project Python environment is missing: %s\n' "$PYTHON" >&2
  exit 1
fi
if [[ ! -f "$ROOT/console/web/dist/index.html" ]]; then
  printf 'Built console frontend is missing: %s\n' "$ROOT/console/web/dist/index.html" >&2
  exit 1
fi

if "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.settimeout(.2); sys.exit(0 if s.connect_ex(("127.0.0.1",8788)) == 0 else 1)'; then
  printf 'Port 8788 is already accepting connections; refusing to affect an unknown service.\n' >&2
  exit 1
fi

ENV_ARGS=(
  "--setenv=XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  "--setenv=DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
)
for key in LAB_CONSOLE_API_URL LAB_CONSOLE_TOKEN_FILE LAB_CONSOLE_SUITE_REGISTRY_FILE MODEL_RUNS_ENABLED; do
  if [[ -v "$key" ]]; then
    ENV_ARGS+=("--setenv=$key=${!key}")
  fi
done

systemd-run --user --collect --quiet --unit="$UNIT" --service-type=exec \
  --working-directory="$ROOT" \
  --property=MemoryMax=512M \
  --property=MemorySwapMax=0 \
  --property=CPUQuota=50% \
  --property=TasksMax=64 \
  --property=KillMode=control-group \
  --setenv=PYTHONPATH="$ROOT" \
  --setenv=OMP_NUM_THREADS=1 \
  --setenv=OPENBLAS_NUM_THREADS=1 \
  --setenv=MKL_NUM_THREADS=1 \
  "${ENV_ARGS[@]}" \
  "$PYTHON" -m console.server

for _attempt in {1..40}; do
  if "$PYTHON" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8788/console-api/overview",timeout=.25).read(1)' >/dev/null 2>&1; then
    printf 'Console ready at %s\n' "$URL"
    exit 0
  fi
  sleep 0.25
done
printf 'Console did not become healthy.\n' >&2
systemctl --user --no-pager --full status "$UNIT" || true
exit 1

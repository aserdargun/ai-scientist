#!/usr/bin/env bash
# Run on the client computer; SSH keeps the Lab loopback-only.
set -euo pipefail
host=cachyos user=cachyos remote_dir=/home/cachyos/ai-scientist local_port=8788
start_lab=1 browser=1
usage() {
  cat <<'EOF'
Usage: bash ops/connect-lab.sh [--host SSH_HOST] [--user SSH_USER]
       [--local-port PORT] [--remote-dir ABSOLUTE_PATH] [--no-start-lab] [--no-browser]
Run on aserdargun's computer. SSH_HOST must resolve/reach the CachyOS computer
or name an existing SSH config alias. Default: cachyos. Ctrl+C closes the tunnel.
By default runs the remote ops/start-lab.sh launcher; --no-start-lab skips it.
Remote paths may contain only letters, digits, slash, underscore, dot and dash.
EOF
}
while (($#)); do
  case "$1" in
    --host|--user|--local-port|--remote-dir)
      (($# >= 2)) || { usage >&2; exit 2; }
      case "$1" in
        --host) host=$2;; --user) user=$2;; --local-port) local_port=$2;; --remote-dir) remote_dir=$2;;
      esac
      shift 2;;
    --start-lab) start_lab=1; shift;;
    --no-start-lab) start_lab=0; shift;;
    --no-browser) browser=0; shift;;
    --help|-h) usage; exit 0;;
    *) usage >&2; exit 2;;
  esac
done
[[ "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ && "$user" =~ ^[a-zA-Z0-9_][a-zA-Z0-9._-]*$ ]] || {
  printf 'Invalid SSH host or user; use a hostname or SSH config alias.\n' >&2; exit 2;
}
[[ "$remote_dir" =~ ^/[a-zA-Z0-9._/-]+$ ]] || { printf 'Invalid remote path.\n' >&2; exit 2; }
[[ "$local_port" =~ ^[0-9]{1,5}$ ]] || { printf 'Invalid port.\n' >&2; exit 2; }
local_port=$((10#$local_port))
((local_port > 0 && local_port <= 65535)) || { printf 'Invalid port.\n' >&2; exit 2; }
for command in ssh curl; do
  command -v "$command" >/dev/null || { printf 'Required command missing: %s\n' "$command" >&2; exit 1; }
done
opener=
if ((browser)); then
  if command -v xdg-open >/dev/null; then opener=xdg-open
  elif command -v open >/dev/null; then opener=open
  else printf 'No browser opener found; use --no-browser.\n' >&2; exit 1
  fi
fi
if (exec 3<>"/dev/tcp/127.0.0.1/$local_port") 2>/dev/null; then
  printf 'Local port %s is occupied; choose --local-port.\n' "$local_port" >&2; exit 1
fi
ssh_options=(-o ExitOnForwardFailure=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if ((start_lab)); then
  ssh "${ssh_options[@]}" -l "$user" "$host" "cd '$remote_dir' && bash ops/start-lab.sh"
fi
ssh_pid=
cleanup() {
  if [[ -n "$ssh_pid" ]]; then
    kill "$ssh_pid" 2>/dev/null || true
    wait "$ssh_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# stdin stays attached for normal SSH authentication/host-key prompts.
ssh "${ssh_options[@]}" -N -T -L "127.0.0.1:$local_port:127.0.0.1:8788" -l "$user" "$host" <&0 &
ssh_pid=$!
url="http://127.0.0.1:$local_port/"
ready=0
deadline=$((SECONDS + 60))
while ((SECONDS < deadline)); do
  kill -0 "$ssh_pid" 2>/dev/null || { printf 'SSH tunnel exited before readiness.\n' >&2; exit 1; }
  if curl --noproxy '*' --fail --silent --output /dev/null --max-time 1 "$url"; then
    # A failed forward can race an unrelated listener; require the SSH child alive.
    sleep 0.2
    kill -0 "$ssh_pid" 2>/dev/null || { printf 'SSH tunnel exited before readiness.\n' >&2; exit 1; }
    ready=1; break
  fi
  sleep 0.25
done
((ready)) || { printf 'Lab did not respond within 60 seconds.\n' >&2; exit 1; }
printf 'Lab: %s\nKeep this terminal open. Ctrl+C closes only this SSH tunnel.\n' "$url"
if ((browser)); then "$opener" "$url" || printf 'Browser could not open; use the URL above.\n' >&2; fi
wait "$ssh_pid"

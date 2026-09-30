"""Enter a private user/network namespace before replacing the process."""

from __future__ import annotations

import os
import subprocess  # nosec B404 -- fixed ip binary, no candidate-controlled command
import sys
from pathlib import Path


def main() -> int:
    """Bring up only private loopback, then exec the already fixed child argv."""
    args = sys.argv[1:]
    if not args or args[0] != "--" or len(args) < 2:
        raise SystemExit("expected -- followed by a fixed runtime command")
    host_net_text = os.environ.get("SWAPP_GPU_HOST_NETNS_INODE", "")
    if not host_net_text.isdecimal():
        raise SystemExit("trusted host network namespace identity is unavailable")
    host_net = int(host_net_text)
    own_net = os.stat("/proc/self/ns/net").st_ino
    if host_net == own_net:
        raise SystemExit("refusing to start the model outside a private network namespace")
    ip = Path("/usr/bin/ip")
    if not ip.is_file() or ip.is_symlink():
        raise SystemExit("fixed ip utility is unavailable")
    subprocess.run(  # nosec B603 -- fixed binary and fixed loopback operation
        [str(ip), "link", "set", "dev", "lo", "up"],
        check=True,
        timeout=3,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    command = args[1:]
    # Replace this wrapper with the absolute vLLM executable and fixed server argv.
    os.execv(command[0], command)  # nosec B606
    return 127


if __name__ == "__main__":
    raise SystemExit(main())

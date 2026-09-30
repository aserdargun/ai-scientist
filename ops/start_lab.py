"""Start the existing local installation without restarting active services.

MODEL_RUNS_ENABLED controls the console UI only; it does not fence Director GPU work.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv/bin/python")
CONFIG = Path.home() / ".config/swapp-ai-scientist"
RUNTIME = ROOT / "data/runtime/console-bootstrap-034"
CONTAINER = "swapp-lab-postgres-m0"
SERVICES = (
    (
        "api",
        "1G",
        "100%",
        "32",
        "lab-api.env",
        8766,
        ["lab.cli", "api", "serve", "--host", "127.0.0.1", "--port", "8766"],
    ),
    (
        "director-drain",
        "2G",
        "100%",
        "32",
        "lab-worker.env",
        None,
        ["lab.cli", "director", "drain", "--poll-seconds", "2"],
    ),
    ("console", "512M", "50%", "64", None, 8788, ["console.server"]),
)


def command(args: list[str], *, timeout: int = 20) -> str:
    """Keep private service configuration and Docker metadata out of errors."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} başarısız (kod {result.returncode}).")
    return result.stdout


def inspect_unit(unit: str) -> dict[str, str]:
    output = command(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState,ActiveState,WorkingDirectory,ExecStart,MainPID,"
            "MemoryMax,MemorySwapMax,CPUQuotaPerSecUSec,TasksMax",
        ]
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def _process_identity(pid: str) -> tuple[Path, list[str]]:
    """Read only the named service process, with its full argument vector."""
    if not pid.isdecimal() or int(pid) < 1:
        raise RuntimeError("Servisin geçerli süreç kimliği yok.")
    process = Path("/proc") / pid
    raw = process.joinpath("cmdline").read_bytes()
    if not raw.endswith(b"\0"):
        raise RuntimeError("Servis süreç komutu okunamadı.")
    return process.joinpath("cwd").resolve(), [part.decode() for part in raw[:-1].split(b"\0")]


def check_unit(unit: str, module: str, port: int | None) -> dict[str, str]:
    configured = [item for item in SERVICES if unit == f"swapp-ai-scientist-{item[0]}.service"]
    if len(configured) != 1 or (module, port) != (configured[0][6][0], configured[0][5]):
        raise RuntimeError("Bilinmeyen servis kimliği; işlem yapılmadı.")
    _, memory, cpu, tasks, _, _, args = configured[0]
    expected_caps = {
        "MemoryMax": str(int(memory[:-1]) * (1024 ** (3 if memory[-1] == "G" else 2))),
        "MemorySwapMax": "0",
        "CPUQuotaPerSecUSec": "1s" if cpu == "100%" else "500ms",
        "TasksMax": tasks,
    }
    python_argv = [PYTHON, "-m", *args]
    lab = str(ROOT / ".venv/bin/lab")
    allowed = [python_argv]
    if module == "lab.cli":
        allowed.append([lab, *args[1:]])
    state = inspect_unit(unit)
    if state["LoadState"] == "loaded":
        definition = re.fullmatch(
            r"\{ path=([^ ;]+) ; argv\[\]=(.*?) ; ignore_errors=no ; [^{}]* \}",
            state.get("ExecStart", ""),
        )
        selected = next(
            (
                argv
                for argv in allowed
                if definition is not None
                and definition.group(1) == argv[0]
                and definition.group(2) == " ".join(argv)
            ),
            None,
        )
        if (
            state.get("WorkingDirectory") != str(ROOT)
            or selected is None
            or any(state.get(key) != value for key, value in expected_caps.items())
        ):
            raise RuntimeError(f"{unit}: servis komutu veya kaynak sınırları eşleşmiyor.")
        if state["ActiveState"] not in {"active", "inactive", "failed"}:
            raise RuntimeError(f"{unit}: geçiş sürüyor; biraz sonra tekrar çalıştırın.")
        if state["ActiveState"] == "active":
            cwd, process_argv = _process_identity(state["MainPID"])
            allowed_processes = (
                [python_argv]
                if selected == python_argv
                else [
                    [interpreter, *selected]
                    for interpreter in (PYTHON, str(ROOT / ".venv/bin/python3"))
                ]
            )
            if cwd != ROOT or process_argv not in allowed_processes:
                raise RuntimeError(f"{unit}: süreç kimliği eşleşmedi.")
            return state
    elif state["LoadState"] != "not-found":
        raise RuntimeError(f"{unit}: servis tanımı okunamıyor.")
    if port is not None:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"{port} portu başka bir süreç tarafından kullanılıyor.")
    return state


def wait_http(url: str, *, console: bool = False) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2) as response:
                payload = json.load(response)
            if console:
                if payload.get("lab", {}).get("connected") is True:
                    return
            elif payload == {"status": "ok", "service": "lab-api"}:
                return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Servis 30 saniye içinde hazır olmadı: {url}")


def start() -> None:
    for executable in ("docker", "systemctl", "systemd-run"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"Gerekli komut bulunamadı: {executable}")
    if not os.environ.get("XDG_RUNTIME_DIR") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise RuntimeError(
            "Komutu cachyos kullanıcısının masaüstü terminalinde, sudo olmadan çalıştırın."
        )
    for path in (CONFIG / "lab-api.env", CONFIG / "lab-worker.env", RUNTIME / "principals.json"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
        ):
            raise RuntimeError(
                f"Kuruluma ait özel yapılandırma eksik veya izinleri uygun değil: {path}"
            )
    for path in (RUNTIME / "registry.json", ROOT / "console/web/dist/index.html"):
        if not path.is_file():
            raise RuntimeError(f"Kurulum dosyası eksik: {path}")

    # Validate all service identities before starting any component.
    for name, _, _, _, _, port, args in SERVICES:
        check_unit(f"swapp-ai-scientist-{name}.service", args[0], port)
    try:
        info = json.loads(command(["docker", "inspect", CONTAINER]))[0]
    except RuntimeError:
        raise RuntimeError(
            "Docker'a veya mevcut Lab veritabanına erişilemiyor. "
            "Docker kapalıysa `sudo systemctl start docker` çalıştırıp tekrar deneyin."
        ) from None
    host = info["HostConfig"]
    if (
        info["Config"].get("Labels", {}).get("org.swapp.component") != "ai-scientist-m0-ledger"
        or host["PortBindings"] != {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
        or host["Memory"] != 512 * 1024 * 1024
        or host["MemorySwap"] != host["Memory"]
        or host["NanoCpus"] != 1_000_000_000
        or host["PidsLimit"] != 128
    ):
        raise RuntimeError("Lab veritabanı kimliği/portu/kaynak sınırları kurulumla eşleşmiyor.")
    if info["State"]["Status"] in {"created", "exited"}:
        print("Veritabanı başlatılıyor…", flush=True)
        command(["docker", "start", info["Id"]])
    elif info["State"]["Status"] != "running":
        raise RuntimeError("Veritabanı durumu başlatmaya uygun değil.")
    for attempt in range(30):
        try:
            command(
                [
                    "docker",
                    "exec",
                    info["Id"],
                    "pg_isready",
                    "-U",
                    "swapp_lab_admin",
                    "-d",
                    "swapp_lab",
                ],
                timeout=5,
            )
            break
        except RuntimeError:
            if attempt == 29:
                raise RuntimeError("PostgreSQL hazır olmadı.") from None
            time.sleep(0.5)
    print("Veritabanı hazır.", flush=True)

    for name, memory, cpu, tasks, env_file, port, args in SERVICES:
        unit = f"swapp-ai-scientist-{name}.service"
        state = check_unit(unit, args[0], port)
        if state["ActiveState"] != "active":
            print(f"{name} başlatılıyor…", flush=True)
            if state["LoadState"] == "loaded":
                command(["systemctl", "--user", "start", unit])
            else:
                invocation = [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--quiet",
                    "--service-type=exec",
                    f"--unit={unit}",
                    f"--working-directory={ROOT}",
                    f"--property=MemoryMax={memory}",
                    "--property=MemorySwapMax=0",
                    f"--property=CPUQuota={cpu}",
                    f"--property=TasksMax={tasks}",
                    "--property=KillMode=control-group",
                    f"--setenv=PYTHONPATH={ROOT}",
                    "--setenv=OMP_NUM_THREADS=1",
                    "--setenv=OPENBLAS_NUM_THREADS=1",
                    "--setenv=MKL_NUM_THREADS=1",
                    # UI control only; Director resource/admission policy is independent.
                    "--setenv=MODEL_RUNS_ENABLED=false",
                ]
                if env_file:
                    invocation.append(f"--property=EnvironmentFile={CONFIG / env_file}")
                else:
                    invocation.extend(
                        [
                            f"--setenv=XDG_RUNTIME_DIR={os.environ['XDG_RUNTIME_DIR']}",
                            f"--setenv=DBUS_SESSION_BUS_ADDRESS={os.environ['DBUS_SESSION_BUS_ADDRESS']}",
                            "--setenv=LAB_CONSOLE_API_URL=http://127.0.0.1:8766",
                            f"--setenv=LAB_CONSOLE_TOKEN_FILE={RUNTIME / 'principals.json'}",
                            f"--setenv=LAB_CONSOLE_SUITE_REGISTRY_FILE={RUNTIME / 'registry.json'}",
                        ]
                    )
                command([*invocation, PYTHON, "-m", *args])
        if name == "api":
            wait_http("http://127.0.0.1:8766/health")
        elif name == "console":
            wait_http("http://127.0.0.1:8788/console-api/overview", console=True)
        print(f"{name}: hazır", flush=True)
    for name, _, _, _, _, port, args in SERVICES:
        if (
            check_unit(f"swapp-ai-scientist-{name}.service", args[0], port)["ActiveState"]
            != "active"
        ):
            raise RuntimeError(f"{name}: başlangıçtan sonra kapandı; servis günlüğünü inceleyin.")
    print(
        "\nArayüz: http://127.0.0.1:8788\n"
        "Terminali kapatabilirsiniz; servisler arka planda çalışır."
    )


def main() -> int:
    if len(sys.argv) > 1:
        print("Kullanım: bash ops/start-lab.sh")
        return 0 if sys.argv[1:] in (["--help"], ["-h"]) else 2
    try:
        lock_path = ROOT / "data/runtime/start-lab.lock"
        if lock_path.is_symlink():
            raise RuntimeError("Başlatma kilidi beklenen normal dosya değil.")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Başka bir başlatma işlemi sürüyor.") from None
            start()
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        print(f"Başlatılamadı: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

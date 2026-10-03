"""Persistent, explicitly selected local CPU Lab profile with an isolated ledger.

Preparation writes only private configuration. Starting creates/uses only exact
profile-owned resources; it never imports or drains another installation's queue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67"
LABEL = "org.swapp.lab-profile"
ROLES = {name: f"swapp_lab_{name}" for name in ("migrator", "director", "planner", "scorer")}


def private_read(path: Path) -> str:
    """Reject swapped, shared or foreign private files before reading their values."""
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 0 < info.st_size <= 65536
    ):
        raise ValueError(f"invalid private profile file: {path.name}")
    return path.read_text(encoding="utf-8")


def write_new(path: Path, value: str) -> None:
    """Create a private file once; never overwrite existing service configuration."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def invoke(args: list[str], *, timeout: int = 30, environment=None, cwd: Path = ROOT) -> str:
    result = subprocess.run(  # nosec B603 -- fixed command vectors, no shell
        args, capture_output=True, text=True, timeout=timeout, check=False, env=environment, cwd=cwd
    )
    if result.returncode:
        raise RuntimeError(
            f"profile command failed: {Path(args[0]).name} (exit {result.returncode})"
        )
    return result.stdout


@dataclass(frozen=True)
class LabProfile:
    name: str
    root: Path = ROOT
    database_port: int = 55434
    api_port: int = 8767
    console_port: int = 8789

    def __post_init__(self):
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.name) is None:
            raise ValueError("profile name must use lowercase letters, digits and hyphens")
        if (
            not self.root.is_absolute()
            or self.root.resolve() != self.root
            or re.fullmatch(r"[/A-Za-z0-9_.-]+", str(self.root)) is None
        ):
            raise ValueError("profile checkout path must be canonical and systemd-safe")
        ports = (self.database_port, self.api_port, self.console_port)
        if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
            raise ValueError("profile ports must be integers in 1024..65535")
        if len(set(ports)) != 3:
            raise ValueError("profile ports must be distinct")

    @property
    def directory(self) -> Path:
        return self.root / "data/runtime/lab-profiles" / self.name

    @property
    def identity(self) -> str:
        return f"{self.name}-{hashlib.sha256(str(self.root).encode()).hexdigest()[:12]}"

    @property
    def container(self) -> str:
        return f"swapp-lab-{self.identity}-postgres"

    @property
    def volume(self) -> str:
        return f"swapp-lab-{self.identity}-ledger"

    def unit(self, component: str) -> str:
        if component not in {"api", "director-drain", "console"}:
            raise ValueError("unknown profile component")
        return f"swapp-lab-{self.identity}-{component}.service"

    def document(self) -> dict:
        return {
            "schema": "lab-local-profile.v1",
            "name": self.name,
            "root": str(self.root),
            "owner_uid": os.getuid(),
            "database_port": self.database_port,
            "api_port": self.api_port,
            "console_port": self.console_port,
            "postgres_image": POSTGRES_IMAGE,
            "cpu_only": True,
            "gpu_arbiter": str(Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"),
        }

    def service_text(self, component: str) -> str:
        commands = {
            "api": f"lab.cli api serve --host 127.0.0.1 --port {self.api_port}",
            "director-drain": "lab.cli director drain --poll-seconds 2",
            "console": "console.server",
        }
        caps = {
            "api": ("768M", "50%", "32"),
            "director-drain": ("512M", "25%", "32"),
            "console": ("384M", "25%", "64"),
        }
        memory, cpu, tasks = caps[component]
        return (
            "[Unit]\nDescription=Explicit local CPU Lab profile\nAfter=network.target\n\n"
            "[Service]\nType=exec\n"
            f"WorkingDirectory={self.root}\n"
            f"EnvironmentFile={self.directory / (component + '.env')}\n"
            f"ExecStart={self.root}/.venv/bin/python -m {commands[component]}\n"
            "Restart=on-failure\nRestartSec=5\n"
            f"MemoryMax={memory}\nMemorySwapMax=0\nCPUQuota={cpu}\nTasksMax={tasks}\n"
            "KillMode=control-group\nTimeoutStopSec=20\nOOMPolicy=stop\n"
            "NoNewPrivileges=yes\n"
        )

    def _environment(self, component: str) -> str:
        shared = {
            "PYTHONPATH": str(self.root),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "LAB_CPU_ONLY": "true",
            "MODEL_RUNS_ENABLED": "false",
            "CUDA_VISIBLE_DEVICES": "",
            "LAB_SUITE_REGISTRY_FILE": str(self.directory / "registry.json"),
            "LAB_SCORER_DSN_FILE": str(self.directory / "postgres/scorer.dsn"),
        }
        if component == "console":
            shared.update(
                {
                    "LAB_CONSOLE_API_URL": f"http://127.0.0.1:{self.api_port}",
                    "LAB_CONSOLE_TOKEN_FILE": str(self.directory / "principals.json"),
                    "LAB_CONSOLE_SUITE_REGISTRY_FILE": str(self.directory / "registry.json"),
                    "LAB_CONSOLE_WATCH_FILE": str(self.directory / "watched-runs.json"),
                    "LAB_CONSOLE_PORT": str(self.console_port),
                }
            )
        else:
            shared.update(
                {
                    "LAB_DIRECTOR_DSN_FILE": str(self.directory / "postgres/director.dsn"),
                    "LAB_PLANNER_DSN_FILE": str(self.directory / "postgres/planner.dsn"),
                    "LAB_API_PRINCIPALS_FILE": str(self.directory / "principals.json"),
                }
            )
        return "".join(f'{key}="{value}"\n' for key, value in sorted(shared.items()))

    def prepare(self) -> None:
        """Opt-in private config only; no Docker or service operations."""
        if self.directory.exists():
            self.validate()
            return
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.parent.resolve() != self.directory.parent:
            raise ValueError("profile parent cannot be a symlink")
        self.directory.mkdir(mode=0o700)
        database = self.directory / "postgres"
        database.mkdir(mode=0o700)
        admin = secrets.token_urlsafe(36)
        write_new(database / "admin.secret", admin + "\n")
        write_new(
            database / "container.env",
            "POSTGRES_USER=swapp_lab_admin\n"
            "POSTGRES_DB=swapp_lab\nPOSTGRES_PASSWORD=" + admin + "\n",
        )
        for role, username in ROLES.items():
            password = secrets.token_urlsafe(36)
            write_new(database / f"{role}.secret", password + "\n")
            dsn = (
                f"postgresql+psycopg://{username}:{quote(password, safe='')}"
                f"@127.0.0.1:{self.database_port}/swapp_lab\n"
            )
            write_new(database / f"{role}.dsn", dsn)
        principal = {
            "schema": "lab-api-principals.v1",
            "principals": [
                {
                    "token": secrets.token_urlsafe(48),
                    "origin": "local",
                    "owner_id": "local:" + self.identity,
                }
            ],
        }
        write_new(self.directory / "principals.json", json.dumps(principal) + "\n")
        write_new(
            self.directory / "registry.json",
            json.dumps({"schema": "lab-suite-registry.v1", "suites": []}) + "\n",
        )
        for component in ("api", "director-drain", "console"):
            write_new(self.directory / f"{component}.env", self._environment(component))
            write_new(self.directory / self.unit(component), self.service_text(component))
        write_new(
            self.directory / "profile.json", json.dumps(self.document(), sort_keys=True) + "\n"
        )
        self.validate()

    def validate(self) -> None:
        """Revalidate private receipts, credentials and CPU-only registry on every start."""
        from lab.api.registry import load_principals, load_suite_registry

        if self.directory.resolve() != self.directory or self.directory.stat().st_mode & 0o077:
            raise ValueError("profile directory must be private and canonical")
        if json.loads(private_read(self.directory / "profile.json")) != self.document():
            raise ValueError("profile receipt differs from requested checkout/ports")
        for component in ("api", "director-drain", "console"):
            if private_read(self.directory / f"{component}.env") != self._environment(component):
                raise ValueError("profile environment changed")
            if private_read(self.directory / self.unit(component)) != self.service_text(component):
                raise ValueError("profile service template changed")
        database = self.directory / "postgres"
        admin = private_read(database / "admin.secret").strip()
        if private_read(database / "container.env") != (
            "POSTGRES_USER=swapp_lab_admin\nPOSTGRES_DB=swapp_lab\nPOSTGRES_PASSWORD="
            + admin
            + "\n"
        ):
            raise ValueError("profile database environment changed")
        for role, username in ROLES.items():
            password = private_read(database / f"{role}.secret").strip()
            expected = (
                f"postgresql+psycopg://{username}:{quote(password, safe='')}"
                f"@127.0.0.1:{self.database_port}/swapp_lab"
            )
            if private_read(database / f"{role}.dsn").strip() != expected:
                raise ValueError("profile role/DSN identity changed")
        principals = load_principals(self.directory / "principals.json")
        if len(principals) != 1 or principals[0].owner_id != "local:" + self.identity:
            raise ValueError("profile principal identity changed")
        registry = load_suite_registry(self.directory / "registry.json", self.root / "data/runtime")
        if any(
            entry.provider not in {"mode-grid", "mode-stream"}
            for entry in registry.entries.values()
        ):
            raise ValueError("CPU profile registry contains a model provider")

    def inspect_database(self) -> dict | None:
        """Inspect only this deterministic name, checking all mutable resource bindings."""
        listed = invoke(
            [
                "docker",
                "container",
                "ls",
                "-a",
                "--filter",
                f"name=^/{self.container}$",
                "--format",
                "{{.ID}}",
            ]
        )
        if not listed.strip():
            return None
        info = json.loads(invoke(["docker", "inspect", self.container]))[0]
        host = info["HostConfig"]
        mounts = info["Mounts"]
        if (
            info["Config"].get("Labels", {}).get(LABEL) != self.identity
            or info["Image"] != POSTGRES_IMAGE
            or host["PortBindings"]
            != {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.database_port)}]}
            or host["Memory"] != 512 * 1024**2
            or host["MemorySwap"] != host["Memory"]
            or host["NanoCpus"] != 500_000_000
            or host["PidsLimit"] != 64
            or host["RestartPolicy"]["Name"] != "no"
            or len(mounts) != 1
            or mounts[0].get("Name") != self.volume
            or mounts[0].get("Destination") != "/var/lib/postgresql/data"
            or mounts[0].get("RW") is not True
        ):
            raise ValueError("profile database identity/limits/storage differ")
        return info

    def check_queue(self) -> dict[str, int]:
        """Read-only CPU queue inspection; never imports, changes or drains old rows."""
        from sqlalchemy import create_engine, text

        engine = create_engine(
            private_read(self.directory / "postgres/director.dsn").strip(),
            hide_parameters=True,
            pool_size=1,
            max_overflow=0,
            connect_args={
                "connect_timeout": 5,
                "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
            },
        )
        try:
            with engine.connect() as connection:
                rows = (
                    connection.execute(
                        text(
                            "SELECT state,request_json->>'provider' AS provider,count(*) AS count "
                            "FROM lab.runs WHERE state IN ('queued','running','stop_requested') "
                            "GROUP BY state,request_json->>'provider'"
                        )
                    )
                    .mappings()
                    .all()
                )
            if any(row["provider"] not in {"mode-grid", "mode-stream"} for row in rows):
                raise ValueError("profile queue contains non-CPU work; launch denied")
            return {
                state: sum(row["count"] for row in rows if row["state"] == state)
                for state in ("queued", "running", "stop_requested")
            }
        finally:
            engine.dispose()

    def provision_database(self) -> None:
        """Create only a new empty owned ledger or resume its exact existing container."""
        self.validate()
        info = self.inspect_database()
        if info is None:
            check_free_port(self.database_port)
            image = json.loads(invoke(["docker", "image", "inspect", POSTGRES_IMAGE]))[0]
            if image["Id"] != POSTGRES_IMAGE:
                raise ValueError("cached profile image differs; image pulls are disabled")
            volume_names = invoke(
                [
                    "docker",
                    "volume",
                    "ls",
                    "--filter",
                    f"name=^{self.volume}$",
                    "--format",
                    "{{.Name}}",
                ]
            )
            if volume_names.strip():
                volume = json.loads(invoke(["docker", "volume", "inspect", self.volume]))[0]
                if volume.get("Labels", {}).get(LABEL) != self.identity:
                    raise ValueError("existing database volume is not profile-owned")
            else:
                invoke(
                    [
                        "docker",
                        "volume",
                        "create",
                        "--label",
                        f"{LABEL}={self.identity}",
                        self.volume,
                    ]
                )
            invoke(
                [
                    "docker",
                    "create",
                    "--name",
                    self.container,
                    "--label",
                    f"{LABEL}={self.identity}",
                    "--memory=512m",
                    "--memory-swap=512m",
                    "--cpus=.5",
                    "--pids-limit=64",
                    "--restart=no",
                    "--publish",
                    f"127.0.0.1:{self.database_port}:5432",
                    "--mount",
                    f"type=volume,source={self.volume},target=/var/lib/postgresql/data",
                    "--env-file",
                    str(self.directory / "postgres/container.env"),
                    POSTGRES_IMAGE,
                    "postgres",
                    "-c",
                    "shared_buffers=64MB",
                    "-c",
                    "max_connections=20",
                    "-c",
                    "max_wal_size=256MB",
                    "-c",
                    "temp_file_limit=65536",
                ]
            )
            info = self.inspect_database()
        if info is None or info["State"]["Status"] not in {"running", "created", "exited"}:
            raise ValueError("owned database is not in a startable state")
        if info["State"]["Status"] != "running":
            check_free_port(self.database_port)
            invoke(["docker", "start", info["Id"]])
        for attempt in range(60):
            try:
                invoke(
                    [
                        "docker",
                        "exec",
                        info["Id"],
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "swapp_lab_admin",
                        "-d",
                        "swapp_lab",
                    ],
                    timeout=3,
                )
                break
            except RuntimeError:
                if attempt == 59:
                    raise RuntimeError("owned PostgreSQL did not become ready") from None
                time.sleep(0.5)
        receipt = self.directory / "database-ready.json"
        if not receipt.exists():
            self._initialize_roles()
            environment = os.environ.copy()
            environment["LAB_MIGRATOR_DSN_FILE"] = str(self.directory / "postgres/migrator.dsn")
            invoke(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "alembic",
                    "-c",
                    str(self.root / "alembic.ini"),
                    "upgrade",
                    "head",
                ],
                environment=environment,
                cwd=self.root,
                timeout=120,
            )
            write_new(
                receipt,
                json.dumps({"schema": "lab-profile-ledger.v1", "identity": self.identity}) + "\n",
            )
        elif json.loads(private_read(receipt)) != {
            "schema": "lab-profile-ledger.v1",
            "identity": self.identity,
        }:
            raise ValueError("database provisioning receipt changed")
        self.check_queue()

    def _initialize_roles(self) -> None:
        """Provision existing role boundaries only on this profile's fresh database."""
        import psycopg
        from psycopg import sql

        with psycopg.connect(
            host="127.0.0.1",
            port=self.database_port,
            dbname="swapp_lab",
            user="swapp_lab_admin",
            connect_timeout=5,
            password=private_read(self.directory / "postgres/admin.secret").strip(),
            autocommit=True,
        ) as connection:
            for role, username in ROLES.items():
                present = connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (username,)
                ).fetchone()
                action = "ALTER" if present else "CREATE"
                connection.execute(
                    sql.SQL(
                        action + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOINHERIT CONNECTION LIMIT 10 PASSWORD {}"
                    ).format(
                        sql.Identifier(username),
                        sql.Literal(
                            private_read(self.directory / f"postgres/{role}.secret").strip()
                        ),
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE swapp_lab TO {}").format(
                        sql.Identifier(username)
                    )
                )
            migrator = sql.Identifier(ROLES["migrator"])
            connection.execute(sql.SQL("GRANT CREATE ON DATABASE swapp_lab TO {}").format(migrator))
            for schema in ("lab", "scorer"):
                connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                        sql.Identifier(schema), migrator
                    )
                )

    def inspect_unit(self, component: str) -> dict[str, str]:
        from ops.start_lab import _process_identity, inspect_unit

        unit = self.unit(component)
        installed = Path.home() / ".config/systemd/user" / unit
        if installed.exists() or installed.is_symlink():
            if private_read(installed) != self.service_text(component):
                raise ValueError("installed profile unit differs")
        state = inspect_unit(unit)
        if state.get("LoadState") == "not-found":
            return state
        if state.get("LoadState") != "loaded" or state.get("WorkingDirectory") != str(self.root):
            raise ValueError("profile service checkout identity differs")
        if private_read(installed) != self.service_text(component):
            raise ValueError("installed profile unit differs")
        definition = dict(
            line.split("=", 1) for line in self.service_text(component).splitlines() if "=" in line
        )
        memory = definition["MemoryMax"]
        expected_command = definition["ExecStart"]
        expected_caps = {
            "MemoryMax": str(int(memory[:-1]) * 1024**2),
            "MemorySwapMax": "0",
            "CPUQuotaPerSecUSec": "500ms" if component == "api" else "250ms",
            "TasksMax": definition["TasksMax"],
            "Restart": "on-failure",
            "RestartUSec": "5s",
            "DropInPaths": "",
            "EnvironmentFiles": f"{self.directory / (component + '.env')} (ignore_errors=no)",
            "FragmentPath": str(installed),
            "Environment": "",
        }
        execution = re.fullmatch(
            r"\{ path=([^ ;]+) ; argv\[\]=(.*?) ; ignore_errors=no ; [^{}]* \}",
            state.get("ExecStart", ""),
        )
        if (
            execution is None
            or execution.group(1) != expected_command.split()[0]
            or execution.group(2) != expected_command
            or any(state.get(key) != value for key, value in expected_caps.items())
        ):
            raise ValueError("profile loaded command/environment/resource limits differ")
        if state.get("ActiveState") not in {"active", "inactive", "failed"}:
            raise ValueError("profile service is in a transition")
        if state["ActiveState"] == "active":
            cwd, argv = _process_identity(state.get("MainPID", ""))
            expected = (
                self.service_text(component).split("ExecStart=", 1)[1].splitlines()[0].split()
            )
            if cwd != self.root or argv != expected:
                raise ValueError("profile process identity differs")
            actual_environment = dict(
                item.split("=", 1)
                for item in Path(f"/proc/{state['MainPID']}/environ")
                .read_bytes()
                .decode()
                .split("\x00")
                if "=" in item
            )
            expected_environment = dict(
                line.split("=", 1) for line in self._environment(component).splitlines()
            )
            if any(
                actual_environment.get(key) != value.strip('"')
                for key, value in expected_environment.items()
            ):
                raise ValueError("profile process environment differs")
        return state

    def start(self) -> None:
        """Start exact persistent profile units without enabling login/boot startup."""
        from ops.start_lab import wait_http

        if not os.environ.get("XDG_RUNTIME_DIR") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise ValueError("profile startup requires the current user's desktop systemd session")
        for executable in ("docker", "systemctl"):
            if shutil.which(executable) is None:
                raise ValueError("required profile executable is absent")
        self.validate()
        if not (self.root / "console/web/dist/index.html").is_file():
            raise ValueError("console build is absent; build console/web before starting")
        # Verify every service and occupied port before any component can be started.
        for component, port in (
            ("api", self.api_port),
            ("director-drain", None),
            ("console", self.console_port),
        ):
            state = self.inspect_unit(component)
            if state.get("ActiveState") != "active" and port is not None:
                check_free_port(port)
        budget = self.resource_admission()
        if not budget["admitted"]:
            raise ValueError("profile RAM/disk reserve is insufficient; inspect --check budgets")
        self.provision_database()
        unit_directory = Path.home() / ".config/systemd/user"
        unit_directory.mkdir(parents=True, exist_ok=True)
        for component in ("api", "director-drain", "console"):
            target = unit_directory / self.unit(component)
            expected = self.service_text(component)
            if target.exists() or target.is_symlink():
                if private_read(target) != expected:
                    raise ValueError("refusing to replace another service definition")
            else:
                write_new(target, expected)
        invoke(["systemctl", "--user", "daemon-reload"])
        for component in ("api", "director-drain", "console"):
            state = self.inspect_unit(component)
            if state.get("ActiveState") != "active":
                invoke(["systemctl", "--user", "start", self.unit(component)])
            if component == "api":
                wait_http(f"http://127.0.0.1:{self.api_port}/health")
            elif component == "console":
                wait_http(
                    f"http://127.0.0.1:{self.console_port}/console-api/overview", console=True
                )
        print(f"CPU Lab: http://127.0.0.1:{self.console_port}")

    def resource_admission(self) -> dict:
        """Reserve the existing bounded CPU pipeline and host RAM before mutations."""
        from lab.llm.native_runtime import HOST_RAM_RESERVE_BYTES
        from lab.sandbox.docker_runner import MIN_FREE_DISK_BYTES, SandboxProfile
        from lab.scorer.supervisor import SLICE_MEMORY_BYTES

        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
            if ":" in line
        )
        available = int(memory["MemAvailable"].split()[0]) * 1024
        services = 0
        for component, cap in (("api", 768), ("director-drain", 512), ("console", 384)):
            if self.inspect_unit(component).get("ActiveState") != "active":
                services += cap * 1024**2
        database = self.inspect_database()
        if database is None or database["State"]["Status"] != "running":
            services += 512 * 1024**2
        # One existing run owner (2 GiB), Scorer aggregate slice and its Docker phase.
        # These are conservative concurrent ceilings, not measured consumption.
        children = 2 * 1024**3 + SLICE_MEMORY_BYTES + SandboxProfile().memory_bytes
        required = services + children + HOST_RAM_RESERVE_BYTES
        free_disk = shutil.disk_usage(self.root / "data/runtime").free
        return {
            "admitted": available >= required and free_disk >= MIN_FREE_DISK_BYTES,
            "mem_available_bytes": available,
            "inactive_service_caps_bytes": services,
            "cpu_pipeline_caps_bytes": children,
            "host_reserve_bytes": HOST_RAM_RESERVE_BYTES,
            "required_available_bytes": required,
            "free_disk_bytes": free_disk,
            "disk_reserve_bytes": MIN_FREE_DISK_BYTES,
            "profile_cpu_ceiling_cores": 5.5,
            "source": "existing host reserve, Scorer slice and sandbox caps",
        }

    def stop(self) -> None:
        """Stop an idle owned profile while preserving its ledger and artefacts."""
        self.validate()
        for component in ("api", "director-drain", "console"):
            self.inspect_unit(component)
        database = self.inspect_database()
        if database is not None and database["State"]["Status"] == "running":
            queue = self.check_queue()
            if any(queue.values()):
                raise ValueError(
                    "profile has nonterminal runs; stop them in the console and "
                    "wait for terminal reports before --stop"
                )
        elif any(
            self.inspect_unit(component).get("ActiveState") == "active"
            for component in ("api", "director-drain", "console")
        ):
            raise ValueError("cannot prove an idle profile while its database is unavailable")
        # Close admission, then recheck. A request admitted between the first proof
        # and API shutdown keeps its worker alive and prevents database shutdown.
        for component in ("console", "api"):
            if self.inspect_unit(component).get("ActiveState") == "active":
                invoke(["systemctl", "--user", "stop", self.unit(component)])
        if database is not None and database["State"]["Status"] == "running":
            if any(self.check_queue().values()):
                raise ValueError(
                    "a run arrived during shutdown; its worker remains active. "
                    "Restart the profile and stop that run in the console"
                )
        if self.inspect_unit("director-drain").get("ActiveState") == "active":
            invoke(["systemctl", "--user", "stop", self.unit("director-drain")])
        if database is not None and database["State"]["Status"] == "running":
            invoke(["docker", "stop", "--time", "20", database["Id"]])
        print("Owned profile stopped; ledger, reports and configuration are retained.")


def check_free_port(port: int) -> None:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise ValueError(f"profile port is occupied: {port}")

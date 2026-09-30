"""Fixed-origin, local-principal-only client for the existing Lab API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from lab.api.registry import SuiteRegistry, load_principals, load_suite_registry

ROOT = Path(__file__).resolve().parents[1]
MAX_HEALTH_BODY_BYTES = 4096
CONFIGURATION_ERROR = (
    "Lab API configuration is invalid; check the loopback URL, private principal, "
    "and trusted suite registry."
)


def validate_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise ValueError("Lab API URL must be an explicit loopback HTTP origin")
    return f"http://{parsed.netloc}"


def _read_local_token(path_value: str | None) -> str:
    if not path_value:
        raise ValueError("LAB_CONSOLE_TOKEN_FILE is not configured")
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Lab API principal file is not a regular private file")
    metadata = path.stat()
    if metadata.st_mode & 0o077 or metadata.st_size > 64 * 1024:
        raise ValueError("Lab API principal file permissions or size are invalid")
    principals = load_principals(path)
    local = [principal for principal in principals if principal.origin == "local"]
    if len(local) != 1:
        raise ValueError("exactly one local Lab API principal must be configured")
    return local[0].token


class Upstream:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        token_file: str | None = None,
        suite_registry_file: str | None = None,
        runtime_root: Path | None = None,
        model_runs_enabled: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url_value = api_url if api_url is not None else os.environ.get("LAB_CONSOLE_API_URL")
        self._token_file = (
            token_file if token_file is not None else os.environ.get("LAB_CONSOLE_TOKEN_FILE")
        )
        self._registry_file = (
            suite_registry_file
            if suite_registry_file is not None
            else os.environ.get("LAB_CONSOLE_SUITE_REGISTRY_FILE")
        )
        if model_runs_enabled is None:
            self.model_runs_enabled = os.environ.get("MODEL_RUNS_ENABLED", "false").casefold() in {
                "1",
                "true",
                "yes",
            }
        else:
            self.model_runs_enabled = model_runs_enabled
        self._client: httpx.Client | None = None
        self._error: str | None = None
        self.url: str | None = None
        self.token: str | None = None
        self.registry: SuiteRegistry | None = None
        if self._url_value is None:
            self._error = "Lab API is not configured"
            return
        try:
            self.url = validate_loopback_url(self._url_value)
            self.token = _read_local_token(self._token_file)
            if not self._registry_file:
                raise ValueError("LAB_CONSOLE_SUITE_REGISTRY_FILE is not configured")
            self.registry = load_suite_registry(
                Path(self._registry_file).expanduser(), runtime_root or ROOT / "data/runtime"
            )
            self._client = httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(2.0, connect=0.75),
                follow_redirects=False,
                trust_env=False,
            )
        except (OSError, ValueError, ValidationError):
            # Parser exceptions may embed rejected values, including credential text.
            self._error = CONFIGURATION_ERROR

    @property
    def configured(self) -> bool:
        return self._error is None and self.url is not None and self.registry is not None

    def health(self) -> tuple[bool, str | None]:
        if self._error:
            return False, self._error
        if self._client is None or self.url is None:
            return False, "Lab API configuration is unavailable"
        try:
            with self._client.stream("GET", f"{self.url}/health") as response:
                if response.status_code != 200:
                    return False, f"Lab API health returned HTTP {response.status_code}"
                content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0]
                if content_type.strip().lower() != "application/json":
                    return False, "Lab API health response was not JSON"
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        return False, "Lab API health response had an invalid size header"
                    if declared_length < 0 or declared_length > MAX_HEALTH_BODY_BYTES:
                        return False, "Lab API health response exceeded its size limit"
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=1):
                    remaining = MAX_HEALTH_BODY_BYTES + 1 - len(body)
                    body.extend(chunk[:remaining])
                    if len(body) > MAX_HEALTH_BODY_BYTES:
                        return False, "Lab API health response exceeded its size limit"
                try:
                    parsed_body = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    return False, "Lab API health response was invalid JSON"
                if parsed_body != {"status": "ok", "service": "lab-api"}:
                    return False, "Lab API health response did not match the expected service"
                return True, None
        except httpx.HTTPError as exc:
            return False, f"Lab API unreachable: {type(exc).__name__}"

    def suites(self) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        result: list[dict[str, Any]] = []
        for entry in self.registry.entries.values():
            result.append(
                {
                    "suite_id": entry.suite_id,
                    "track": entry.track,
                    "program_version": entry.program_version,
                    "provider": entry.provider,
                    "proposal_limit": entry.proposal_limit,
                }
            )
        return sorted(result, key=lambda row: row["suite_id"])

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None or self.url is None or self.token is None:
            raise HTTPException(status_code=503, detail=self._error or "Lab API is unavailable")
        skip_health = bool(kwargs.pop("_skip_health", False))
        if not skip_health:
            connected, reason = self.health()
            if not connected:
                raise HTTPException(status_code=503, detail=reason or "Lab API unavailable")
        try:
            response = self._client.request(
                method,
                f"{self.url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail=f"Lab API unreachable: {type(exc).__name__}"
            ) from None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "Lab API request failed")
            except (ValueError, AttributeError):
                detail = "Lab API request failed"
            raise HTTPException(status_code=response.status_code, detail=str(detail))
        return response

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

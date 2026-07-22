"""Shared utilities for the vitrine package.

Deduplicates common patterns used across multiple modules:
PID checks, directory resolution, path escaping, health checks,
and file-type constants.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SERVER_BIND_HOST = "127.0.0.1"
SUPPORTED_DISPLAY_HOSTS: frozenset[str] = frozenset(
    {
        SERVER_BIND_HOST,
        "localhost",
        "vitrine.localhost",
    }
)


class ServerMetadataError(RuntimeError):
    """Raised when persistent server metadata cannot be trusted."""


class ServerRestartRequired(ServerMetadataError):
    """Raised when metadata predates the current lifecycle contract."""


def package_version() -> str:
    """Return the installed Vitrine package version."""
    try:
        return importlib.metadata.version("vitrine")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _metadata_error(pid_path: Path, detail: str) -> ServerMetadataError:
    return ServerMetadataError(f"Invalid server metadata at {pid_path}: {detail}")


def _require_nonempty_string(info: dict[str, Any], key: str, pid_path: Path) -> str:
    value = info.get(key)
    if not isinstance(value, str) or not value:
        raise _metadata_error(pid_path, f"{key} must be a non-empty string")
    return value


def load_server_metadata(pid_path: Path) -> dict[str, Any]:
    """Read and strictly validate a project's persistent server metadata.

    Validation happens before callers probe a PID or make an HTTP request.
    Only the fixed loopback API origin and the metadata file's own project
    directory are accepted. Older metadata lacks enough information to prove
    daemon version and project ownership, so it requires a manual restart.
    """
    try:
        raw = json.loads(pid_path.read_text())
    except json.JSONDecodeError as exc:
        raise _metadata_error(pid_path, "invalid JSON") from exc
    except OSError as exc:
        raise _metadata_error(pid_path, str(exc)) from exc

    if not isinstance(raw, dict):
        raise _metadata_error(pid_path, "top-level value must be an object")
    info: dict[str, Any] = raw

    pid = info.get("pid")
    if type(pid) is not int or pid <= 0:
        raise _metadata_error(pid_path, "pid must be a positive integer")

    port = info.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise _metadata_error(pid_path, "port must be an integer from 1 to 65535")

    host = _require_nonempty_string(info, "host", pid_path)
    if host != SERVER_BIND_HOST:
        raise _metadata_error(pid_path, f"host must be {SERVER_BIND_HOST}")

    _require_nonempty_string(info, "session_id", pid_path)
    _require_nonempty_string(info, "token", pid_path)

    url = _require_nonempty_string(info, "url", pid_path)
    try:
        parsed_url = urlsplit(url)
        display_host = parsed_url.hostname
        display_port = parsed_url.port
    except ValueError as exc:
        raise _metadata_error(pid_path, "url is malformed") from exc
    expected_url = (
        f"http://{display_host}:{port}"
        if display_host in SUPPORTED_DISPLAY_HOSTS
        else None
    )
    if (
        parsed_url.scheme != "http"
        or display_port != port
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path != ""
        or parsed_url.query != ""
        or parsed_url.fragment != ""
        or url != expected_url
    ):
        raise _metadata_error(
            pid_path, "url must be an exact supported loopback origin"
        )

    current_fields = ("api_url", "data_dir", "version")
    missing = [field for field in current_fields if field not in info]
    if missing:
        fields = ", ".join(missing)
        raise ServerRestartRequired(
            "Server restart required: metadata predates the managed lifecycle "
            f"contract (missing {fields}) at {pid_path}"
        )

    api_url = _require_nonempty_string(info, "api_url", pid_path)
    expected_api_url = f"http://{SERVER_BIND_HOST}:{port}"
    if api_url != expected_api_url:
        raise _metadata_error(pid_path, f"api_url must be {expected_api_url}")

    data_dir = _require_nonempty_string(info, "data_dir", pid_path)
    data_path = Path(data_dir)
    if not data_path.is_absolute() or data_path.resolve() != pid_path.parent.resolve():
        raise _metadata_error(
            pid_path, "data_dir must match the metadata file's project directory"
        )

    version = _require_nonempty_string(info, "version", pid_path)
    if version == "unknown":
        raise ServerRestartRequired(
            "Server restart required: daemon version is unknown in metadata "
            f"at {pid_path}"
        )

    return info


# ---------------------------------------------------------------------------
# PID check
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Cross-platform file locking
# ---------------------------------------------------------------------------


def lock_file(fd: Any, exclusive: bool = True, blocking: bool = True) -> None:
    """Acquire a file lock. Works on Unix (fcntl) and Windows (msvcrt)."""
    if sys.platform == "win32":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        # msvcrt.locking operates on the file descriptor's current position
        # Lock 1 byte at position 0
        fd.seek(0)
        msvcrt.locking(fd.fileno(), mode, 1)
    else:
        import fcntl

        if exclusive:
            op = fcntl.LOCK_EX
        else:
            op = fcntl.LOCK_SH
        if not blocking:
            op |= fcntl.LOCK_NB
        fcntl.flock(fd, op)


def unlock_file(fd: Any) -> None:
    """Release a file lock."""
    if sys.platform == "win32":
        import msvcrt

        fd.seek(0)
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Cross-platform subprocess detach kwargs
# ---------------------------------------------------------------------------


def detached_popen_kwargs() -> dict[str, Any]:
    """Return Popen kwargs for detaching a subprocess from the parent."""
    if sys.platform == "win32":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS}
    return {"start_new_session": True}


def terminate_spawned_process(process: Any, timeout: float = 3.0) -> None:
    """Terminate and reap exactly the child represented by ``process``.

    The retained ``Popen`` handle is the authority. No PID-file process is
    signalled, which avoids killing a concurrent or unrelated daemon.
    """
    returncode = process.poll()
    if returncode is not None:
        process.wait()
        return

    try:
        process.terminate()
    except OSError:
        if process.poll() is None:
            raise
        process.wait()
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            if process.poll() is None:
                raise
        process.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Vitrine directory resolution
# ---------------------------------------------------------------------------


def get_vitrine_dir() -> Path:
    """Resolve the vitrine directory.

    Resolution order:
    1. ``VITRINE_DATA_DIR`` environment variable (explicit override)
    2. Walk up from cwd looking for an existing ``.vitrine/`` directory
    3. Default: ``cwd / ".vitrine"``

    Returns the path without performing migration (caller handles that).
    """
    env = os.getenv("VITRINE_DATA_DIR")
    if env:
        return Path(env)
    # Walk up from cwd looking for existing .vitrine/
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".vitrine"
        if candidate.exists():
            return candidate
    # Default: cwd / ".vitrine"
    return cwd / ".vitrine"


# ---------------------------------------------------------------------------
# DuckDB path escaping
# ---------------------------------------------------------------------------


def duckdb_safe_path(path: Path | str) -> str:
    """Escape a file path for safe interpolation into DuckDB SQL string literals.

    Single quotes in the path are doubled to prevent SQL injection.
    """
    return str(path).replace("'", "''")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def health_check(
    url: str,
    session_id: str | None = None,
    *,
    version: str | None = None,
    data_dir: str | None = None,
) -> bool:
    """GET /api/health and optionally validate daemon identity."""
    try:
        import urllib.request

        req = urllib.request.Request(f"{url}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if data.get("status") != "ok":
                return False
            if session_id is not None and data.get("session_id") != session_id:
                return False
            if version is not None and data.get("version") != version:
                return False
            if data_dir is not None and data.get("data_dir") != data_dir:
                return False
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# File-type constants
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".sql",
        ".r",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".txt",
        ".cfg",
        ".log",
        ".sh",
        ".bash",
        ".ini",
        ".env",
    }
)

IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


# ---------------------------------------------------------------------------
# Option description resolution
# ---------------------------------------------------------------------------


def resolve_option_descriptions(
    values: dict[str, Any],
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    """Cross-reference selected values with field specs to get option descriptions.

    For single-select fields, returns::

        {"field_name": {"selected": "label", "description": "desc"}}

    For multi-select fields, returns::

        {"field_name": {"selected": ["a", "b"], "descriptions": ["desc_a", "desc_b"]}}

    Fields whose selected value doesn't match any option (e.g. "Other" free-text)
    are included with an empty description.

    Args:
        values: Submitted form values (``{field_name: label_or_list}``).
        fields: Field specs from ``card.preview["fields"]``.

    Returns:
        Dict mapping field names to enriched selection dicts.
    """
    # Build a lookup: field_name -> {label: description}
    field_options: dict[str, dict[str, str]] = {}
    for f in fields:
        name = f.get("name", "")
        opts = f.get("options", [])
        label_to_desc: dict[str, str] = {}
        for opt in opts:
            if isinstance(opt, dict):
                label_to_desc[opt.get("label", "")] = opt.get("description", "")
            elif isinstance(opt, str):
                label_to_desc[opt] = ""
        field_options[name] = label_to_desc

    result: dict[str, Any] = {}
    for field_name, selected in values.items():
        descs = field_options.get(field_name, {})
        if isinstance(selected, list):
            result[field_name] = {
                "selected": selected,
                "descriptions": [descs.get(s, "") for s in selected],
            }
        else:
            result[field_name] = {
                "selected": selected,
                "description": descs.get(selected, "") if selected else "",
            }
    return result

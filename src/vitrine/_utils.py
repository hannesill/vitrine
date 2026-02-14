"""Shared utilities for the vitrine package.

Deduplicates common patterns used across multiple modules:
PID checks, directory resolution, path escaping, health checks,
and file-type constants.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def health_check(url: str, session_id: str | None = None) -> bool:
    """GET /api/health and optionally validate session_id matches."""
    try:
        import urllib.request

        req = urllib.request.Request(f"{url}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if data.get("status") != "ok":
                return False
            if session_id is not None:
                return data.get("session_id") == session_id
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

"""Shared utilities for the vitrine package.

Deduplicates common patterns used across multiple modules:
PID checks, directory resolution, path escaping, health checks,
and file-type constants.
"""

from __future__ import annotations

import functools
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
HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
PROCESS_REAP_TIMEOUT_SECONDS = 3.0
OWNER_PID_ENV = "VITRINE_OWNER_PID"
OWNER_CHECK_INTERVAL_SECONDS = 2.0
# PIDs are signed 32-bit numbers on POSIX; larger ones overflow os.kill.
MAX_PID = 2**31 - 1


class ServerMetadataError(RuntimeError):
    """Raised when persistent server metadata cannot be trusted."""


class ServerRestartRequired(ServerMetadataError):
    """Raised when metadata predates the current lifecycle contract."""


class OwnerRejected(RuntimeError):
    """Raised when a server does not take a process as an owner.

    ``reason`` is ``"not_running"`` when the process is not running and
    ``"stopping"`` when the server is stopping because its owners exited.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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

    # Servers before owners were added write no "owners"; they have none.
    owners = info.get("owners", [])
    if not isinstance(owners, list) or not all(_is_owner(o) for o in owners):
        raise _metadata_error(
            pid_path, "owners must be a list of objects with a positive integer pid"
        )

    return info


def _is_owner(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    pid = value.get("pid")
    start_time = value.get("start_time")
    return (
        type(pid) is int and pid > 0 and (start_time is None or type(start_time) is int)
    )


def owner_pids(info: dict[str, Any]) -> list[int]:
    """The PIDs of the processes a server stops with, from its metadata."""
    return [owner["pid"] for owner in info.get("owners", [])]


def parse_owner_pid(value: str | None) -> int | None:
    """Read an owner PID as given to ``--owner-pid`` or in ``VITRINE_OWNER_PID``.

    Empty means no owner. Anything but a positive integer that fits a PID
    raises ``ValueError``: a host that names an owner by mistake should hear
    about it, not get a server that never stops.
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) <= MAX_PID:
        raise ValueError(f"An owner PID must be a positive integer, not {value!r}.")
    return int(text)


# ---------------------------------------------------------------------------
# PID check
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but may not be signalled from here, as from
        # inside an agent's sandbox. Treating that as dead discarded a
        # running server's metadata and left the server orphaned.
        return True
    except (OSError, OverflowError):
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # As with EPERM elsewhere: the process exists but may not be opened
        # from here. A PID that is not in use fails with another error.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        # A process that has exited can still be opened while anyone holds a
        # handle to it, such as the parent that started it, so ask whether it
        # still runs. (One that exited with code 259 reads as running.)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_start_time(pid: int) -> int | None:
    """When process ``pid`` started, as a number that is only good for comparing.

    With the PID it tells a process from a later one that was given the same
    PID. Read from ``/proc`` on Linux, ``sysctl`` on macOS and
    ``GetProcessTimes`` on Windows. ``None`` where the platform does not say,
    the process is not there, or reading it fails: callers then fall back to
    the PID alone.
    """
    record = _process_record(pid)
    return None if record is None else record[0]


def is_same_process_alive(pid: int, start_time: int | None) -> bool:
    """Whether ``pid`` runs and is still the process that started at ``start_time``.

    A process that has exited but is not reaped yet (a zombie) does not run.
    Without a ``start_time``, or when the current one cannot be read, the PID
    alone decides.
    """
    if not is_pid_alive(pid):
        return False
    record = _process_record(pid)
    if record is None:
        return True
    current, exited = record
    if exited:
        return False
    return start_time is None or current == start_time


def _process_record(pid: int) -> tuple[int, bool] | None:
    """``(start time, exited)`` of process ``pid``, or ``None`` if unknown."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_process(pid)
        if sys.platform == "darwin":
            return _darwin_process(pid)
        if sys.platform == "win32":
            # is_pid_alive already tells an exited process on Windows.
            start_time = _windows_start_time(pid)
            return None if start_time is None else (start_time, False)
    except Exception:
        return None
    return None


def _linux_process(pid: int, proc: Path = Path("/proc")) -> tuple[int, bool]:
    stat = (proc / str(pid) / "stat").read_text()
    # The command name before the fields is in parentheses and may hold
    # spaces and parentheses itself, so count from the last one.
    fields = stat[stat.rindex(")") + 2 :].split()
    # Field 3 is the state, field 22 the start time in clock ticks after boot.
    state, start_time = fields[3 - 3], int(fields[22 - 3])
    return start_time, state in {"Z", "X", "x"}


@functools.cache
def _darwin_sysctl() -> Any:
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    sysctl = libc.sysctl
    sysctl.argtypes = (
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    sysctl.restype = ctypes.c_int
    return sysctl


def _darwin_process(pid: int) -> tuple[int, bool] | None:
    import ctypes
    import struct

    ctl_kern, kern_proc, kern_proc_pid = 1, 14, 1
    mib = (ctypes.c_int * 4)(ctl_kern, kern_proc, kern_proc_pid, pid)
    buffer = ctypes.create_string_buffer(1024)  # struct kinfo_proc is 648 bytes
    size = ctypes.c_size_t(ctypes.sizeof(buffer))
    if _darwin_sysctl()(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value == 0:  # no such process
        return None
    # struct kinfo_proc starts with struct extern_proc: p_starttime, a struct
    # timeval, at offset 0, and the state p_stat at offset 36.
    seconds, microseconds = struct.unpack_from("=qi", buffer.raw)
    zombie = 5  # SZOMB
    return seconds * 1_000_000 + microseconds, buffer.raw[36] == zombie


@functools.cache
def _windows_kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    # An instance of our own, so these prototypes do not change ctypes.windll.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        *(ctypes.POINTER(wintypes.FILETIME),) * 4,
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_start_time(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


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


def terminate_spawned_process(
    process: Any, timeout: float = PROCESS_REAP_TIMEOUT_SECONDS
) -> None:
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


def _is_bare_repository(git_dir: Path) -> bool:
    """Whether the repository at ``git_dir`` is bare, by ``core.bare`` in its config.

    Read from the file, like the rest of resolution. A bare repository can sit
    in a directory named ``.git`` (``git clone --bare <url> project/.git``), so
    the name alone does not tell.
    """
    try:
        lines = (git_dir / "config").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    section = ""
    for raw in lines:
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
        elif section == "core" and "=" in line:
            key, value = (part.strip().lower() for part in line.split("=", 1))
            if key == "bare":
                return value in {"true", "yes", "on", "1"}
    return False


def _linked_worktree(start: Path) -> tuple[Path, Path] | None:
    """Find the linked git worktree holding ``start`` and its main checkout.

    A linked worktree's root holds a ``.git`` *file* naming its private git
    directory, whose ``commondir`` file names the repository's shared one at
    ``<main checkout>/.git``. Both are read directly, so resolution spawns no
    process and works where git is not installed.

    Returns ``(worktree root, main checkout root)``, or ``None`` in a main
    checkout, outside a repository, in a submodule, and for worktrees of a
    bare repository, which has no main checkout.
    """
    for directory in [start, *start.parents]:
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return None
        if not dot_git.is_file():
            continue
        try:
            pointer = dot_git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = (directory / pointer[len("gitdir:") :].strip()).resolve()
            common_dir = (
                git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
            ).resolve()
        except (OSError, UnicodeDecodeError):
            return None
        if common_dir.name != ".git" or _is_bare_repository(common_dir):
            return None
        return (directory, common_dir.parent)
    return None


def _nearest_vitrine_dir(start: Path, stop: Path | None = None) -> Path | None:
    """Find an existing ``.vitrine/`` in ``start`` or its parents, up to ``stop``."""
    for directory in [start, *start.parents]:
        candidate = directory / ".vitrine"
        if candidate.exists():
            return candidate
        if directory == stop:
            break
    return None


_STORE_GITIGNORE = "# Written by vitrine: study data stays out of version control.\n*\n"


def keep_out_of_git(vitrine_dir: Path) -> None:
    """Make git ignore the study store.

    The store holds the rows behind every card, which can be patient-level,
    and the running server's access token. A ``.gitignore`` of ``*`` inside
    the directory ignores everything in it, itself included: the store does
    not show up as untracked, ``git add -A`` does not sweep it into a commit
    (tools that snapshot a project folder do exactly that), and
    ``git clean -fd`` leaves the cards alone. A ``.gitignore`` that is already
    there is the user's and is left as it is.
    """
    path = vitrine_dir / ".gitignore"
    if path.exists():
        return
    try:
        vitrine_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(_STORE_GITIGNORE)
    except OSError:
        # A read-only store still works; it is just not protected.
        pass


def browser_opening_enabled() -> bool:
    """Whether starting a server may open the system browser.

    ``VITRINE_OPEN_BROWSER=0`` turns it off for a whole environment. A host
    application that shows the board itself sets it, so that an agent's first
    ``show()`` does not also open the board in a separate browser.
    """
    value = os.getenv("VITRINE_OPEN_BROWSER", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def get_vitrine_dir() -> Path:
    """Resolve the vitrine directory.

    Resolution order:
    1. ``VITRINE_DATA_DIR`` environment variable (explicit override)
    2. Walk up from cwd looking for an existing ``.vitrine/`` directory
    3. Default: ``cwd / ".vitrine"``

    A linked git worktree that has no ``.vitrine/`` of its own resolves as if
    run from its main checkout's root, so every worktree of a repository
    shares one study store. Its search for its own store stops at the
    worktree's root: a store further up belongs to whatever directory happens
    to hold the worktrees.

    Returns the path without performing migration (caller handles that).
    """
    env = os.getenv("VITRINE_DATA_DIR")
    if env:
        return Path(env)
    cwd = Path.cwd()
    linked = _linked_worktree(cwd)
    if linked is not None:
        worktree_root, main_checkout = linked
        own = _nearest_vitrine_dir(cwd, stop=worktree_root)
        if own is not None:
            return own
        cwd = main_checkout
    return _nearest_vitrine_dir(cwd) or cwd / ".vitrine"


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
        with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as resp:
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

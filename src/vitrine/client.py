"""Client infrastructure: server lifecycle, discovery, remote comms, migration.

All server interaction plumbing lives here. Mutable state is in ``_state.py``
and imported here for convenience. The public API in ``__init__.py`` imports
and uses these functions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

# Shared mutable state — imported so client functions can read/write it
import vitrine._state as _st
from vitrine._types import DisplayEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vitrine directory & migration
# ---------------------------------------------------------------------------


def _get_vitrine_dir() -> Path:
    """Resolve the vitrine directory.

    Uses the standalone vitrine._utils.get_vitrine_dir() helper which checks
    VITRINE_DATA_DIR env var, walks up from cwd for .vitrine/, or defaults
    to cwd/.vitrine.

    Performs one-time migration from the old M4_DATA_DIR/vitrine/ location.
    """
    from vitrine._utils import get_vitrine_dir

    vitrine_dir = get_vitrine_dir()
    _migrate_if_needed(vitrine_dir)
    return vitrine_dir


def _migrate_if_needed(vitrine_dir: Path) -> None:
    """Migrate storage from old layout to new layout if needed."""
    # 1. Move legacy M4_DATA_DIR/vitrine/ -> .vitrine/ (same parent)
    # Check M4_DATA_DIR env var for the old data directory
    old_dir = None
    m4_data = os.getenv("M4_DATA_DIR")
    if m4_data:
        old_dir = Path(m4_data) / "vitrine"

    if old_dir and old_dir.exists() and not vitrine_dir.exists():
        try:
            shutil.move(str(old_dir), str(vitrine_dir))
            logger.debug(f"Migrated {old_dir} -> {vitrine_dir}")
        except OSError:
            logger.debug(f"Failed to migrate {old_dir} -> {vitrine_dir}")
            return

    if not vitrine_dir.exists():
        return

    # 2. Rename runs/ -> studies/
    old_runs_dir = vitrine_dir / "runs"
    new_studies_dir = vitrine_dir / "studies"
    if old_runs_dir.exists() and not new_studies_dir.exists():
        try:
            old_runs_dir.rename(new_studies_dir)
            logger.debug("Migrated runs/ -> studies/")
        except OSError:
            pass

    # 3. Remove legacy registry files (runs.json / studies.json)
    for legacy in ("runs.json", "studies.json"):
        legacy_path = vitrine_dir / legacy
        if legacy_path.exists():
            try:
                legacy_path.unlink()
                logger.debug(f"Removed legacy {legacy}")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# PID file & process helpers
# ---------------------------------------------------------------------------


def _pid_file_path() -> Path:
    """Return the path to the server PID file."""
    return _get_vitrine_dir() / ".server.json"


def _lock_file_path() -> Path:
    """Return the path to the server lock file."""
    return _get_vitrine_dir() / ".server.lock"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    from vitrine._utils import is_pid_alive

    return is_pid_alive(pid)


def _health_check(url: str, expected_session_id: str) -> bool:
    """GET /api/health and validate session_id matches."""
    from vitrine._utils import health_check

    return health_check(url, session_id=expected_session_id)


# ---------------------------------------------------------------------------
# Server discovery
# ---------------------------------------------------------------------------


def _discover_server() -> dict[str, Any] | None:
    """Read PID file, validate process and health, return server info or None.

    Cleans up stale PID files automatically.
    """
    pid_path = _pid_file_path()
    if not pid_path.exists():
        return None

    try:
        info = json.loads(pid_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    pid = info.get("pid")
    session_id = info.get("session_id")
    url = info.get("url")
    host = info.get("host", "127.0.0.1")
    port = info.get("port")

    if not all([pid, session_id, url]):
        return None

    # Check if process is alive
    if not _is_process_alive(pid):
        logger.debug(f"Stale PID file (pid={pid} not alive), removing")
        try:
            pid_path.unlink()
        except OSError:
            pass
        return None

    # Build an API-safe URL from host:port.  The "url" field uses
    # vitrine.localhost which Python's urllib can't always resolve,
    # so all programmatic access must go through 127.0.0.1.
    api_url = f"http://{host}:{port}" if port else url
    if not _health_check(api_url, session_id):
        logger.debug(f"Health check failed for {api_url}, removing stale PID file")
        try:
            pid_path.unlink()
        except OSError:
            pass
        return None

    info["api_url"] = api_url
    return info


# ---------------------------------------------------------------------------
# Remote comms
# ---------------------------------------------------------------------------


def _remote_command(url: str, token: str, payload: dict[str, Any]) -> bool:
    """POST /api/command with Bearer auth. Returns True on success."""
    try:
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{url}/api/command",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        logger.warning(f"Remote command failed for {url}")
        return False


def _push_remote(card_data: dict[str, Any]) -> bool:
    """Push a card to the remote server. Returns True on success."""

    with _st._lock:
        url, token = _st._remote_url, _st._auth_token

    if url is None or token is None:
        return False

    ok = _remote_command(url, token, {"type": "card", "card": card_data})
    if not ok:
        # Retry once after re-discovery
        with _st._lock:
            _st._remote_url = None
            _st._auth_token = None
        info = _discover_server()
        if info:
            with _st._lock:
                _st._remote_url = info.get("api_url", info["url"])
                _st._auth_token = info.get("token")
                url, token = _st._remote_url, _st._auth_token
            if url is None or token is None:
                logger.warning(
                    "Remote card push: re-discovery returned no URL or token"
                )
                return False
            ok = _remote_command(url, token, {"type": "card", "card": card_data})
            if not ok:
                logger.warning("Remote card push failed after re-discovery")
        else:
            logger.warning("Remote card push failed and server re-discovery failed")
    return ok


def _poll_remote_response(card_id: str, timeout: float) -> dict[str, Any]:
    """Poll the remote server for a blocking response via long-poll."""
    import urllib.error
    import urllib.request

    with _st._lock:
        url, token = _st._remote_url, _st._auth_token

    poll_url = f"{url}/api/response/{card_id}?timeout={timeout}"
    try:
        req = urllib.request.Request(
            poll_url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"Remote response poll HTTP error {e.code} for card {card_id}")
        return {"action": "error", "card_id": card_id}
    except urllib.error.URLError as e:
        logger.warning(f"Remote response poll connection error for card {card_id}: {e}")
        return {"action": "error", "card_id": card_id}
    except Exception:
        logger.warning(f"Remote response poll unexpected error for card {card_id}")
        return {"action": "error", "card_id": card_id}


def _poll_remote_events() -> None:
    """Background thread that polls a remote server for UI events."""
    import urllib.request

    while not _st._event_poll_stop.is_set():
        with _st._lock:
            url, token = _st._remote_url, _st._auth_token

        if not url or not token:
            _st._event_poll_stop.wait(0.5)
            continue

        try:
            events_url = f"{url}/api/events"
            req = urllib.request.Request(
                events_url,
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                events = json.loads(resp.read())
            with _st._lock:
                callbacks = list(_st._event_callbacks)
            for evt_data in events:
                event = DisplayEvent(
                    event_type=evt_data.get("event_type", ""),
                    card_id=evt_data.get("card_id", ""),
                    payload=evt_data.get("payload", {}),
                )
                for cb in callbacks:
                    try:
                        cb(event)
                    except Exception:
                        logger.debug("Event callback error", exc_info=True)
        except Exception:
            logger.debug("Remote event poll error", exc_info=True)
        _st._event_poll_stop.wait(0.5)


# ---------------------------------------------------------------------------
# Study manager helper
# ---------------------------------------------------------------------------


def _get_session_dir() -> Path:
    """Determine the session directory for artifact storage."""
    return _get_vitrine_dir()


def _ensure_study_manager() -> Any:
    """Ensure a StudyManager exists for local artifact storage."""
    if _st._study_manager is None:
        from vitrine.study_manager import StudyManager

        _st._study_manager = StudyManager(_get_vitrine_dir())
    return _st._study_manager


# ---------------------------------------------------------------------------
# Session registration
# ---------------------------------------------------------------------------


def register_session(study: str | None = None) -> None:
    """Associate the current Claude Code session with a study.

    If CLAUDE_SESSION_ID is in the environment, stores it in the
    study's meta.json. Called automatically on first show() for a study.
    No-op if the env var is not set.
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        return
    sm = _ensure_study_manager()
    if sm is None:
        return
    if study is None:
        return
    sm.set_session_id(study, session_id)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _ensure_started(
    port: int = 7741,
    open_browser: bool = True,
) -> None:
    """Ensure the display server is running, starting it if needed.

    Discovery flow:
    1. If _st._remote_url set -> health check -> if healthy, return
    2. If in-process _st._server running -> return
    3. Acquire file lock
    4. Inside lock: _discover_server() -> _start_process()
    5. Release lock
    6. Fallback in-thread server if polling fails
    """
    from vitrine._utils import lock_file, unlock_file

    with _st._lock:
        # Fast path: already connected to remote
        if _st._remote_url is not None:
            info = _discover_server()
            if info and info.get("url") == _st._remote_url:
                return
            # Stale remote, clear it
            _st._remote_url = None
            _st._auth_token = None

        # In-process server running
        if _st._server is not None and _st._server.is_running:
            return

        # Ensure study manager exists for local artifact storage
        _ensure_study_manager()

        # Acquire cross-process file lock before discovery. Release it before
        # spawning so the child server can take the same lock deterministically.
        should_start_process = False
        lock_path = _lock_file_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            try:
                lock_file(lock_fd)

                # Try to discover an existing persistent server (PID file).
                # The PID file is the sole authority — no port scanning.
                # Port scanning would risk connecting to a different project's
                # server when multiple projects run vitrine concurrently.
                info = _discover_server()
                if info:
                    _st._remote_url = info.get("api_url", info["url"])
                    _st._auth_token = info.get("token")
                    _st._session_id = info["session_id"]
                    return

                # No server found -> start a new persistent process after
                # releasing the lock. The child will hold it until PID metadata
                # is written, so concurrent clients will block and then discover.
                should_start_process = True

            finally:
                unlock_file(lock_fd)

        if should_start_process:
            _start_process(port=port, open_browser=open_browser)

        # Poll for the PID file to appear (server writes it after binding)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            info = _discover_server()
            if info:
                _st._remote_url = info.get("api_url", info["url"])
                _st._auth_token = info.get("token")
                _st._session_id = info["session_id"]
                return
            time.sleep(0.1)

        # Fallback: start in-thread if process discovery failed
        logger.debug("Process discovery failed, falling back to in-thread server")
        from vitrine.server import DisplayServer

        if _st._session_id is None:
            _st._session_id = uuid.uuid4().hex[:12]

        _st._server = DisplayServer(
            study_manager=_st._study_manager, port=port, session_id=_st._session_id
        )
        _st._server.start(open_browser=open_browser)


def start(
    port: int = 7741,
    open_browser: bool = True,
    mode: str = "thread",
) -> None:
    """Start the display server.

    Called automatically on first show(). Call explicitly to customize settings.

    Args:
        port: Port to bind (auto-increments if taken).
        open_browser: Open browser tab on start.
        mode: "thread" (default) or "process" (separate daemon).
    """
    if mode == "process":
        _start_process(port=port, open_browser=open_browser)
    else:
        _ensure_started(port=port, open_browser=open_browser)


def _start_process(port: int = 7741, open_browser: bool = True) -> None:
    """Start the display server as a separate process."""
    import subprocess
    import sys

    cmd = [
        sys.executable,
        "-m",
        "vitrine.server",
        "--port",
        str(port),
    ]
    if not open_browser:
        cmd.append("--no-open")

    from vitrine._utils import detached_popen_kwargs

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_popen_kwargs(),
    )


def stop() -> None:
    """Stop the display server and event polling.

    Stops an in-process server if present. If no in-process server is active
    but a persistent server is connected/discoverable, attempts to stop it.
    """

    _st._event_poll_stop.set()
    with _st._lock:
        poll_thread = _st._event_poll_thread
        _st._event_poll_thread = None
        _st._event_callbacks.clear()
    if poll_thread is not None:
        poll_thread.join(timeout=2)

    with _st._lock:
        if _st._server is not None:
            _st._server.stop()
            _st._server = None
            return

    # No in-process server. If we have a remote connection hint, try stopping
    # the persistent server as well.
    with _st._lock:
        url = _st._remote_url

    if url is not None:
        stop_server()


def stop_server() -> bool:
    """Stop a running persistent display server via POST /api/shutdown.

    Study data persists on disk. Only the PID file is cleaned up.

    Returns True if a server was stopped.
    """
    info = _discover_server()
    if not info:
        return False

    url = info.get("api_url", info["url"])
    token = info.get("token")

    shutdown_requested = False
    try:
        import urllib.request

        data = json.dumps({}).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(
            f"{url}/api/shutdown",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            shutdown_requested = True
    except Exception:
        logger.debug(f"Failed to request shutdown for {url}")

    # Wait for process to exit if we have a PID; otherwise fall back to health.
    pid = info.get("pid")
    session_id = info.get("session_id")
    stopped = False
    if pid:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _is_process_alive(pid):
                stopped = True
                break
            time.sleep(0.1)
        if not stopped:
            stopped = not _health_check(url, session_id)
    else:
        stopped = not _health_check(url, session_id)

    # If still alive, keep PID metadata so status/stop can retry later.
    if not stopped:
        if shutdown_requested:
            logger.debug(f"Shutdown requested but server still healthy at {url}")
        return False

    # Clean up PID file only -- study data persists.
    pid_path = _pid_file_path()
    if pid_path.exists():
        try:
            pid_path.unlink()
        except OSError:
            pass

    # Stop event polling
    _st._event_poll_stop.set()
    if _st._event_poll_thread is not None:
        _st._event_poll_thread.join(timeout=2)
        _st._event_poll_thread = None
    _st._event_callbacks.clear()

    # Clear module state
    with _st._lock:
        _st._remote_url = None
        _st._auth_token = None
        if _st._session_id == session_id:
            _st._store = None
            _st._study_manager = None
            _st._session_id = None

    return True


def server_status() -> dict[str, Any] | None:
    """Return info about a running display server for this project, or None.

    Uses the PID file as the sole authority. No port scanning — that would
    risk reporting a different project's server as ours.
    """
    return _discover_server()


def _wait_for_card_response(card_id: str, timeout: float) -> dict[str, Any]:
    """Wait for a browser response to a blocking card.

    Uses in-process server if available, otherwise polls remote endpoint.
    """
    with _st._lock:
        server, url, token = _st._server, _st._remote_url, _st._auth_token

    if server is not None and hasattr(server, "wait_for_response_sync"):
        return server.wait_for_response_sync(card_id, timeout)

    if url and token:
        return _poll_remote_response(card_id, timeout)

    return {"action": "timeout", "card_id": card_id}


def _study_url(study: str | None) -> str | None:
    """Build a browser URL deep link for a study, when available."""
    if not study:
        return None
    from urllib.parse import quote

    with _st._lock:
        url, server = _st._remote_url, _st._server

    if url:
        return f"{url}/#study={quote(study, safe='')}"
    if server is not None:
        from vitrine.server import _get_display_host

        port = getattr(server, "port", 7741)
        return f"http://{_get_display_host()}:{port}/#study={quote(study, safe='')}"
    return None

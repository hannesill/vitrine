"""Display server: Starlette + WebSocket + REST for the display pipeline.

Runs in a background thread (default) or separate process, serving a browser
UI that renders cards pushed from the Python API. Uses Starlette (available
via fastmcp transitive dependency) instead of FastAPI.

Endpoints:
    GET  /                               → index.html
    GET  /static/{path}                  → static files (vendor JS, etc.)
    WS   /ws                             → bidirectional display channel
    GET  /api/cards?study=...             → list card descriptors
    GET  /api/table/{card_id}            → table page (offset, limit, sort)
    GET  /api/artifact/{card_id}         → raw artifact
    GET  /api/session                    → session metadata
    GET  /api/health                     → health check (returns session_id)
    POST /api/command                    → unified command endpoint (auth required)
    POST /api/shutdown                   → graceful shutdown (auth required)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from vitrine import routes_agent, routes_card, routes_study, ws_handlers
from vitrine._types import CardDescriptor
from vitrine.artifacts import ArtifactStore, _serialize_card
from vitrine.dispatch import (
    DispatchInfo,
    _dispatch_watchdog,
    _is_pid_alive,
    cleanup_dispatches,
    reconcile_orphaned_agents,
)
from vitrine.study_manager import StudyManager

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_DEFAULT_PORT = 7741
_MAX_PORT = 7750
_DISPLAY_HOST = "vitrine.localhost"


def _get_display_host() -> str:
    """Return the host name used in browser-facing URLs."""
    override = os.getenv("VITRINE_DISPLAY_HOST")
    if override:
        return override
    # Windows does not consistently resolve localhost subdomains for browsers.
    if sys.platform == "win32":
        return "127.0.0.1"
    return _DISPLAY_HOST


def _check_health(url: str, session_id: str | None = None) -> bool:
    """GET /api/health and optionally validate session_id matches."""
    from vitrine._utils import health_check

    return health_check(url, session_id=session_id)


def _get_vitrine_dir() -> Path:
    """Resolve the vitrine directory."""
    from vitrine._utils import get_vitrine_dir

    return get_vitrine_dir()


class DisplayServer:
    """WebSocket + REST server for the display pipeline.

    Manages the Starlette app, WebSocket connections, and study manager.
    Designed to run in a background thread via ``start()``.

    Args:
        store: ArtifactStore for persisting and reading artifacts (legacy).
        study_manager: StudyManager for study-centric storage (preferred).
        port: Port to bind to (auto-discovers if taken).
        host: Host to bind to (default: 127.0.0.1 for security).
    """

    def __init__(
        self,
        store: ArtifactStore | None = None,
        port: int = _DEFAULT_PORT,
        host: str = "127.0.0.1",
        token: str | None = None,
        session_id: str | None = None,
        study_manager: StudyManager | None = None,
    ) -> None:
        self.study_manager = study_manager
        # Backwards compat: if only store is passed, wrap it
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self.session_id = session_id or (store.session_id if store else "display")
        self._pid_path: Path | None = None
        self._connections: list[WebSocket] = []
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Agent-human interaction state
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._event_callbacks: list[Callable] = []
        self._event_queue: list[dict[str, Any]] = []
        self._selections: dict[str, list[int]] = {}  # card_id -> selected indices

        # Selection persistence
        vitrine_dir = study_manager.display_dir if study_manager else None
        self._selections_path: Path | None = (
            vitrine_dir / "selections.json" if vitrine_dir else None
        )
        self._load_selections()
        self._selection_save_timer: threading.Timer | None = None

        # Agent dispatch state
        self._dispatches: dict[str, DispatchInfo] = {}
        self._watchdog_task: asyncio.Task | None = None

        # Fix agent cards orphaned by previous server crashes/restarts
        fixed = reconcile_orphaned_agents(self)
        if fixed:
            logger.info(f"Reconciled {fixed} orphaned agent card(s)")

        # Server start time for health endpoint
        self._started_at = datetime.now(timezone.utc)

        self._app = self._build_app()

    def _load_selections(self) -> None:
        """Load persisted selections from disk."""
        if self._selections_path and self._selections_path.exists():
            try:
                data = json.loads(self._selections_path.read_text())
                if isinstance(data, dict):
                    self._selections = data
            except (json.JSONDecodeError, OSError):
                pass

    def _save_selections(self) -> None:
        """Save selections to disk (debounced — max 1 write/sec)."""
        if not self._selections_path:
            return
        try:
            with self._lock:
                snapshot = dict(self._selections)
            self._selections_path.write_text(json.dumps(snapshot, default=str))
        except OSError:
            logger.debug("Failed to persist selections to disk")

    def _schedule_save_selections(self) -> None:
        """Schedule a debounced selection save (max 1 write/sec)."""
        if self._selection_save_timer is not None:
            self._selection_save_timer.cancel()
        self._selection_save_timer = threading.Timer(1.0, self._save_selections)
        self._selection_save_timer.daemon = True
        self._selection_save_timer.start()

    def _build_app(self) -> Starlette:
        """Build the Starlette application with all routes."""

        def _r(handler):
            """Bind a handler(server, request) to this server instance."""
            return partial(handler, self)

        routes = [
            # Card & core endpoints
            Route("/", _r(routes_card.index)),
            Route("/api/health", _r(routes_card.api_health)),
            Route("/api/cards", _r(routes_card.api_cards)),
            Route("/api/table/{card_id}/selection", _r(routes_card.api_table_selection)),
            Route("/api/table/{card_id}/stats", _r(routes_card.api_table_stats)),
            Route("/api/table/{card_id}/export", _r(routes_card.api_table_export)),
            Route("/api/table/{card_id}", _r(routes_card.api_table)),
            Route("/api/card/{card_id}", _r(routes_card.api_card)),
            Route("/api/card/{card_id}/delete", _r(routes_card.api_card_delete), methods=["POST"]),
            Route("/api/artifact/{card_id}", _r(routes_card.api_artifact)),
            Route("/api/session", _r(routes_card.api_session)),
            Route("/api/command", _r(routes_card.api_command), methods=["POST"]),
            Route("/api/shutdown", _r(routes_card.api_shutdown), methods=["POST"]),
            Route("/api/response/{card_id}", _r(routes_card.api_response), methods=["GET"]),
            Route("/api/events", _r(routes_card.api_events), methods=["GET"]),
            # Study endpoints
            Route("/api/studies", _r(routes_study.api_studies), methods=["GET"]),
            Route("/api/studies/{study:path}/rename", _r(routes_study.api_study_rename), methods=["PATCH"]),
            Route("/api/studies/{study:path}/context", _r(routes_study.api_study_context), methods=["GET"]),
            Route("/api/studies/{study:path}/export", _r(routes_study.api_study_export), methods=["GET"]),
            Route("/api/studies/{study:path}/files", _r(routes_study.api_study_files), methods=["GET"]),
            Route("/api/studies/{study:path}/files-archive", _r(routes_study.api_study_files_archive), methods=["GET"]),
            Route("/api/studies/{study:path}/files/{filepath:path}", _r(routes_study.api_study_file), methods=["GET"]),
            # Agent endpoints
            Route("/api/studies/{study:path}/agents", _r(routes_agent.api_create_agent), methods=["POST"]),
            Route("/api/agents/{card_id}/run", _r(routes_agent.api_run_agent), methods=["POST"]),
            Route("/api/agents/{card_id}", _r(routes_agent.api_agent_handler), methods=["GET", "DELETE"]),
            # Study delete & global export
            Route("/api/studies/{study:path}", _r(routes_study.api_study_delete), methods=["DELETE"]),
            Route("/api/export", _r(routes_study.api_export), methods=["GET"]),
            Route("/api/files-archive", _r(routes_study.api_all_files_archive), methods=["GET"]),
            # WebSocket
            WebSocketRoute("/ws", _r(ws_handlers.ws_endpoint)),
        ]

        # Mount static files if the directory exists
        if _STATIC_DIR.exists():
            routes.append(Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR))))

        return Starlette(routes=routes)

    # --- Public interface for dispatch (DispatchHost protocol) ---

    @property
    def dispatches(self) -> dict[str, DispatchInfo]:
        """Agent dispatch state, keyed by card_id."""
        return self._dispatches

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients (public API)."""
        await self._broadcast(message)

    # --- Store Resolution ---

    def _resolve_store(self, card_id: str | None = None) -> ArtifactStore | None:
        """Resolve the ArtifactStore for a given card_id.

        If study_manager is available, looks up the card in the cross-study index.
        Falls back to the legacy self.store. Refreshes from disk if not found.
        """
        if card_id and self.study_manager:
            store = self.study_manager.get_store_for_card(card_id)
            if store:
                return store
            # Card not in index — client may have created a new study
            self.study_manager.refresh()
            store = self.study_manager.get_store_for_card(card_id)
            if store:
                return store
        return self.store

    def _get_card_annotations(
        self, store: ArtifactStore, card_id: str
    ) -> list[dict[str, Any]]:
        """Return a copy of the annotations list for a card, or [] if not found."""
        for c in store.list_cards():
            if c.card_id == card_id:
                return list(c.annotations)
        return []

    # --- HTTP Endpoints (delegated to route modules) ---

    def _check_auth(self, request: Request) -> bool:
        """Check Bearer token authorization."""
        if not self.token:
            return True
        auth = request.headers.get("authorization", "")
        return auth == f"Bearer {self.token}"

    def _preview_tabular_file(self, path: Path, suffix: str) -> Response:
        """Preview a CSV or Parquet file as JSON table (max 1000 rows)."""
        try:
            import duckdb

            con = duckdb.connect(":memory:")
            try:
                safe_path = str(path).replace("'", "''")
                if suffix == ".csv":
                    reader = f"read_csv_auto('{safe_path}')"
                else:
                    reader = f"read_parquet('{safe_path}')"

                total = con.execute(f"SELECT COUNT(*) FROM {reader}").fetchone()[0]
                result = con.execute(f"SELECT * FROM {reader} LIMIT 1000")
                columns = [desc[0] for desc in result.description]
                rows = [
                    [v.isoformat() if hasattr(v, "isoformat") else v for v in row]
                    for row in result.fetchall()
                ]
            finally:
                con.close()

            return JSONResponse(
                {
                    "columns": columns,
                    "rows": rows,
                    "total_rows": total,
                    "truncated": total > 1000,
                }
            )
        except Exception as e:
            return JSONResponse({"error": f"Failed to read file: {e}"}, status_code=500)

    def _build_summary(
        self,
        card_id: str,
        selected_rows: list | None,
        points: list | None = None,
        columns: list | None = None,
    ) -> str:
        """Build a human-readable summary of a selection."""
        # Look up card title from store
        card_title = ""
        try:
            if self.study_manager:
                cards = self.study_manager.list_all_cards()
            elif self.store:
                cards = self.store.list_cards()
            else:
                cards = []
            for c in cards:
                if c.card_id == card_id:
                    card_title = c.title or ""
                    break
        except Exception:
            pass

        parts = []
        if selected_rows:
            n = len(selected_rows)
            ncols = len(columns) if columns else 0
            shape = f"{n} row{'s' if n != 1 else ''}"
            if ncols:
                shape += f" \u00d7 {ncols} col{'s' if ncols != 1 else ''}"
            parts.append(shape)
            if columns:
                col_str = ", ".join(str(c) for c in columns[:6])
                if len(columns) > 6:
                    col_str += ", \u2026"
                parts.append(f"({col_str})")
        if points:
            n = len(points)
            parts.append(f"{n} point{'s' if n != 1 else ''}")
        if card_title:
            parts.append(f"from '{card_title}'")
        return " ".join(parts) if parts else ""

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients."""
        with self._lock:
            connections = list(self._connections)
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception:
                with self._lock:
                    if ws in self._connections:
                        self._connections.remove(ws)

    # --- Blocking Response ---

    async def wait_for_response(self, card_id: str, timeout: float) -> dict[str, Any]:
        """Wait for a browser response to a blocking show() card.

        Args:
            card_id: The card ID to wait for.
            timeout: Maximum seconds to wait.

        Returns:
            Dict with action, card_id, message, artifact_id.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_responses[card_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"action": "timeout", "card_id": card_id}
        finally:
            self._pending_responses.pop(card_id, None)

    def wait_for_response_sync(self, card_id: str, timeout: float) -> dict[str, Any]:
        """Sync wrapper for wait_for_response (called from Python API thread).

        Args:
            card_id: The card ID to wait for.
            timeout: Maximum seconds to wait.

        Returns:
            Dict with action, card_id, message, artifact_id.
        """
        if self._loop is None:
            return {"action": "timeout", "card_id": card_id}
        future = asyncio.run_coroutine_threadsafe(
            self.wait_for_response(card_id, timeout), self._loop
        )
        try:
            return future.result(timeout=timeout + 1)
        except Exception:
            return {"action": "timeout", "card_id": card_id}

    def register_event_callback(self, callback: Callable) -> None:
        """Register a callback for UI events.

        Args:
            callback: Function that receives DisplayEvent instances.
        """
        with self._lock:
            self._event_callbacks.append(callback)

    # --- Lifecycle ---

    def _find_port(self) -> int:
        """Find an available port, starting from self.port."""
        for port in range(self.port, _MAX_PORT + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((self.host, port))
                    return port
            except OSError:
                continue
        raise RuntimeError(f"No available port in range {self.port}-{_MAX_PORT}")

    def start(
        self,
        open_browser: bool = True,
        pid_path: Path | None = None,
    ) -> None:
        """Start the server in a background daemon thread.

        Args:
            open_browser: Open a browser tab to the display.
            pid_path: If set, write a PID file after the server binds.
        """
        if self._thread and self._thread.is_alive():
            return

        self.port = self._find_port()

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._started.set()
            self._loop.run_until_complete(self._server.serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)

        # Wait a moment for the server to fully bind
        self._wait_for_server()

        # Start dispatch watchdog
        if self._loop:
            self._loop.call_soon_threadsafe(
                lambda: setattr(
                    self,
                    "_watchdog_task",
                    self._loop.create_task(_dispatch_watchdog(self)),
                )
            )

        # Write PID file if requested
        if pid_path is not None:
            self._write_pid_file(pid_path)

        import sys

        print(
            f"vitrine: {self.url}",
            file=sys.stderr,
        )

        if open_browser:
            try:
                import webbrowser

                webbrowser.open(self.url)
            except Exception:
                pass

    def _wait_for_server(self, timeout: float = 3.0) -> None:
        """Wait for the server to accept connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.1)
                    s.connect((self.host, self.port))
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)

    def stop(self) -> None:
        """Stop the server and remove PID file if set."""
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        cleanup_dispatches(self)
        self._remove_pid_file()
        # Flush pending selection save
        if self._selection_save_timer is not None:
            self._selection_save_timer.cancel()
            self._selection_save_timer = None
        self._save_selections()
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._server = None
        logger.debug("Display server stopped")

    def _write_pid_file(self, pid_path: Path) -> None:
        """Write the PID file with server metadata."""
        self._pid_path = pid_path
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        info = {
            "pid": os.getpid(),
            "port": self.port,
            "host": self.host,
            "url": self.url,
            "session_id": self.session_id,
            "token": self.token,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        pid_path.write_text(json.dumps(info, indent=2))
        logger.debug(f"PID file written: {pid_path}")

    def _remove_pid_file(self) -> None:
        """Remove the PID file only if it still belongs to this server.

        Another server may have overwritten the PID file after we started.
        Blindly deleting it would orphan that newer server, so we verify
        our own PID is still recorded before unlinking.
        """
        if not self._pid_path or not self._pid_path.exists():
            self._pid_path = None
            return
        try:
            info = json.loads(self._pid_path.read_text())
            if info.get("pid") != os.getpid():
                logger.debug(
                    "PID file belongs to pid=%s, not us (%s); leaving it",
                    info.get("pid"),
                    os.getpid(),
                )
                self._pid_path = None
                return
            self._pid_path.unlink()
            logger.debug(f"PID file removed: {self._pid_path}")
        except (json.JSONDecodeError, OSError):
            pass
        self._pid_path = None

    @property
    def is_running(self) -> bool:
        """Check if the server is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        """Return the browser-facing server URL."""
        return f"http://{_get_display_host()}:{self.port}"

    def push_card(self, card: CardDescriptor) -> None:
        """Push a card to all connected WebSocket clients.

        Called by the Python API after rendering + storing a card.
        """
        message = {
            "type": "display.add",
            "card": _serialize_card(card),
        }
        self._broadcast_from_thread(message)

    def push_update(self, card_id: str, card: CardDescriptor) -> None:
        """Push a card update to all connected WebSocket clients.

        Sends a display.update message with the full card data so
        the frontend can re-render the card in place.
        """
        message = {
            "type": "display.update",
            "card_id": card_id,
            "card": _serialize_card(card),
        }
        self._broadcast_from_thread(message)

    def push_section(self, title: str, study: str | None = None) -> None:
        """Push a section divider to all connected clients."""
        message = {
            "type": "display.section",
            "title": title,
            "study": study,
        }
        self._broadcast_from_thread(message)

    def _broadcast_from_thread(self, message: dict[str, Any]) -> None:
        """Broadcast a message from a sync context (called from Python API thread)."""
        with self._lock:
            connections = list(self._connections)
        if not connections:
            return
        try:
            loop = self._loop
            for ws in connections:
                asyncio.run_coroutine_threadsafe(ws.send_json(message), loop)
        except Exception:
            logger.debug("Could not broadcast message")


def _kill_orphaned_servers(host: str, port_lo: int, port_hi: int) -> None:
    """Kill any vitrine servers lingering on our port range without a PID file.

    These are servers whose PID file was lost (e.g. the process crashed
    before cleanup ran).  Without a PID file they are undiscoverable and
    block port allocation, so new servers keep bumping to higher ports.

    Strategy: probe each port for a vitrine health endpoint.  If found,
    use ``lsof`` to resolve the PID and send SIGTERM.

    On Windows this is a no-op — orphaned servers are handled by PID file
    checks and health checks (already implemented).
    """
    import sys

    if sys.platform == "win32":
        return

    import subprocess
    import urllib.request

    for port in range(port_lo, port_hi + 1):
        # Quick probe — unoccupied ports fail instantly
        try:
            hreq = urllib.request.Request(
                f"http://{host}:{port}/api/health", method="GET"
            )
            with urllib.request.urlopen(hreq, timeout=0.5) as resp:
                data = json.loads(resp.read())
            if data.get("status") != "ok":
                continue
        except Exception:
            continue

        logger.debug(f"Found orphaned vitrine server on port {port}")

        # Resolve the PID owning this port via lsof
        try:
            out = subprocess.check_output(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                text=True,
                timeout=2,
            ).strip()
        except Exception:
            logger.debug(f"Could not resolve PID for port {port}")
            continue

        for pid_str in out.splitlines():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            logger.debug(f"Sending SIGTERM to orphaned vitrine pid={pid}")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        # Wait for port to free up
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind((host, port))
                break
            except OSError:
                time.sleep(0.1)


def _run_standalone(port: int = _DEFAULT_PORT, no_open: bool = False) -> None:
    """Run the display server as a standalone persistent process.

    Acquires a file lock to prevent duplicate servers, checks the
    PID file for an existing healthy server, then starts.
    """
    import atexit
    import sys

    from vitrine._utils import lock_file, unlock_file

    display_dir = _get_vitrine_dir()
    display_dir.mkdir(parents=True, exist_ok=True)

    lock_path = display_dir / ".server.lock"
    pid_path = display_dir / ".server.json"

    # Acquire cross-process file lock
    lock_fd = open(lock_path, "w")
    try:
        lock_file(lock_fd, exclusive=True, blocking=False)
    except OSError:
        # Another process holds the lock — a server is starting
        logger.debug("Another server process holds the lock, exiting")
        lock_fd.close()
        sys.exit(0)

    try:
        # Check PID file for an existing healthy server
        if pid_path.exists():
            try:
                info = json.loads(pid_path.read_text())
                pid = info.get("pid")
                host = info.get("host", "127.0.0.1")
                port_num = info.get("port")
                sid = info.get("session_id")
                api_url = f"http://{host}:{port_num}" if port_num else info.get("url")
                if (
                    pid
                    and api_url
                    and _is_pid_alive(pid)
                    and _check_health(api_url, sid)
                ):
                    logger.debug(f"Healthy server already running (pid={pid}), exiting")
                    sys.exit(0)
            except (json.JSONDecodeError, OSError):
                pass

        # Kill any orphaned servers occupying our port range.
        # These are leftovers from crashed sessions whose PID file was
        # lost — without this they'd force us onto a higher port and
        # accumulate indefinitely.
        _kill_orphaned_servers("127.0.0.1", port, _MAX_PORT)

        # No server found for this project — start one while holding the lock
        session_id = uuid.uuid4().hex[:12]
        token = secrets.token_hex(16)

        study_manager = StudyManager(display_dir)
        server = DisplayServer(
            study_manager=study_manager,
            port=port,
            host="127.0.0.1",
            token=token,
            session_id=session_id,
        )

        stop_event = threading.Event()

        def _shutdown(signum: int, frame: Any) -> None:
            logger.debug(f"Received signal {signum}, shutting down...")
            stop_event.set()

        # On Windows, only SIGINT and SIGBREAK are supported.
        # Use SIGBREAK as the Windows equivalent of SIGTERM.
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, _shutdown)  # type: ignore[attr-defined]
        else:
            signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        atexit.register(server.stop)

        # start() writes the PID file after binding — still inside the lock
        server.start(open_browser=not no_open, pid_path=pid_path)

    finally:
        # Release the lock after PID file is written (or on error)
        unlock_file(lock_fd)
        lock_fd.close()

    # Block until signal (outside lock — other processes can now discover us)
    stop_event.wait()
    server.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="vitrine server")
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="Port to bind to"
    )
    parser.add_argument("--no-open", action="store_true", help="Don't open browser")
    args = parser.parse_args()
    _run_standalone(port=args.port, no_open=args.no_open)

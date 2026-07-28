"""Standalone vitrine CLI.

Usage:
    vitrine doctor  [--json]
    vitrine restart [--port PORT] [--no-open]
    vitrine start   [--port PORT] [--no-open]
    vitrine stop
    vitrine status
    vitrine studies
    vitrine clean OLDER_THAN
    vitrine export PATH [--format FORMAT] [--study STUDY]
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import typer
from rich.console import Console

from vitrine._utils import (
    HEALTH_CHECK_TIMEOUT_SECONDS,
    PROCESS_REAP_TIMEOUT_SECONDS,
)

app = typer.Typer(
    name="vitrine",
    help="Manage the vitrine display server and studies.",
    no_args_is_help=True,
)
console = Console()

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_NOT_RUNNING = 3
LIFECYCLE_CONTRACT_VERSION = 1
STARTUP_READY_TIMEOUT_SECONDS = 20.0
STARTUP_POLL_INTERVAL_SECONDS = 0.2
STARTUP_STATUS_CHECK_TIMEOUT_SECONDS = HEALTH_CHECK_TIMEOUT_SECONDS
STARTUP_REAP_TIMEOUT_SECONDS = PROCESS_REAP_TIMEOUT_SECONDS
# terminate_spawned_process may wait once after terminate and once after kill.
STARTUP_CLEANUP_BUDGET_SECONDS = 2 * STARTUP_REAP_TIMEOUT_SECONDS
STARTUP_COMMAND_BUDGET_SECONDS = (
    STARTUP_READY_TIMEOUT_SECONDS
    + STARTUP_STATUS_CHECK_TIMEOUT_SECONDS
    + STARTUP_POLL_INTERVAL_SECONDS
    + STARTUP_CLEANUP_BUDGET_SECONDS
)


class _StartFailure(RuntimeError):
    """Raised when a background server does not become healthy."""


def _info(msg: str) -> None:
    console.print(f"[dim]>[/dim] {msg}")


def _success(msg: str) -> None:
    console.print(f"[green]\u2713[/green] {msg}")


def _error(msg: str) -> None:
    console.print(f"[red]\u2717[/red] {msg}")


def _package_version() -> str:
    """Return the installed Vitrine package version."""
    from vitrine._utils import package_version

    return package_version()


def _lifecycle_payload(
    status: str,
    info: dict[str, Any] | None = None,
    *,
    error: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Build the stable machine-readable lifecycle response."""
    from vitrine._utils import get_vitrine_dir

    info = info or {}
    api_url = info.get("api_url")
    port = info.get("port")
    if not api_url and port:
        api_url = f"http://{info.get('host', '127.0.0.1')}:{port}"

    return {
        "status": status,
        "lifecycle_contract_version": LIFECYCLE_CONTRACT_VERSION,
        "url": info.get("url"),
        "api_url": api_url,
        "pid": info.get("pid"),
        "port": port,
        "session_id": info.get("session_id"),
        "data_dir": info.get("data_dir", str(get_vitrine_dir().resolve())),
        "version": version or info.get("version") or _package_version(),
        "error": error,
    }


def _emit_json(payload: dict[str, Any]) -> None:
    """Write one compact JSON object to stdout."""
    typer.echo(json.dumps(payload, separators=(",", ":")))


def _fail_metadata(exc: RuntimeError, json_output: bool) -> None:
    """Report untrusted server metadata without accepting daemon identity."""
    if json_output:
        _emit_json(_lifecycle_payload("failed", error=str(exc), version="unknown"))
    else:
        _error(str(exc))
    raise typer.Exit(EXIT_FAILURE) from exc


def _server_status(json_output: bool) -> dict[str, Any] | None:
    """Read status and turn metadata failures into stable CLI failures."""
    from vitrine import server_status
    from vitrine._utils import ServerMetadataError

    try:
        return server_status()
    except ServerMetadataError as exc:
        _fail_metadata(exc, json_output)


def _stop_server(json_output: bool) -> bool:
    """Stop a daemon and turn metadata failures into stable CLI failures."""
    from vitrine import stop_server
    from vitrine._utils import ServerMetadataError

    try:
        return stop_server()
    except ServerMetadataError as exc:
        _fail_metadata(exc, json_output)


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable installation diagnostics."
    ),
) -> None:
    """Report the installed Vitrine runtime and lifecycle contract."""
    version = _package_version()
    if json_output:
        _emit_json(
            {
                "status": "ok",
                "version": version,
                "lifecycle_contract_version": LIFECYCLE_CONTRACT_VERSION,
                "python_executable": sys.executable,
            }
        )
        return

    _success("Vitrine installation is healthy")
    console.print(f"  [bold]Version:[/bold] {version}")
    console.print(f"  [bold]Lifecycle contract:[/bold] {LIFECYCLE_CONTRACT_VERSION}")
    console.print("  [bold]Python executable:[/bold]")
    console.print(f"    {sys.executable}")


@app.command()
def restart(
    port: int = typer.Option(7741, "--port", "-p", help="Port to bind to."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open browser."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable lifecycle JSON."
    ),
) -> None:
    """Stop the running vitrine server and start a fresh one."""
    info = _server_status(json_output)
    if info:
        if not json_output:
            _info(
                f"Stopping server (pid={info.get('pid')}, port={info.get('port')})..."
            )
        if _stop_server(json_output):
            if not json_output:
                _success("Server stopped.")
        else:
            message = "Failed to stop server. Try killing the process manually."
            if json_output:
                _emit_json(_lifecycle_payload("failed", error=message))
            else:
                _error(message)
            raise typer.Exit(EXIT_FAILURE)
    else:
        if not json_output:
            _info("No running server found — starting fresh.")

    try:
        started = _start_background(port=port, no_open=no_open, json_output=json_output)
    except _StartFailure as exc:
        if json_output:
            _emit_json(_lifecycle_payload("failed", error=str(exc)))
        raise typer.Exit(EXIT_FAILURE) from exc
    if json_output:
        _emit_json(_lifecycle_payload("running", started))


@app.command()
def start(
    port: int = typer.Option(7741, "--port", "-p", help="Port to bind to."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open browser."),
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="Run in foreground (blocks)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable lifecycle JSON."
    ),
) -> None:
    """Start the vitrine server."""
    info = _server_status(json_output)
    if info:
        if json_output:
            _emit_json(_lifecycle_payload("running", info))
        else:
            _info(
                f"Server already running (pid={info.get('pid')}, "
                f"port={info.get('port')}, url={info.get('url')})"
            )
        return

    if foreground:
        if json_output:
            message = "--json cannot be combined with --foreground."
            _emit_json(_lifecycle_payload("failed", error=message))
            raise typer.Exit(EXIT_FAILURE)
        from vitrine.server import _run_standalone

        _info(f"Starting vitrine server on port {port}...")
        _run_standalone(port=port, no_open=no_open)
    else:
        try:
            started = _start_background(
                port=port, no_open=no_open, json_output=json_output
            )
        except _StartFailure as exc:
            if json_output:
                _emit_json(_lifecycle_payload("failed", error=str(exc)))
            raise typer.Exit(EXIT_FAILURE) from exc
        if json_output:
            _emit_json(_lifecycle_payload("running", started))


@app.command()
def stop(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable lifecycle JSON."
    ),
) -> None:
    """Stop the running vitrine server."""
    if json_output:
        info = _server_status(json_output)
        if _stop_server(json_output):
            _emit_json(_lifecycle_payload("stopped"))
            return
        if info:
            message = "Failed to stop the running server."
            _emit_json(_lifecycle_payload("failed", error=message))
            raise typer.Exit(EXIT_FAILURE)
        _emit_json(_lifecycle_payload("stopped"))
        return

    if _stop_server(json_output):
        _success("Server stopped.")
    else:
        _info("No running server found.")


@app.command()
def status(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable lifecycle JSON."
    ),
) -> None:
    """Show status of the vitrine server."""
    info = _server_status(json_output)
    if info:
        if json_output:
            _emit_json(_lifecycle_payload("running", info))
            return
        _success("Server is running")
        console.print(f"  [bold]URL:[/bold]        {info.get('url')}")
        console.print(f"  [bold]PID:[/bold]        {info.get('pid')}")
        console.print(f"  [bold]Port:[/bold]       {info.get('port')}")
        console.print(f"  [bold]Session:[/bold]    {info.get('session_id')}")
        console.print(f"  [bold]Started:[/bold]    {info.get('started_at')}")
    else:
        if json_output:
            _emit_json(_lifecycle_payload("stopped"))
            raise typer.Exit(EXIT_NOT_RUNNING)
        _info("No running server found.")


@app.command()
def studies() -> None:
    """List all vitrine studies."""
    from vitrine import list_studies as do_list_studies

    result = do_list_studies()
    if not result:
        _info("No studies found.")
        return

    console.print()
    console.print(f"[bold]Studies ({len(result)}):[/bold]")
    console.print()
    for s in result:
        label = s.get("label", "?")
        start = s.get("start_time", "?")
        cards = s.get("card_count", 0)
        console.print(f"  [green]{label:<30s}[/green] {cards:>3d} cards   {start}")


@app.command()
def clean(
    older_than: str = typer.Argument(
        help="Remove studies older than duration (e.g., '7d', '24h', '0d' for all)."
    ),
) -> None:
    """Remove studies older than a given duration."""
    from vitrine import clean_studies as do_clean

    removed = do_clean(older_than=older_than)
    if removed > 0:
        _success(f"Removed {removed} study/studies.")
    else:
        _info("No studies matched the age filter.")


@app.command()
def export(
    path: str = typer.Argument(help="Output file path."),
    format: str = typer.Option(
        "html", "--format", "-f", help="Export format: 'html' or 'json'."
    ),
    study: str | None = typer.Option(
        None, "--study", help="Study label (default: all studies)."
    ),
) -> None:
    """Export study/studies to file."""
    from vitrine import export as do_export

    try:
        result = do_export(path, format=format, study=study)
        _success(f"Exported to {result}")
    except ValueError as e:
        _error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        _error(f"Export failed: {e}")
        raise typer.Exit(1)


def _start_background(
    port: int = 7741,
    no_open: bool = False,
    *,
    json_output: bool = False,
) -> dict[str, Any]:
    """Start the server as a background process and wait for it to come up."""
    import subprocess

    cmd = [
        sys.executable,
        "-m",
        "vitrine.server",
        "--port",
        str(port),
    ]
    if no_open:
        cmd.append("--no-open")

    from vitrine._utils import (
        ServerMetadataError,
        detached_popen_kwargs,
        terminate_spawned_process,
    )

    if not json_output:
        _info(f"Starting vitrine server on port {port}...")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached_popen_kwargs(),
        )
    except OSError as exc:
        message = f"Failed to start vitrine server: {exc}"
        if not json_output:
            _error(message)
        raise _StartFailure(message) from exc

    # Wait for server to come up. Keep the Popen handle so failures can only
    # signal and reap the exact child created above.
    from vitrine import server_status

    process_reaped = False
    deadline = time.monotonic() + STARTUP_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            info = server_status()
        except ServerMetadataError as exc:
            if not process_reaped:
                terminate_spawned_process(process, timeout=STARTUP_REAP_TIMEOUT_SECONDS)
            raise _StartFailure(str(exc)) from exc
        if info:
            spawned_pid = getattr(process, "pid", None)
            running_pid = info.get("pid")
            if (
                type(spawned_pid) is int
                and type(running_pid) is int
                and spawned_pid != running_pid
                and not process_reaped
            ):
                terminate_spawned_process(process, timeout=STARTUP_REAP_TIMEOUT_SECONDS)
            if not json_output:
                _success(
                    f"Server started (pid={info.get('pid')}, url={info.get('url')})"
                )
            return info
        returncode = process.poll()
        if isinstance(returncode, int):
            if not process_reaped:
                terminate_spawned_process(process, timeout=STARTUP_REAP_TIMEOUT_SECONDS)
                process_reaped = True
            if returncode != 0:
                message = (
                    "Vitrine server exited before becoming healthy "
                    f"(exit code {returncode})."
                )
                if not json_output:
                    _error(message)
                raise _StartFailure(message)
        time.sleep(STARTUP_POLL_INTERVAL_SECONDS)

    if not process_reaped:
        terminate_spawned_process(process, timeout=STARTUP_REAP_TIMEOUT_SECONDS)
    timeout = f"{STARTUP_READY_TIMEOUT_SECONDS:g}"
    message = f"Server process started but didn't become healthy within {timeout}s."
    if not json_output:
        _error(message)
    raise _StartFailure(message)


if __name__ == "__main__":
    app()

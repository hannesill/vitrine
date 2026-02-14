"""Standalone vitrine CLI.

Usage:
    vitrine restart [--port PORT] [--no-open]
    vitrine start   [--port PORT] [--no-open]
    vitrine stop
    vitrine status
    vitrine studies
    vitrine clean OLDER_THAN
    vitrine export PATH [--format FORMAT] [--study STUDY]
"""

from __future__ import annotations

import sys
import time

import typer
from rich.console import Console

app = typer.Typer(
    name="vitrine",
    help="Manage the vitrine display server and studies.",
    no_args_is_help=True,
)
console = Console()


def _info(msg: str) -> None:
    console.print(f"[dim]>[/dim] {msg}")


def _success(msg: str) -> None:
    console.print(f"[green]\u2713[/green] {msg}")


def _error(msg: str) -> None:
    console.print(f"[red]\u2717[/red] {msg}")


@app.command()
def restart(
    port: int = typer.Option(7741, "--port", "-p", help="Port to bind to."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open browser."),
) -> None:
    """Stop the running vitrine server and start a fresh one."""
    from vitrine import server_status, stop_server

    info = server_status()
    if info:
        _info(f"Stopping server (pid={info.get('pid')}, port={info.get('port')})...")
        if stop_server():
            _success("Server stopped.")
        else:
            _error("Failed to stop server. Try killing the process manually.")
            raise typer.Exit(1)
    else:
        _info("No running server found — starting fresh.")

    _start_background(port=port, no_open=no_open)


@app.command()
def start(
    port: int = typer.Option(7741, "--port", "-p", help="Port to bind to."),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open browser."),
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="Run in foreground (blocks)."
    ),
) -> None:
    """Start the vitrine server."""
    from vitrine import server_status

    info = server_status()
    if info:
        _info(
            f"Server already running (pid={info.get('pid')}, "
            f"port={info.get('port')}, url={info.get('url')})"
        )
        return

    if foreground:
        from vitrine.server import _run_standalone

        _info(f"Starting vitrine server on port {port}...")
        _run_standalone(port=port, no_open=no_open)
    else:
        _start_background(port=port, no_open=no_open)


@app.command()
def stop() -> None:
    """Stop the running vitrine server."""
    from vitrine import stop_server

    if stop_server():
        _success("Server stopped.")
    else:
        _info("No running server found.")


@app.command()
def status() -> None:
    """Show status of the vitrine server."""
    from vitrine import server_status

    info = server_status()
    if info:
        _success("Server is running")
        console.print(f"  [bold]URL:[/bold]        {info.get('url')}")
        console.print(f"  [bold]PID:[/bold]        {info.get('pid')}")
        console.print(f"  [bold]Port:[/bold]       {info.get('port')}")
        console.print(f"  [bold]Session:[/bold]    {info.get('session_id')}")
        console.print(f"  [bold]Started:[/bold]    {info.get('started_at')}")
    else:
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
        console.print(
            f"  [green]{label:<30s}[/green] {cards:>3d} cards   {start}"
        )


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
    format: str = typer.Option("html", "--format", "-f", help="Export format: 'html' or 'json'."),
    study: str | None = typer.Option(None, "--study", help="Study label (default: all studies)."),
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


def _start_background(port: int = 7741, no_open: bool = False) -> None:
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

    _info(f"Starting vitrine server on port {port}...")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for server to come up
    from vitrine import server_status

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        info = server_status()
        if info:
            _success(f"Server started (pid={info.get('pid')}, url={info.get('url')})")
            return
        time.sleep(0.2)

    _error("Server process started but didn't become healthy within 5s.")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()

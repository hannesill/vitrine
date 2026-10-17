"""Card, table, artifact, session, health, command, response, and events endpoints."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from vitrine._utils import package_version
from vitrine.artifacts import _serialize_card

if TYPE_CHECKING:
    from vitrine.server import DisplayServer

_STATIC_DIR = None  # Set at import time below


def _get_static_dir():
    from pathlib import Path

    return Path(__file__).parent / "static"


_STATIC_URL_RE = re.compile(r'((?:src|href)=")(/static/[^"?#]+)(")')


def compute_asset_version(static_dir: Path) -> str:
    """Return a cache-busting token: package version plus a hash of the static files.

    The hash covers file paths and contents, so editing a JS/CSS file busts the
    browser cache on the next server start, not only a vitrine upgrade.
    """
    digest = hashlib.sha256()
    if static_dir.is_dir():
        for path in sorted(p for p in static_dir.rglob("*") if p.is_file()):
            digest.update(path.relative_to(static_dir).as_posix().encode())
            digest.update(path.read_bytes())
    return f"{package_version()}-{digest.hexdigest()[:12]}"


def _version_static_urls(html: str, asset_version: str) -> str:
    """Append ``?v=<asset_version>`` to every /static/ src and href in *html*."""
    return _STATIC_URL_RE.sub(rf"\1\2?v={asset_version}\3", html)


async def index(server: DisplayServer, request: Request) -> Response:
    """Serve the main index.html page.

    Asset URLs carry a version query and the page itself is always
    revalidated, so a browser never runs JS/CSS from an older vitrine.
    """
    static_dir = _get_static_dir()
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>vitrine</h1><p>index.html not found</p>")
    html = _version_static_urls(index_path.read_text(), server.asset_version)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def api_cards(server: DisplayServer, request: Request) -> JSONResponse:
    """List card descriptors, optionally filtered by study."""
    study = request.query_params.get("study")
    if server.study_manager:
        server.study_manager.refresh()
        cards = server.study_manager.list_all_cards(study=study)
    elif server.store:
        cards = server.store.list_cards(study=study)
    else:
        cards = []
    return JSONResponse([_serialize_card(c) for c in cards])


async def api_card(server: DisplayServer, request: Request) -> JSONResponse:
    """Return a single card descriptor by ID or prefix."""
    raw = request.path_params["card_id"]
    id_prefix = raw.split("-")[0]

    if server.study_manager:
        server.study_manager.refresh()
        cards = server.study_manager.list_all_cards()
    elif server.store:
        cards = server.store.list_cards()
    else:
        cards = []

    for card in cards:
        if card.card_id.startswith(id_prefix):
            return JSONResponse(_serialize_card(card))
    return JSONResponse({"error": f"Card {raw} not found"}, status_code=404)


async def api_card_delete(server: DisplayServer, request: Request) -> JSONResponse:
    """Soft-delete or restore a card."""
    card_id = request.path_params["card_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    deleted = body.get("deleted", True)

    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse({"error": f"Card {card_id} not found"}, status_code=404)

    updates: dict[str, Any] = {"deleted": deleted}
    if deleted:
        updates["deleted_at"] = datetime.now(timezone.utc).isoformat()
    else:
        updates["deleted_at"] = None
    updated = store.update_card(card_id, **updates)
    if updated:
        await server.broadcast(
            {
                "type": "display.update",
                "card_id": card_id,
                "card": _serialize_card(updated),
            }
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": f"Card {card_id} not found"}, status_code=404)


async def api_table(server: DisplayServer, request: Request) -> JSONResponse:
    """Return a page of table data from a stored Parquet artifact."""
    card_id = request.path_params["card_id"]
    offset = max(0, int(request.query_params.get("offset", "0")))
    limit = max(1, min(int(request.query_params.get("limit", "50")), 10000))
    sort_col = request.query_params.get("sort")
    sort_asc = request.query_params.get("asc", "true").lower() == "true"
    search = request.query_params.get("search") or None

    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )

    try:
        page = store.read_table_page(
            card_id=card_id,
            offset=offset,
            limit=limit,
            sort_col=sort_col,
            sort_asc=sort_asc,
            search=search,
        )
        return JSONResponse(page)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )


async def api_table_selection(server: DisplayServer, request: Request) -> JSONResponse:
    """Return selected rows for a table card."""
    card_id = request.path_params["card_id"]
    indices = server._selections.get(card_id, [])
    if not indices:
        return JSONResponse({"selected_indices": [], "columns": [], "rows": []})

    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse({"selected_indices": indices, "columns": [], "rows": []})

    path = store._artifacts_dir / f"{card_id}.parquet"
    if not path.exists():
        return JSONResponse({"selected_indices": indices, "columns": [], "rows": []})

    try:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            safe_path = str(path).replace("'", "''")
            idx_list = ", ".join(str(int(i)) for i in indices)
            query = (
                f"SELECT * FROM ("
                f"  SELECT *, ROW_NUMBER() OVER () - 1 AS _rn "
                f"  FROM read_parquet('{safe_path}')"
                f") WHERE _rn IN ({idx_list})"
            )
            result = con.execute(query)
            columns = [desc[0] for desc in result.description if desc[0] != "_rn"]
            rows = [
                [v for v, d in zip(row, result.description) if d[0] != "_rn"]
                for row in result.fetchall()
            ]
        finally:
            con.close()

        return JSONResponse(
            {"selected_indices": indices, "columns": columns, "rows": rows}
        )
    except Exception:
        return JSONResponse({"selected_indices": indices, "columns": [], "rows": []})


async def api_table_stats(server: DisplayServer, request: Request) -> JSONResponse:
    """Return per-column statistics for a table artifact."""
    card_id = request.path_params["card_id"]
    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )
    try:
        stats = store.table_stats(card_id)
        return JSONResponse(stats)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )


async def api_table_export(server: DisplayServer, request: Request) -> Response:
    """Export a table artifact as CSV."""
    card_id = request.path_params["card_id"]
    sort_col = request.query_params.get("sort")
    sort_asc = request.query_params.get("asc", "true").lower() == "true"
    search = request.query_params.get("search") or None

    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )

    try:
        csv_data = store.export_table_csv(
            card_id=card_id,
            sort_col=sort_col,
            sort_asc=sort_asc,
            search=search,
        )
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{card_id}.csv"',
            },
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"No table artifact for card {card_id}"}, status_code=404
        )


async def api_artifact(server: DisplayServer, request: Request) -> Response:
    """Return a raw artifact by card ID."""
    card_id = request.path_params["card_id"]
    store = server._resolve_store(card_id)
    if store is None:
        return JSONResponse(
            {"error": f"No artifact for card {card_id}"}, status_code=404
        )
    try:
        data = store.get_artifact(card_id)
        if isinstance(data, dict):
            return JSONResponse(data)
        media_type = "application/octet-stream"
        for ext, mime in (
            ("svg", "image/svg+xml"),
            ("png", "image/png"),
        ):
            if (store._artifacts_dir / f"{card_id}.{ext}").exists():
                media_type = mime
                break
        return Response(content=data, media_type=media_type)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"No artifact for card {card_id}"}, status_code=404
        )


async def api_session(server: DisplayServer, request: Request) -> JSONResponse:
    """Return session metadata."""
    if server.study_manager:
        studies = server.study_manager.list_studies()
        study_labels = [s["label"] for s in studies]
        return JSONResponse(
            {"session_id": server.session_id, "study_names": study_labels}
        )
    if server.store:
        meta_path = server.store._meta_path
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            return JSONResponse(meta)
        return JSONResponse({"session_id": server.store.session_id, "study_names": []})
    return JSONResponse({"session_id": server.session_id, "study_names": []})


async def api_health(server: DisplayServer, request: Request) -> JSONResponse:
    """Health check endpoint. No auth required."""
    uptime_seconds = (datetime.now(timezone.utc) - server._started_at).total_seconds()
    study_count = (
        len(server.study_manager.list_studies()) if server.study_manager else 0
    )
    return JSONResponse(
        {
            "status": "ok",
            "session_id": server.session_id,
            "uptime": round(uptime_seconds, 1),
            "version": server.version,
            "data_dir": str(server.data_dir),
            "study_count": study_count,
        }
    )


async def api_command(server: DisplayServer, request: Request) -> JSONResponse:
    """Unified command endpoint for pushing cards/sections/clears."""
    if not server._check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    cmd_type = body.get("type")

    if cmd_type == "card":
        card_data = body.get("card", {})
        message = {"type": "display.add", "card": card_data}
        card_id = card_data.get("card_id")
        study = card_data.get("study")
        if card_id and server.study_manager and study:
            dir_name = server.study_manager.get_dir_for_label(study)
            if not dir_name:
                server.study_manager.refresh()
                dir_name = server.study_manager.get_dir_for_label(study)
            if dir_name:
                server.study_manager.register_card(card_id, dir_name)
        await server.broadcast(message)
        return JSONResponse({"status": "ok"})

    elif cmd_type == "section":
        title = body.get("title", "")
        study = body.get("study")
        message = {
            "type": "display.section",
            "title": title,
            "study": study,
        }
        await server.broadcast(message)
        return JSONResponse({"status": "ok"})

    elif cmd_type == "update":
        card_id = body.get("card_id", "")
        card_data = body.get("card", {})
        message = {
            "type": "display.update",
            "card_id": card_id,
            "card": card_data,
        }
        await server.broadcast(message)
        return JSONResponse({"status": "ok"})

    return JSONResponse({"error": f"unknown command type: {cmd_type}"}, status_code=400)


async def api_shutdown(server: DisplayServer, request: Request) -> JSONResponse:
    """Gracefully shut down the server. Requires auth."""
    if not server._check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if server._server:
        server._server.should_exit = True
    return JSONResponse({"status": "shutting_down"})


async def api_owner(server: DisplayServer, request: Request) -> JSONResponse:
    """Add a process the server stops with once it has exited. Requires auth.

    Answers 409 when that process is not running and 503 when the server is
    already stopping because its owners exited.
    """
    from vitrine._utils import MAX_PID, OwnerRejected

    if not server._check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    pid = body.get("pid") if isinstance(body, dict) else None
    if type(pid) is not int or not 0 < pid <= MAX_PID:
        return JSONResponse(
            {"error": "pid must be a positive integer"}, status_code=400
        )

    try:
        owner_pids = server.add_owner(pid)
    except OwnerRejected as exc:
        status_code = 503 if exc.reason == "stopping" else 409
        return JSONResponse(
            {"error": exc.reason, "message": str(exc)}, status_code=status_code
        )
    return JSONResponse({"owner_pids": owner_pids})


async def api_response(server: DisplayServer, request: Request) -> JSONResponse:
    """Long-poll endpoint for blocking show() responses."""
    if not server._check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    card_id = request.path_params["card_id"]
    timeout = float(request.query_params.get("timeout", "300"))
    timeout = min(timeout, 1800)

    result = await server.wait_for_response(card_id, timeout)
    return JSONResponse(result)


async def api_events(server: DisplayServer, request: Request) -> JSONResponse:
    """Return and drain queued UI events. Requires auth."""
    if not server._check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with server._lock:
        events = list(server._event_queue)
        server._event_queue.clear()
    return JSONResponse(events)

"""Study CRUD, export, and file serving endpoints."""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from vitrine.server import DisplayServer


async def api_studies(server: DisplayServer, request: Request) -> JSONResponse:
    """List all studies with metadata and card counts."""
    if server.study_manager:
        server.study_manager.refresh()
        return JSONResponse(server.study_manager.list_studies())
    return JSONResponse([])


async def api_study_rename(server: DisplayServer, request: Request) -> JSONResponse:
    """Rename a study by label."""
    study = request.path_params["study"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    new_label = body.get("new_label", "").strip()
    if not new_label:
        return JSONResponse({"error": "new_label is required"}, status_code=400)
    if server.study_manager:
        renamed = server.study_manager.rename_study(study, new_label)
        if renamed:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {
                "error": f"Cannot rename: '{study}' not found or '{new_label}' already exists"
            },
            status_code=409,
        )
    return JSONResponse({"error": "No study manager"}, status_code=400)


async def api_study_context(server: DisplayServer, request: Request) -> JSONResponse:
    """Return a structured context summary for a study."""
    study = request.path_params["study"]
    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    server.study_manager.refresh()
    ctx = server.study_manager.build_context(study)
    cards = ctx.get("cards", [])
    card_ids = [c.get("card_id", "") for c in cards]

    current_selections = {}
    for cid in card_ids:
        sel = server._selections.get(cid, [])
        if sel:
            current_selections[cid] = sel

    for card_summary in cards:
        cid = card_summary.get("card_id", "")
        sel = server._selections.get(cid, [])
        if sel:
            card_summary["selection_count"] = len(sel)
            card_summary["selected_indices"] = sel

    pending_ids = {
        item.get("card_id", "")
        for item in ctx.get("pending_responses", [])
        if item.get("card_id")
    }
    for cid in card_ids:
        fut = server._pending_responses.get(cid)
        if fut and not fut.done() and cid not in pending_ids:
            pending_ids.add(cid)
            ctx.setdefault("pending_responses", []).append(
                {"card_id": cid, "title": None, "prompt": None}
            )

    ctx["current_selections"] = current_selections
    ctx["decisions"] = ctx.get("pending_responses", [])
    return JSONResponse(ctx)


async def api_study_delete(server: DisplayServer, request: Request) -> JSONResponse:
    """Delete a study by label."""
    study = request.path_params["study"]
    if server.study_manager:
        deleted = server.study_manager.delete_study(study)
        if deleted:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {"error": f"Study '{study}' not found"}, status_code=404
        )
    return JSONResponse({"error": "No study manager"}, status_code=400)


async def api_study_export(server: DisplayServer, request: Request) -> Response:
    """Export a specific study as HTML or JSON."""
    study = request.path_params["study"]
    fmt = request.query_params.get("format", "html")

    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    from vitrine.export import export_html_string, export_json_bytes

    server.study_manager.refresh()

    if fmt == "json":
        data = export_json_bytes(server.study_manager, study=study)
        filename = f"vitrine-export-{study}.zip"
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    html = export_html_string(server.study_manager, study=study)
    filename = f"vitrine-export-{study}.html"
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def api_study_files(server: DisplayServer, request: Request) -> JSONResponse:
    """List files in a study's output directory."""
    study = request.path_params["study"]
    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)
    server.study_manager.refresh()
    files = server.study_manager.list_output_files(study)
    return JSONResponse(files)


async def api_study_file(server: DisplayServer, request: Request) -> Response:
    """Serve a file from a study's output directory."""
    study = request.path_params["study"]
    filepath = request.path_params["filepath"]
    mode = request.query_params.get("mode", "preview")

    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    server.study_manager.refresh()
    resolved = server.study_manager.get_output_file_path(study, filepath)
    if resolved is None:
        return JSONResponse({"error": "File not found"}, status_code=404)

    suffix = resolved.suffix.lower()

    if mode == "download":
        content = resolved.read_bytes()
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{resolved.name}"',
            },
        )

    from vitrine._utils import IMAGE_MIME_TYPES, TEXT_EXTENSIONS

    if suffix == ".md":
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return Response(content=text, media_type="text/plain; charset=utf-8")

    if suffix in TEXT_EXTENSIONS:
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return Response(content=text, media_type="text/plain; charset=utf-8")

    if suffix in (".csv", ".parquet"):
        return server._preview_tabular_file(resolved, suffix)

    if suffix in IMAGE_MIME_TYPES:
        content = resolved.read_bytes()
        return Response(content=content, media_type=IMAGE_MIME_TYPES[suffix])

    if suffix == ".pdf":
        content = resolved.read_bytes()
        return Response(content=content, media_type="application/pdf")

    if suffix in (".html", ".htm"):
        content = resolved.read_bytes()
        return Response(content=content, media_type="text/html; charset=utf-8")

    content = resolved.read_bytes()
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{resolved.name}"',
        },
    )


async def api_study_files_archive(server: DisplayServer, request: Request) -> Response:
    """Download all output files as a zip archive."""
    study = request.path_params["study"]
    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    server.study_manager.refresh()
    output_dir = server.study_manager.get_output_dir(study)
    if output_dir is None or not output_dir.exists():
        return JSONResponse({"error": "No output directory"}, status_code=404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(output_dir.rglob("*")):
            if item.is_file() and not item.name.startswith("."):
                arcname = str(item.relative_to(output_dir))
                zf.write(item, arcname)

    filename = f"{study}-files.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def api_export(server: DisplayServer, request: Request) -> Response:
    """Export all studies as HTML or JSON."""
    fmt = request.query_params.get("format", "html")

    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    from vitrine.export import export_html_string, export_json_bytes

    server.study_manager.refresh()

    if fmt == "json":
        data = export_json_bytes(server.study_manager)
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="vitrine-export-all.zip"',
            },
        )

    html = export_html_string(server.study_manager)
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": 'attachment; filename="vitrine-export-all.html"',
        },
    )


async def api_all_files_archive(server: DisplayServer, request: Request) -> Response:
    """Download output files from all studies as a zip archive."""
    if not server.study_manager:
        return JSONResponse({"error": "No study manager"}, status_code=400)

    server.study_manager.refresh()
    studies = server.study_manager.list_studies()

    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for study_info in studies:
            label = study_info["label"]
            output_dir = server.study_manager.get_output_dir(label)
            if output_dir is None or not output_dir.exists():
                continue
            for item in sorted(output_dir.rglob("*")):
                if item.is_file() and not item.name.startswith("."):
                    arcname = f"{label}/{item.relative_to(output_dir)}"
                    zf.write(item, arcname)
                    file_count += 1

    if file_count == 0:
        return JSONResponse({"error": "No output files"}, status_code=404)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="vitrine-files-all.zip"',
        },
    )

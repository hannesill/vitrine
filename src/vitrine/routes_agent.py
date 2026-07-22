"""Agent create/run/status/cancel endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

import vitrine.dispatch as _dispatch_mod
from vitrine.dispatch import (
    cancel_agent,
    create_agent_card,
    get_agent_status,
    run_agent,
)

if TYPE_CHECKING:
    from vitrine.server import DisplayServer


async def api_create_agent(server: DisplayServer, request: Request) -> JSONResponse:
    """Create an agent card for a study."""
    study = request.path_params["study"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    task = body.get("task")
    _dispatch_mod._require_config()
    if task not in _dispatch_mod._TASK_CONFIG:
        available = ", ".join(sorted(_dispatch_mod._TASK_CONFIG))
        return JSONResponse(
            {
                "error": f"Unknown task: {task!r} (expected one of: {available})"
            },
            status_code=400,
        )

    try:
        info = await create_agent_card(task, study, server)
        return JSONResponse(
            {
                "status": "ok",
                "task": task,
                "study": study,
                "card_id": info.card_id,
            }
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_run_agent(server: DisplayServer, request: Request) -> JSONResponse:
    """Start an agent for an existing agent card."""
    card_id = request.path_params["card_id"]
    config = None
    try:
        body = await request.json()
        config = body if body else None
    except Exception:
        pass

    try:
        info = await run_agent(card_id, server, config=config)
        return JSONResponse(
            {
                "status": "ok",
                "card_id": card_id,
                "pid": info.pid,
            }
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_agent_handler(server: DisplayServer, request: Request) -> JSONResponse:
    """Handle GET (status) and DELETE (cancel) for an agent card."""
    card_id = request.path_params["card_id"]
    if request.method == "DELETE":
        cancelled = await cancel_agent(card_id, server)
        if cancelled:
            return JSONResponse({"status": "ok"})
        if server.study_manager:
            from vitrine._types import CardType

            for card in server.study_manager.list_all_cards():
                if card.card_id != card_id:
                    continue
                if card.card_type != CardType.AGENT:
                    break
                status = card.preview.get("status") if card.preview else None
                if status in ("running", "pending"):
                    new_preview = dict(card.preview)
                    new_preview["status"] = "failed"
                    new_preview["error"] = "Agent process no longer running"
                    _, store = server.study_manager.get_or_create_study(card.study)
                    if store:
                        store.update_card(card_id, preview=new_preview)
                    await server.broadcast(
                        {
                            "type": "display.update",
                            "card_id": card_id,
                            "card": {
                                "card_id": card_id,
                                "card_type": CardType.AGENT.value,
                                "study": card.study,
                                "title": card.title,
                                "preview": new_preview,
                            },
                        }
                    )
                    return JSONResponse({"status": "ok"})
                break
        return JSONResponse(
            {"error": "No running agent for this card"}, status_code=404
        )
    # GET — status
    status = get_agent_status(card_id, server)
    if status is None:
        return JSONResponse({"status": "none", "card_id": card_id})
    return JSONResponse(status)

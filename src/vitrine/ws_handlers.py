"""WebSocket endpoint and event handlers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from vitrine.artifacts import _serialize_card

if TYPE_CHECKING:
    from vitrine.server import DisplayServer

logger = logging.getLogger(__name__)


async def ws_endpoint(server: "DisplayServer", ws: WebSocket) -> None:
    """Handle a WebSocket connection."""
    await ws.accept()
    with server._lock:
        server._connections.append(ws)
    logger.debug("WebSocket client connected")

    # Replay existing cards on connect
    try:
        if server.study_manager:
            server.study_manager.refresh()
            cards = server.study_manager.list_all_cards()
        elif server.store:
            cards = server.store.list_cards()
        else:
            cards = []
        for card in cards:
            msg = {
                "type": "display.add",
                "card": _serialize_card(card),
            }
            await ws.send_json(msg)
        await ws.send_json({"type": "display.replay_done"})
    except Exception:
        logger.exception("Error replaying cards on WebSocket connect")

    try:
        while True:
            data = await ws.receive_json()
            await handle_ws_event(server, data)
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    except Exception:
        logger.debug("WebSocket connection closed")
    finally:
        with server._lock:
            if ws in server._connections:
                server._connections.remove(ws)


async def handle_ws_event(server: "DisplayServer", data: dict[str, Any]) -> None:
    """Route incoming WebSocket events from the browser."""
    msg_type = data.get("type")
    logger.debug(f"Received WebSocket message: {msg_type}")

    if msg_type != "vitrine.event":
        return

    event_type = data.get("event_type")
    card_id = data.get("card_id", "")
    payload = data.get("payload", {})

    if event_type == "response":
        await _handle_response(server, card_id, payload)
    elif event_type == "annotation":
        await _handle_annotation(server, card_id, payload)
    elif event_type == "rename":
        await _handle_rename(server, card_id, payload)
    elif event_type == "dismiss":
        await _handle_dismiss(server, card_id, payload)
    elif event_type == "delete":
        await _handle_delete(server, card_id, payload)
    elif event_type == "selection":
        server._selections[card_id] = payload.get("selected_indices", [])
        server._schedule_save_selections()
    else:
        await _handle_general_event(server, event_type, card_id, payload)


async def _handle_response(
    server: "DisplayServer", card_id: str, payload: dict[str, Any]
) -> None:
    """Resolve a pending blocking show() call."""
    action = payload.get("action", "confirm")
    message = payload.get("message")
    selected_rows = payload.get("selected_rows")
    columns = payload.get("columns")
    points = payload.get("points")
    form_values = payload.get("form_values", {})

    sel_store = server._resolve_store(card_id)
    artifact_id = None
    if selected_rows and columns:
        artifact_id = f"resp-{card_id}"
        if sel_store:
            sel_store.store_selection(artifact_id, selected_rows, columns)
        elif server.study_manager:
            server.study_manager.store_selection(
                artifact_id, selected_rows, columns
            )
    elif points:
        artifact_id = f"resp-{card_id}"
        if sel_store:
            sel_store.store_selection_json(artifact_id, {"points": points})
        elif server.study_manager:
            server.study_manager.store_selection_json(
                artifact_id, {"points": points}
            )

    summary = server._build_summary(card_id, selected_rows, points, columns)

    result = {
        "action": action,
        "card_id": card_id,
        "message": message,
        "artifact_id": artifact_id,
        "summary": summary,
        "values": form_values,
    }

    if sel_store is not None:
        sel_store.update_card(
            card_id,
            response_requested=False,
            response_action=action,
            response_message=message,
            response_values=form_values,
            response_summary=summary,
            response_artifact_id=artifact_id,
            response_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    future = server._pending_responses.get(card_id)
    if future and not future.done():
        future.set_result(result)


async def _handle_annotation(
    server: "DisplayServer", card_id: str, payload: dict[str, Any]
) -> None:
    """Handle researcher annotations: add, edit, delete."""
    action = payload.get("action")
    store = server._resolve_store(card_id)
    if store is None:
        return

    if action == "add":
        text = payload.get("text", "").strip()
        if not text:
            return
        annotation_id = uuid.uuid4().hex[:8]
        annotation = {
            "id": annotation_id,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        current = server._get_card_annotations(store, card_id)
        current.append(annotation)
        updated = store.update_card(card_id, annotations=current)
        if updated:
            await server.broadcast(
                {
                    "type": "display.update",
                    "card_id": card_id,
                    "card": _serialize_card(updated),
                }
            )

    elif action == "edit":
        ann_id = payload.get("annotation_id", "")
        new_text = payload.get("text", "")
        current = server._get_card_annotations(store, card_id)
        for ann in current:
            if ann.get("id") == ann_id:
                ann["text"] = new_text
                ann["timestamp"] = datetime.now(timezone.utc).isoformat()
                break
        updated = store.update_card(card_id, annotations=current)
        if updated:
            await server.broadcast(
                {
                    "type": "display.update",
                    "card_id": card_id,
                    "card": _serialize_card(updated),
                }
            )

    elif action == "delete":
        ann_id = payload.get("annotation_id", "")
        current = server._get_card_annotations(store, card_id)
        current = [a for a in current if a.get("id") != ann_id]
        updated = store.update_card(card_id, annotations=current)
        if updated:
            await server.broadcast(
                {
                    "type": "display.update",
                    "card_id": card_id,
                    "card": _serialize_card(updated),
                }
            )


async def _handle_rename(
    server: "DisplayServer", card_id: str, payload: dict[str, Any]
) -> None:
    """Handle card rename."""
    new_title = (payload.get("new_title") or "").strip()
    if new_title:
        store = server._resolve_store(card_id)
        if store is not None:
            updated = store.update_card(card_id, title=new_title)
            if updated:
                await server.broadcast(
                    {
                        "type": "display.update",
                        "card_id": card_id,
                        "card": _serialize_card(updated),
                    }
                )


async def _handle_dismiss(
    server: "DisplayServer", card_id: str, payload: dict[str, Any]
) -> None:
    """Handle card dismiss/un-dismiss."""
    dismissed = payload.get("dismissed", True)
    store = server._resolve_store(card_id)
    if store is not None:
        updated = store.update_card(card_id, dismissed=dismissed)
        if updated:
            await server.broadcast(
                {
                    "type": "display.update",
                    "card_id": card_id,
                    "card": _serialize_card(updated),
                }
            )


async def _handle_delete(
    server: "DisplayServer", card_id: str, payload: dict[str, Any]
) -> None:
    """Handle card delete/restore via WebSocket."""
    deleted = payload.get("deleted", True)
    if deleted and card_id in server.dispatches:
        from vitrine.dispatch import cancel_agent
        await cancel_agent(card_id, server)
    store = server._resolve_store(card_id)
    if store is not None:
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


async def _handle_general_event(
    server: "DisplayServer", event_type: str, card_id: str, payload: dict[str, Any]
) -> None:
    """Handle general events (row_click, point_select, etc.)."""
    from vitrine._types import DisplayEvent

    event = DisplayEvent(
        event_type=event_type,
        card_id=card_id,
        payload=payload,
    )
    with server._lock:
        callbacks = list(server._event_callbacks)
    for cb in callbacks:
        try:
            cb(event)
        except Exception:
            logger.debug(f"Event callback error for {event_type}")

    with server._lock:
        server._event_queue.append(
            {
                "event_type": event_type,
                "card_id": card_id,
                "payload": payload,
            }
        )
        if len(server._event_queue) > 1000:
            server._event_queue = server._event_queue[-500:]

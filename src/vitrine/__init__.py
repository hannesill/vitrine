"""vitrine: Visualization backend for code execution agents.

Provides a local display server that pushes visualizations to a browser tab.
Agents call show() to render DataFrames, charts, markdown, and more.

Quick Start:
    from vitrine import show

    show(df, title="Demographics")
    show("## Key Finding\\nMortality is 23%")
    show({"patients": 4238, "mortality": "23%"})
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

# Shared mutable state (used by both __init__ and client)
import vitrine._state as _st

# Client infrastructure (lifecycle, discovery, remote comms)
from vitrine import client as _client
from vitrine._types import (
    CardDescriptor,
    CardType,
    DisplayEvent,
    DisplayHandle,
    DisplayResponse,
    Form,
    Question,
)

__all__ = [
    "CardType",
    "DisplayEvent",
    "DisplayHandle",
    "DisplayResponse",
    "Form",
    "Question",
    "ask",
    "clean_studies",
    "confirm",
    "delete_study",
    "export",
    "get_card",
    "get_selection",
    "list_annotations",
    "list_studies",
    "on_event",
    "progress",
    "register_output_dir",
    "register_session",
    "section",
    "server_status",
    "show",
    "start",
    "stop",
    "stop_server",
    "study_context",
    "wait_for",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-exports from client (public API surface)
# ---------------------------------------------------------------------------

register_session = _client.register_session
start = _client.start
stop = _client.stop
stop_server = _client.stop_server
server_status = _client.server_status

# ---------------------------------------------------------------------------
# Proxy module-level state and private functions for backwards compat
# ---------------------------------------------------------------------------
# Test code may access vitrine._server, vitrine._store, etc.
# We proxy reads through __getattr__. Writes go through vitrine._state
# directly (tests should use vitrine._state.X = value or the existing
# attribute-set pattern which works because __getattr__ returns _state attrs).

_STATE_ATTRS = {
    "_lock", "_server", "_store", "_study_manager", "_session_id",
    "_remote_url", "_auth_token", "_event_callbacks", "_event_poll_thread",
    "_event_poll_stop",
}

_CLIENT_ATTRS = {
    "_get_vitrine_dir", "_migrate_if_needed", "_pid_file_path",
    "_lock_file_path", "_is_process_alive", "_health_check",
    "_discover_server", "_remote_command", "_push_remote",
    "_poll_remote_response", "_poll_remote_events", "_get_session_dir",
    "_ensure_study_manager", "_ensure_started", "_start_process",
    "_wait_for_card_response", "_study_url",
}


def __getattr__(name: str) -> Any:
    if name in _STATE_ATTRS:
        return getattr(_st, name)
    if name in _CLIENT_ATTRS:
        return getattr(_client, name)
    raise AttributeError(f"module 'vitrine' has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def show(
    obj: Any,
    title: str | None = None,
    description: str | None = None,
    *,
    study: str | None = None,
    source: str | None = None,
    replace: str | None = None,
    position: str | None = None,
    wait: bool = False,
    prompt: str | None = None,
    timeout: float = 600,
    actions: list[str] | None = None,
    controls: list[Any] | None = None,
) -> Any:
    """Push any displayable object to the browser.

    Returns a string-like card handle by default, or DisplayResponse when
    wait=True.

    Supported types:
    - pd.DataFrame -> interactive table (artifact-backed, paged)
    - plotly Figure -> interactive chart
    - matplotlib Figure -> static chart (SVG)
    - str -> markdown card
    - dict -> formatted key-value card
    - Form -> structured input card (freezes on confirm)
    - Other -> repr() fallback

    Auto-starts the display server on first call.

    Args:
        obj: Python object to display.
        title: Card title shown in header.
        description: Subtitle or context line.
        study: Group cards into a named study (for filtering).
        source: Provenance string (e.g., table name, query).
        replace: Card ID to update instead of appending.
        position: "top" to prepend instead of append.
        wait: If True, block until user responds in the browser.
        prompt: Question shown to the user (requires wait=True).
        timeout: Seconds to wait for response (default 600).
        actions: Named action buttons for decision cards. When provided,
            replaces the default Confirm button (requires wait=True).
        controls: List of form field primitives to attach as controls to
            a table or chart card. Creates a hybrid data+controls card.

    Returns:
        DisplayHandle (str subclass) when wait=False, DisplayResponse when
        wait=True.
    """
    # Wrap bare Question in a Form automatically
    if isinstance(obj, Question):
        obj = Form([obj])

    # Forms are always decision cards — force wait=True
    if isinstance(obj, Form):
        wait = True
        if prompt is None:
            prompt = title
    if controls:
        wait = True

    _client._ensure_started()

    from vitrine.artifacts import _serialize_card
    from vitrine.renderer import render

    # Resolve the store for this card via StudyManager
    store = _st._store  # backwards-compat fallback
    if _st._study_manager is not None:
        _label, store = _st._study_manager.get_or_create_study(study)
        # Use the resolved label for the card's study
        study = _label
        # Auto-register Claude session ID with this study
        register_session(study)

    if replace is not None:
        # Update an existing card in place
        # Resolve store for the card being replaced
        replace_store = store
        if _st._study_manager is not None:
            rs = _st._study_manager.get_store_for_card(replace)
            if rs:
                replace_store = rs
        card = render(
            obj,
            title=title,
            description=description,
            source=source,
            study=study,
            store=replace_store,
        )
        # Update the old card's entry in the store
        updated = replace_store.update_card(
            replace,
            **{
                "title": card.title,
                "description": card.description,
                "preview": card.preview,
                "artifact_id": card.artifact_id,
                "artifact_type": card.artifact_type,
            },
        )
        # Broadcast an update (not add) so frontend re-renders in place
        update_card = updated if updated else card
        if _st._remote_url:
            _client._remote_command(
                _st._remote_url,
                _st._auth_token,
                {
                    "type": "update",
                    "card_id": replace,
                    "card": _serialize_card(update_card),
                },
            )
        elif _st._server is not None:
            _st._server.push_update(replace, update_card)
        return card.card_id

    card = render(
        obj,
        title=title,
        description=description,
        source=source,
        study=study,
        store=store,
    )

    # Attach controls to the card preview for hybrid data+controls cards
    if controls:
        card.preview["controls"] = [c.to_dict() for c in controls]
        store.update_card(card.card_id, preview=card.preview)

    # Register the card in StudyManager's cross-study index
    if _st._study_manager is not None and study:
        dir_name = _st._study_manager.get_dir_for_label(study)
        if dir_name:
            _st._study_manager.register_card(card.card_id, dir_name)

    # Set interaction fields and update the stored card
    interaction_updates = {}
    if wait:
        card.response_requested = True
        interaction_updates["response_requested"] = True
        card.timeout = timeout
        interaction_updates["timeout"] = timeout
    if prompt is not None:
        card.prompt = prompt
        interaction_updates["prompt"] = prompt
    if actions is not None:
        card.actions = actions
        interaction_updates["actions"] = actions
    if interaction_updates:
        store.update_card(card.card_id, **interaction_updates)

    if _st._remote_url:
        _client._push_remote(_serialize_card(card))
    elif _st._server is not None:
        _st._server.push_card(card)

    if not wait:
        return DisplayHandle(card.card_id, url=_client._study_url(study), study=study)

    # Signal in terminal that we're waiting for browser input
    _wait_label = title or prompt or "decision card"
    _wait_url = _client._study_url(study)
    if _wait_url:
        print(f'Waiting for response on "{_wait_label}" in vitrine \u2192 {_wait_url}')
    else:
        print(f'Waiting for response on "{_wait_label}" in vitrine')

    # Blocking flow: wait for user response
    result = _client._wait_for_card_response(card.card_id, timeout)
    action = result.get("action", "timeout")

    # Terminal notification so the agent (and researcher) sees the outcome
    if action == "timeout":
        print(
            f'Timed out waiting for "{_wait_label}" '
            f'-- use wait_for("{card.card_id}") to re-attach'
        )
    else:
        print(f'Response received: {action} on "{_wait_label}"')

    return DisplayResponse(
        action=action,
        card_id=card.card_id,
        message=result.get("message"),
        summary=result.get("summary", ""),
        artifact_id=result.get("artifact_id"),
        values=result.get("values", {}),
        fields=card.preview.get("fields") or card.preview.get("controls"),
        _store=store,
    )


def section(title: str, study: str | None = None) -> None:
    """Insert a section divider in the display feed.

    Args:
        title: Section title.
        study: Optional study name for grouping.
    """
    _client._ensure_started()

    from vitrine._types import CardDescriptor, CardType
    from vitrine.renderer import _make_card_id, _make_timestamp

    # Resolve store via StudyManager if available
    store = _st._store
    if _st._study_manager is not None:
        _label, store = _st._study_manager.get_or_create_study(study)
        study = _label

    card = CardDescriptor(
        card_id=_make_card_id(),
        card_type=CardType.SECTION,
        title=title,
        timestamp=_make_timestamp(),
        study=study,
        preview={"title": title},
    )

    if store is not None:
        store.store_card(card)
    if _st._remote_url and _st._auth_token:
        _client._remote_command(
            _st._remote_url,
            _st._auth_token,
            {"type": "section", "title": title, "study": study},
        )
    elif _st._server is not None:
        _st._server.push_section(title, study=study)


def confirm(
    message: str,
    *,
    study: str | None = None,
    timeout: float = 600,
) -> bool:
    """Block until the researcher confirms or skips.

    Shorthand for ``show(message, wait=True, actions=["Confirm", "Skip"])``.

    Args:
        message: Markdown text shown in the decision card.
        study: Optional study name for grouping.
        timeout: Seconds to wait (default 600).

    Returns:
        True if confirmed, False if skipped or timed out.
    """
    r = show(message, wait=True, study=study, timeout=timeout)
    return r.action == "confirm"


def ask(
    question: str,
    options: list[str],
    *,
    study: str | None = None,
    timeout: float = 600,
) -> str:
    """Block until the researcher picks one of the given options.

    Shorthand for ``show(question, wait=True, actions=options)``.

    Args:
        question: Markdown text shown in the decision card.
        options: Action button labels (e.g. ``["SOFA", "APACHE III"]``).
        study: Optional study name for grouping.
        timeout: Seconds to wait (default 600).

    Returns:
        The chosen action string, or ``"timeout"`` if no response.
    """
    r = show(question, wait=True, actions=options, study=study, timeout=timeout)
    return r.message if r.message is not None else r.action


class ProgressContext:
    """Context manager that shows a progress card with auto-complete/fail.

    Used via the ``progress()`` factory function.
    """

    def __init__(self, title: str, *, study: str | None = None) -> None:
        self._title = title
        self._study = study
        self._card_id: str | None = None

    def __enter__(self) -> ProgressContext:
        handle = show(
            f"\u23f3 {self._title}",
            title=self._title,
            study=self._study,
        )
        self._card_id = str(handle)
        return self

    def __call__(self, message: str) -> None:
        """Update the progress card with a new message."""
        if self._card_id is not None:
            show(
                f"\u23f3 {message}",
                title=self._title,
                study=self._study,
                replace=self._card_id,
            )

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._card_id is not None:
            if exc_type is not None:
                show(
                    f"\u2717 {self._title} \u2014 failed",
                    title=self._title,
                    study=self._study,
                    replace=self._card_id,
                )
            else:
                show(
                    f"\u2713 {self._title} \u2014 complete",
                    title=self._title,
                    study=self._study,
                    replace=self._card_id,
                )
        # Never suppress exceptions
        return None


def progress(title: str, *, study: str | None = None) -> ProgressContext:
    """Show a progress card that auto-completes or marks failed on scope exit.

    Simple usage::

        with progress("Running DTW clustering"):
            do_clustering()

    With mid-run updates::

        with progress("Running analysis", study="sepsis-v1") as status:
            build_cohort()
            status("Applying exclusions...")
            apply_exclusions()

    Args:
        title: Label shown on the progress card.
        study: Optional study name for grouping.

    Returns:
        ProgressContext that can be used as a context manager.
    """
    return ProgressContext(title, study=study)


def wait_for(card_id: str, timeout: float = 600) -> DisplayResponse:
    """Re-attach to a previously posted blocking card and wait for its response.

    Use this after a ``show(..., wait=True)`` call has timed out. If the
    researcher has already responded (after the original timeout expired),
    the stored response is returned immediately. Otherwise the card's
    response UI is re-enabled in the browser and the call blocks until the
    researcher responds or the new timeout expires.

    Args:
        card_id: Card identifier (from ``DisplayResponse.card_id`` or
            ``DisplayHandle``).
        timeout: Seconds to wait for response (default 600).

    Returns:
        DisplayResponse with the researcher's action, message, and values.
    """
    from vitrine.artifacts import _serialize_card

    # Strip slug suffix (e.g. "a1b2c3-protocol" -> "a1b2c3")
    id_prefix = card_id.split("-")[0]

    # Look up the card
    card = get_card(id_prefix)
    if card is None:
        return DisplayResponse(
            action="error",
            card_id=card_id,
            message=f"Card not found: {card_id}",
        )

    # If the researcher already responded (after the original timeout),
    # return the stored response immediately.
    if card.response_action is not None:
        _label = card.title or card.prompt or card_id
        print(f'Response already received: {card.response_action} on "{_label}"')
        # Find the store for _store parameter
        _client._ensure_study_manager()
        store = None
        if _st._study_manager is not None:
            store = _st._study_manager.get_store_for_card(id_prefix)
        if store is None:
            store = _st._store
        return DisplayResponse(
            action=card.response_action,
            card_id=card.card_id,
            message=card.response_message,
            summary=card.response_summary or "",
            artifact_id=card.response_artifact_id,
            values=card.response_values or {},
            fields=card.preview.get("fields") or card.preview.get("controls"),
            _store=store,
        )

    # No response yet — re-enable the response UI and wait again.
    _client._ensure_study_manager()
    store = None
    if _st._study_manager is not None:
        store = _st._study_manager.get_store_for_card(id_prefix)
    if store is None:
        store = _st._store

    # Update card metadata to re-enable blocking
    if store is not None:
        store.update_card(
            card.card_id,
            response_requested=True,
            timeout=timeout,
        )
        card.response_requested = True
        card.timeout = timeout

    # Push update to frontend so it re-shows the response UI
    with _st._lock:
        server, url, token = _st._server, _st._remote_url, _st._auth_token

    if url and token:
        _client._remote_command(
            url,
            token,
            {
                "type": "update",
                "card_id": card.card_id,
                "card": _serialize_card(card),
            },
        )
    elif server is not None:
        server.push_update(card.card_id, card)

    _label = card.title or card.prompt or card_id
    print(f'Re-waiting for response on "{_label}" in vitrine')

    # Block again
    result = _client._wait_for_card_response(card.card_id, timeout)
    action = result.get("action", "timeout")

    if action == "timeout":
        print(
            f'Timed out waiting for "{_label}" '
            f'-- use wait_for("{card.card_id}") to re-attach'
        )
    else:
        print(f'Response received: {action} on "{_label}"')

    return DisplayResponse(
        action=action,
        card_id=card.card_id,
        message=result.get("message"),
        summary=result.get("summary", ""),
        artifact_id=result.get("artifact_id"),
        values=result.get("values", {}),
        fields=card.preview.get("fields") or card.preview.get("controls"),
        _store=store,
    )


def study_context(study: str) -> dict[str, Any]:
    """Get a structured context summary for agent re-orientation.

    Returns study metadata, cards, decisions made, pending responses,
    and current selections. Useful at the start of a new conversation
    turn to understand what has happened so far.

    Args:
        study: The study label to summarize.

    Returns:
        Dict with study, card_count, cards, decisions_made,
        pending_responses, and current_selections.
    """
    _client._ensure_study_manager()
    if _st._study_manager is not None:
        ctx = _st._study_manager.build_context(study)
        # In-process enrichment with live selection + pending response state
        if _st._server is not None:
            card_ids = [c.get("card_id", "") for c in ctx.get("cards", [])]
            current_selections = {}
            for cid in card_ids:
                sel = _st._server._selections.get(cid, [])
                if sel:
                    current_selections[cid] = sel
            ctx["current_selections"] = current_selections

            pending_ids = {
                item.get("card_id", "")
                for item in ctx.get("pending_responses", [])
                if item.get("card_id")
            }
            for cid in card_ids:
                pending = getattr(_st._server, "_pending_responses", {})
                fut = pending.get(cid) if isinstance(pending, dict) else None
                if fut and not fut.done() and cid not in pending_ids:
                    ctx.setdefault("pending_responses", []).append(
                        {"card_id": cid, "title": None, "prompt": None}
                    )
            ctx["decisions"] = ctx.get("pending_responses", [])

        # If remote server, try to get enriched version with selection counts
        with _st._lock:
            url = _st._remote_url

        if url:
            try:
                import urllib.request

                ctx_url = f"{url}/api/studies/{study}/context"
                req = urllib.request.Request(ctx_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())
            except Exception:
                pass

        return ctx
    return {
        "study": study,
        "card_count": 0,
        "cards": [],
        "decisions": [],
        "pending_responses": [],
        "decisions_made": [],
        "current_selections": {},
    }


def list_studies() -> list[dict[str, Any]]:
    """List all studies with metadata and card counts.

    Returns:
        List of dicts with label, dir_name, start_time, card_count.
    """
    _client._ensure_study_manager()
    if _st._study_manager is not None:
        return _st._study_manager.list_studies()
    return []


def delete_study(study: str) -> bool:
    """Delete a study by label.

    Args:
        study: The study label to delete.

    Returns:
        True if the study was deleted, False if not found.
    """
    _client._ensure_study_manager()
    if _st._study_manager is not None:
        return _st._study_manager.delete_study(study)
    return False


def clean_studies(older_than: str = "7d") -> int:
    """Remove studies older than a given age.

    Args:
        older_than: Age string (e.g., '7d', '24h', '0d' for all).

    Returns:
        Number of studies removed.
    """
    _client._ensure_study_manager()
    if _st._study_manager is not None:
        return _st._study_manager.clean_studies(older_than)
    return 0


def export(
    path: str,
    format: str = "html",
    study: str | None = None,
) -> str:
    """Export a study (or all studies) as a self-contained artifact.

    Args:
        path: Output file path.
        format: "html" (self-contained) or "json" (card index + artifacts zip).
        study: Specific study label to export, or None for all studies.

    Returns:
        Path to the written file.

    Raises:
        ValueError: If format is not "html" or "json".
    """
    if format not in ("html", "json"):
        raise ValueError(
            f"Unsupported export format: {format!r} (use 'html' or 'json')"
        )

    _client._ensure_study_manager()
    if _st._study_manager is None:
        raise RuntimeError("No study manager available for export")

    from vitrine.export import export_html, export_json

    if format == "html":
        result = export_html(_st._study_manager, path, study=study)
    else:
        result = export_json(_st._study_manager, path, study=study)

    return str(result)


def register_output_dir(
    path: str | Path | None = None,
    study: str | None = None,
) -> Path:
    """Register an output directory for a study.

    If path is None, creates and returns ``{study_dir}/output/``
    (self-contained alongside the study's cards). If path is a
    string/Path, stores it as an external reference and returns it.

    Args:
        path: External directory path, or None for self-contained.
        study: Study label. Creates the study if it doesn't exist.

    Returns:
        Path to the output directory (created if needed).
    """
    sm = _client._ensure_study_manager()
    label, _store = sm.get_or_create_study(study)
    return sm.register_output_dir(label, path)


def on_event(callback: Any) -> None:
    """Register a callback for UI events (row click, point select, etc.).

    The callback receives DisplayEvent instances with event_type, card_id,
    and payload fields. Common event types: 'row_click', 'point_select',
    'point_click'.

    Works in both in-process and remote server modes. For remote servers,
    starts a background polling thread that fetches events via REST.

    Args:
        callback: Function that receives DisplayEvent instances.
    """
    _client._ensure_started()

    with _st._lock:
        _st._event_callbacks.append(callback)
        server, url = _st._server, _st._remote_url

    if server is not None and hasattr(server, "register_event_callback"):
        # In-process server: register directly
        server.register_event_callback(callback)
    elif url is not None:
        # Remote server: start polling thread if not already running
        with _st._lock:
            need_start = _st._event_poll_thread is None or not _st._event_poll_thread.is_alive()
            if need_start:
                _st._event_poll_stop.clear()
                _st._event_poll_thread = threading.Thread(
                    target=_client._poll_remote_events, daemon=True
                )
                _st._event_poll_thread.start()


def get_card(card_id: str) -> CardDescriptor | None:
    """Look up a card descriptor by ID or prefix.

    Accepts full 12-char IDs, short prefixes, or slug-suffixed
    references like ``a1b2c3-my-title`` (the slug is stripped).

    Args:
        card_id: Card identifier, prefix, or slug-suffixed reference.

    Returns:
        CardDescriptor or None.
    """
    # Strip slug suffix (everything after first dash)
    id_prefix = card_id.split("-")[0]

    _client._ensure_study_manager()
    if _st._study_manager is not None:
        # Try prefix match via card index (avoids loading all cards)
        card = _st._study_manager.get_card_by_prefix(id_prefix)
        if card is not None:
            return card
    # Fallback to legacy store
    if _st._store is not None:
        for card in _st._store.list_cards():
            if card.card_id.startswith(id_prefix):
                return card
    return None


def list_annotations(
    study: str | None = None,
) -> list[dict[str, Any]]:
    """List all annotations, optionally filtered by study.

    Each returned dict contains the annotation fields (id, text, timestamp)
    plus ``card_id`` and ``card_title`` for context.

    Args:
        study: If provided, only include annotations from this study.

    Returns:
        List of annotation dicts, newest first.
    """
    _client._ensure_study_manager()
    with _st._lock:
        sm, store = _st._study_manager, _st._store
    cards: list[CardDescriptor] = []
    if sm is not None:
        cards = sm.list_all_cards(study=study)
    elif store is not None:
        cards = store.list_cards()

    annotations: list[dict[str, Any]] = []
    for card in cards:
        for ann in card.annotations:
            annotations.append(
                {
                    **ann,
                    "card_id": card.card_id,
                    "card_title": card.title,
                }
            )

    annotations.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
    return annotations


def get_selection(card_id: str) -> Any:
    """Get the currently selected rows for a table card.

    Reads the browser's checkbox/chart selection state that is passively
    synced to the server via WebSocket. Returns the matching rows as a
    DataFrame.

    Args:
        card_id: The card_id of the table or chart card.

    Returns:
        pd.DataFrame of selected rows, or empty DataFrame if nothing selected.
    """
    import pandas as pd

    _client._ensure_started()

    # In-process server: read directly from memory
    if _st._server is not None and hasattr(_st._server, "_selections"):
        indices = _st._server._selections.get(card_id, [])
        if not indices:
            return pd.DataFrame()
        # Find the store that holds this card's parquet
        store = None
        if _st._study_manager is not None:
            store = _st._study_manager.get_store_for_card(card_id)
        if store is None:
            store = _st._store
        if store is None:
            return pd.DataFrame()
        path = store._artifacts_dir / f"{card_id}.parquet"
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path)
        valid = [i for i in indices if 0 <= i < len(df)]
        if not valid:
            return pd.DataFrame()
        return df.iloc[valid].reset_index(drop=True)

    # Remote server: use REST endpoint
    with _st._lock:
        url = _st._remote_url

    if url:
        try:
            import urllib.request

            sel_url = f"{url}/api/table/{card_id}/selection"
            req = urllib.request.Request(sel_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if data.get("rows") and data.get("columns"):
                return pd.DataFrame(data["rows"], columns=data["columns"])
        except Exception:
            logger.warning(f"Failed to fetch selection for card {card_id} from remote")

    return pd.DataFrame()

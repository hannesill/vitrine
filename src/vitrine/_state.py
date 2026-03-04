"""Module-level mutable state shared between __init__ and client."""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_server: Any = None  # DisplayServer | None
_store: Any = None  # ArtifactStore | None (backwards-compat)
_study_manager: Any = None  # StudyManager | None
_session_id: str | None = None
_remote_url: str | None = None
_auth_token: str | None = None

# Event polling state (for remote server mode)
_event_callbacks: list[Any] = []
_event_poll_thread: threading.Thread | None = None
_event_poll_stop = threading.Event()

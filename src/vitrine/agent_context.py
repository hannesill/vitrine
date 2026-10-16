"""Recognise the agent session that runs this process (ADR-0002)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from vitrine._types import AgentContext


def current_agent_context() -> AgentContext:
    """Describe the agent session this process runs in.

    A host that launches agents sets ``VITRINE_AGENT_HOST``,
    ``VITRINE_AGENT_SESSION_ID`` and ``VITRINE_AGENT_TURN_ID``, and those
    win. Without them, a Claude Code session is recognised by
    ``CLAUDE_CODE_SESSION_ID``. Anything else is recorded as host
    ``unknown``.
    """
    recorded_at = datetime.now(timezone.utc).isoformat()
    host = os.environ.get("VITRINE_AGENT_HOST")
    if host:
        return AgentContext(
            host=host,
            session_id=os.environ.get("VITRINE_AGENT_SESSION_ID") or None,
            turn_id=os.environ.get("VITRINE_AGENT_TURN_ID") or None,
            recorded_at=recorded_at,
        )
    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if claude_session:
        return AgentContext(
            host="claude-code", session_id=claude_session, recorded_at=recorded_at
        )
    return AgentContext(host="unknown", recorded_at=recorded_at)

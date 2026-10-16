"""Shared test fixtures for the vitrine test suite."""

import pytest


@pytest.fixture(autouse=True)
def _no_inherited_owner(monkeypatch):
    """Keep an owner set by the shell running the tests (a host app's agent
    sets one) out of every CLI call and server the tests start."""
    monkeypatch.delenv("VITRINE_OWNER_PID", raising=False)


@pytest.fixture(autouse=True)
def _no_inherited_agent_session(monkeypatch):
    """Keep the agent session running the tests (a Claude Code session, a
    Lincei thread) off the cards the tests create."""
    for name in (
        "VITRINE_AGENT_HOST",
        "VITRINE_AGENT_SESSION_ID",
        "VITRINE_AGENT_TURN_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)

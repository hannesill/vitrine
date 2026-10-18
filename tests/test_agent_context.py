"""Tests for recognising the agent session a card comes from (ADR-0002)."""

from datetime import datetime, timedelta

from vitrine.agent_context import current_agent_context


def test_host_variables_win(monkeypatch):
    monkeypatch.setenv("VITRINE_AGENT_HOST", "lincei")
    monkeypatch.setenv("VITRINE_AGENT_SESSION_ID", "thread-1")
    monkeypatch.setenv("VITRINE_AGENT_TURN_ID", "turn-3")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    agent = current_agent_context()
    assert (agent.host, agent.session_id, agent.turn_id) == (
        "lincei",
        "thread-1",
        "turn-3",
    )


def test_host_without_a_turn(monkeypatch):
    monkeypatch.setenv("VITRINE_AGENT_HOST", "lincei")
    monkeypatch.setenv("VITRINE_AGENT_SESSION_ID", "thread-1")
    monkeypatch.setenv("VITRINE_AGENT_TURN_ID", "")
    assert current_agent_context().turn_id is None


def test_claude_code_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    agent = current_agent_context()
    assert (agent.host, agent.session_id, agent.turn_id) == (
        "claude-code",
        "claude-1",
        None,
    )


def test_older_claude_session_variable(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-old")
    agent = current_agent_context()
    assert (agent.host, agent.session_id) == ("claude-code", "claude-old")


def test_empty_host_falls_through(monkeypatch):
    monkeypatch.setenv("VITRINE_AGENT_HOST", "")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-1")
    assert current_agent_context().host == "claude-code"


def test_unknown_without_a_host():
    agent = current_agent_context()
    assert (agent.host, agent.session_id, agent.turn_id) == ("unknown", None, None)


def test_records_the_time_in_utc():
    recorded = datetime.fromisoformat(current_agent_context().recorded_at)
    assert recorded.utcoffset() == timedelta(0)

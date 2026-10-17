"""Shared test fixtures for the vitrine test suite."""

import pytest


@pytest.fixture(autouse=True)
def _no_inherited_owner(monkeypatch):
    """Keep an owner set by the shell running the tests (a host app's agent
    sets one) out of every CLI call and server the tests start."""
    monkeypatch.delenv("VITRINE_OWNER_PID", raising=False)

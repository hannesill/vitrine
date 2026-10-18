"""What hosts know about agent sessions, kept beside the studies (ADR-0002).

A card stores only its agent session's host and IDs. Titles, the model
behind each turn and a link back into the conversation change over time, so
hosts record them here, in ``agent_sessions.json`` in the data directory,
and the card page looks them up when it renders. Claude Code sessions need
no host: their transcript on disk has the same details.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SESSIONS_FILE = "agent_sessions.json"

# Schemes a browser would run as code or read from disk, never a link to a host.
_UNSAFE_SCHEMES = {"javascript", "data", "vbscript", "file", "blob"}

# A transcript is named by its session ID; anything else must not reach a glob.
_PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def session_key(host: str, session_id: str) -> str:
    return f"{host}/{session_id}"


# ---------------------------------------------------------------------------
# Records hosts supply
# ---------------------------------------------------------------------------


def record_agent_session(data_dir: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Store or update a host's details for one agent session.

    ``record`` has ``host`` and ``session_id``, and optionally ``title``,
    ``open_url`` (a link that opens the session in the host) and ``turns``
    (each with ``started_at`` and optionally ``turn_id``, ``provider`` and
    ``model``). Fields left out keep their stored values; turns are merged
    by ``turn_id`` (or ``started_at`` without one). Unknown fields are
    ignored. Returns the stored record.

    Raises:
        ValueError: when a field has the wrong type or value.
    """
    from vitrine._utils import keep_out_of_git

    incoming = _validated(record)
    data_dir = Path(data_dir)
    keep_out_of_git(data_dir)  # also creates it
    path = data_dir / SESSIONS_FILE
    with _locked(path):
        sessions = load_agent_sessions(data_dir)
        key = session_key(incoming["host"], incoming["session_id"])
        stored = sessions.get(key, {"turns": []})
        merged = {**stored, **{k: v for k, v in incoming.items() if k != "turns"}}
        merged["turns"] = _merge_turns(
            stored.get("turns", []), incoming.get("turns", [])
        )
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        sessions[key] = merged
        _write_json(path, sessions)
    return merged


def load_agent_sessions(data_dir: Path) -> dict[str, dict[str, Any]]:
    """All recorded sessions in ``data_dir``, keyed ``host/session_id``."""
    try:
        sessions = json.loads((Path(data_dir) / SESSIONS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return sessions if isinstance(sessions, dict) else {}


def _validated(record: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("host", "session_id"):
        value = record.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        out[name] = value
    if "/" in out["host"]:
        # Records are keyed host/session_id; a slash in the host makes keys collide.
        raise ValueError("host must not contain '/'")
    if "title" in record:
        if record["title"] is not None and not isinstance(record["title"], str):
            raise ValueError("title must be a string or null")
        out["title"] = record["title"]
    if "open_url" in record:
        url = record["open_url"]
        if url is not None:
            scheme = urlsplit(url).scheme.lower() if isinstance(url, str) else ""
            if not scheme or scheme in _UNSAFE_SCHEMES:
                raise ValueError(
                    "open_url must be a link with a scheme such as https: or the host's own"
                )
        out["open_url"] = url
    turns = record.get("turns", [])
    if not isinstance(turns, list):
        raise ValueError("turns must be a list")
    out["turns"] = [_validated_turn(t) for t in turns]
    return out


def _validated_turn(turn: Any) -> dict[str, Any]:
    if not isinstance(turn, Mapping):
        raise ValueError("each turn must be an object")
    started_at = turn.get("started_at")
    try:
        if not isinstance(started_at, str):
            raise ValueError
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"each turn needs started_at as an ISO timestamp, got {started_at!r}"
        ) from None
    out = {"started_at": started_at}
    for name in ("turn_id", "provider", "model"):
        value = turn.get(name)
        if value is None:
            continue  # left out: a turn recorded again keeps its stored value
        if not isinstance(value, str):
            raise ValueError(f"turn {name} must be a string")
        out[name] = value
    return out


def _merge_turns(stored: list[dict], incoming: list[dict]) -> list[dict]:
    by_key = {_turn_key(t): t for t in stored}
    for turn in incoming:
        key = _turn_key(turn)
        by_key[key] = {**by_key.get(key, {}), **turn}
    return sorted(by_key.values(), key=lambda t: _instant(t["started_at"]))


def _turn_key(turn: Mapping[str, Any]) -> tuple[str, str]:
    # Kept apart so a turn ID that looks like a timestamp never matches a
    # turn that has no ID and started at that time.
    if turn.get("turn_id"):
        return ("id", turn["turn_id"])
    return ("time", turn["started_at"])


def _instant(iso: str) -> datetime:
    when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


@contextlib.contextmanager
def _locked(path: Path):
    from vitrine._utils import lock_file, unlock_file

    with open(path.with_suffix(".lock"), "w") as fd:
        lock_file(fd, exclusive=True, blocking=True)
        try:
            yield
        finally:
            unlock_file(fd)


def _write_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Claude Code transcripts
# ---------------------------------------------------------------------------


def claude_code_session(session_id: str) -> dict[str, Any] | None:
    """Read a Claude Code session's title and models from its transcript.

    Transcripts live at ``~/.claude/projects/<project>/<session_id>.jsonl``
    (``CLAUDE_CONFIG_DIR`` moves ``~/.claude``). Each model the main agent
    switched to becomes a turn starting at its first reply. Returns None
    when there is no transcript.
    """
    if not _PLAIN_ID.fullmatch(session_id or ""):
        return None
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    matches = sorted((root / "projects").glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    try:
        stat = matches[0].stat()
    except OSError:
        return None
    title, turns = _read_transcript(str(matches[0]), stat.st_mtime_ns, stat.st_size)
    return {
        "host": "claude-code",
        "session_id": session_id,
        "title": title,
        "open_url": None,
        "turns": [dict(t) for t in turns],
    }


@functools.lru_cache(maxsize=32)
def _read_transcript(
    path: str, mtime_ns: int, size: int
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Title and model turns of one transcript; cached until the file changes."""
    title = None
    turns: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Most lines are tool output; only parse the few that matter.
                if (
                    '"assistant"' not in line
                    and "title" not in line
                    and '"summary"' not in line
                ):
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = entry.get("type")
                if kind == "custom-title" and entry.get("customTitle"):
                    title = entry["customTitle"]
                elif kind == "summary" and entry.get("summary"):
                    title = entry["summary"]
                elif kind == "assistant" and not entry.get("isSidechain"):
                    model = (entry.get("message") or {}).get("model")
                    when = entry.get("timestamp")
                    if not model or model.startswith("<") or not when:
                        continue
                    if not turns or turns[-1]["model"] != model:
                        turns.append(
                            {
                                "turn_id": None,
                                "started_at": when,
                                "provider": "claude",
                                "model": model,
                            }
                        )
    except OSError:
        return None, ()
    return title, tuple(turns)


# ---------------------------------------------------------------------------
# Lookup for the card page
# ---------------------------------------------------------------------------


def session_details(
    data_dir: Path | None, contexts: Iterable[Mapping[str, Any] | None]
) -> dict[str, dict[str, Any]]:
    """Details for each agent session among ``contexts``, keyed ``host/session_id``.

    A host's record wins; a Claude Code session without one is read from its
    transcript. Sessions nothing is known about are left out.
    """
    recorded = load_agent_sessions(data_dir) if data_dir else {}
    out: dict[str, dict[str, Any]] = {}
    for context in contexts:
        if not context or not context.get("session_id") or not context.get("host"):
            continue
        key = session_key(context["host"], context["session_id"])
        if key in out:
            continue
        record = recorded.get(key)
        if record is None and context["host"] == "claude-code":
            record = claude_code_session(context["session_id"])
        if record is not None:
            out[key] = record
    return out

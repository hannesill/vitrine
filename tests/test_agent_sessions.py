"""Tests for the agent-session details hosts supply (ADR-0002)."""

import json

import pytest

from vitrine.agent_sessions import (
    claude_code_session,
    load_agent_sessions,
    record_agent_session,
    session_details,
)


def _turn(turn_id, started_at, model="claude-opus-5-5"):
    return {
        "turn_id": turn_id,
        "started_at": started_at,
        "provider": "claude",
        "model": model,
    }


class TestRecordAgentSession:
    def test_stores_a_record(self, tmp_path):
        record = record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "thr_1",
                "title": "Sepsis cohort",
                "open_url": "lincei://thread/thr_1",
                "turns": [_turn("t1", "2026-10-18T10:00:00+00:00")],
            },
        )
        assert record["title"] == "Sepsis cohort"
        assert load_agent_sessions(tmp_path)["lincei/thr_1"] == record

    def test_a_later_record_updates_the_title_and_adds_turns(self, tmp_path):
        record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "thr_1",
                "title": "Draft",
                "turns": [_turn("t2", "2026-10-18T10:05:00+00:00")],
            },
        )
        record = record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "thr_1",
                "title": "Sepsis cohort",
                "turns": [
                    _turn("t1", "2026-10-18T10:00:00+00:00"),
                    _turn("t2", "2026-10-18T10:05:00+00:00", model="gpt-6"),
                ],
            },
        )
        assert record["title"] == "Sepsis cohort"
        assert [(t["turn_id"], t["model"]) for t in record["turns"]] == [
            ("t1", "claude-opus-5-5"),
            ("t2", "gpt-6"),
        ]

    def test_fields_left_out_are_kept(self, tmp_path):
        record_agent_session(
            tmp_path, {"host": "lincei", "session_id": "thr_1", "title": "Kept"}
        )
        record = record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "thr_1",
                "turns": [_turn("t1", "2026-10-18T10:00:00+00:00")],
            },
        )
        assert record["title"] == "Kept"

    def test_a_turn_recorded_again_keeps_fields_left_out(self, tmp_path):
        record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "s",
                "turns": [_turn("t1", "2026-10-18T10:00:00Z")],
            },
        )
        record = record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "s",
                "turns": [{"turn_id": "t1", "started_at": "2026-10-18T10:00:00Z"}],
            },
        )
        assert record["turns"][0]["model"] == "claude-opus-5-5"

    def test_a_turn_id_that_looks_like_a_timestamp_is_its_own_turn(self, tmp_path):
        record = record_agent_session(
            tmp_path,
            {
                "host": "lincei",
                "session_id": "s",
                "turns": [
                    {"started_at": "2026-10-18T10:00:00Z", "model": "a"},
                    {
                        "turn_id": "2026-10-18T10:00:00Z",
                        "started_at": "2026-10-18T10:05:00Z",
                        "model": "b",
                    },
                ],
            },
        )
        assert [t["model"] for t in record["turns"]] == ["a", "b"]

    def test_unknown_fields_are_ignored(self, tmp_path):
        record = record_agent_session(
            tmp_path, {"host": "lincei", "session_id": "thr_1", "colour": "blue"}
        )
        assert "colour" not in record

    @pytest.mark.parametrize(
        "record, message",
        [
            ({"session_id": "s"}, "host"),
            ({"host": "lincei"}, "session_id"),
            (
                {
                    "host": "lincei",
                    "session_id": "s",
                    "open_url": "javascript:alert(1)",
                },
                "open_url",
            ),
            (
                {"host": "lincei", "session_id": "s", "open_url": "no-scheme"},
                "open_url",
            ),
            (
                {"host": "lincei", "session_id": "s", "turns": [{"model": "x"}]},
                "started_at",
            ),
            (
                {
                    "host": "lincei",
                    "session_id": "s",
                    "turns": [{"started_at": "soon"}],
                },
                "started_at",
            ),
            ({"host": "lincei", "session_id": "s", "title": 3}, "title"),
            ({"host": "a/b", "session_id": "c"}, "host"),
            (
                {
                    "host": "lincei",
                    "session_id": "s",
                    "turns": [{"started_at": 20261018}],
                },
                "started_at",
            ),
        ],
    )
    def test_rejects_bad_records(self, tmp_path, record, message):
        with pytest.raises(ValueError, match=message):
            record_agent_session(tmp_path, record)
        assert load_agent_sessions(tmp_path) == {}

    def test_creates_the_data_directory(self, tmp_path):
        data_dir = tmp_path / ".vitrine"
        record_agent_session(data_dir, {"host": "lincei", "session_id": "s"})
        assert (data_dir / "agent_sessions.json").exists()
        assert "*" in (data_dir / ".gitignore").read_text().splitlines()


class TestClaudeCodeSession:
    @pytest.fixture
    def transcripts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        directory = tmp_path / "claude" / "projects" / "-Users-me-study"
        directory.mkdir(parents=True)
        return directory

    def _write(self, directory, session_id, entries):
        path = directory / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def test_reads_title_and_models(self, transcripts):
        self._write(
            transcripts,
            "abc-123",
            [
                {"type": "custom-title", "customTitle": "First title"},
                {
                    "type": "assistant",
                    "timestamp": "2026-10-18T10:00:00Z",
                    "message": {"model": "claude-sonnet-5-5"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-10-18T10:01:00Z",
                    "message": {"model": "claude-sonnet-5-5"},
                },
                {"type": "custom-title", "customTitle": "Lactate study"},
                {
                    "type": "assistant",
                    "timestamp": "2026-10-18T10:09:00Z",
                    "message": {"model": "claude-opus-5-5"},
                },
            ],
        )
        record = claude_code_session("abc-123")
        assert record["title"] == "Lactate study"
        assert [(t["started_at"], t["model"]) for t in record["turns"]] == [
            ("2026-10-18T10:00:00Z", "claude-sonnet-5-5"),
            ("2026-10-18T10:09:00Z", "claude-opus-5-5"),
        ]

    def test_skips_subagents_and_synthetic_messages(self, transcripts):
        self._write(
            transcripts,
            "abc-123",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-10-18T10:00:00Z",
                    "message": {"model": "claude-opus-5-5"},
                },
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "timestamp": "2026-10-18T10:01:00Z",
                    "message": {"model": "claude-haiku-5-5"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-10-18T10:02:00Z",
                    "message": {"model": "<synthetic>"},
                },
            ],
        )
        record = claude_code_session("abc-123")
        assert [t["model"] for t in record["turns"]] == ["claude-opus-5-5"]

    def test_older_summary_entries_give_the_title(self, transcripts):
        self._write(transcripts, "abc-123", [{"type": "summary", "summary": "Old"}])
        assert claude_code_session("abc-123")["title"] == "Old"

    def test_reflects_a_transcript_that_grew(self, transcripts):
        path = self._write(
            transcripts, "abc-123", [{"type": "custom-title", "customTitle": "A"}]
        )
        assert claude_code_session("abc-123")["title"] == "A"
        with path.open("a") as f:
            f.write(json.dumps({"type": "custom-title", "customTitle": "B"}) + "\n")
        assert claude_code_session("abc-123")["title"] == "B"

    def test_missing_transcript(self, transcripts):
        assert claude_code_session("not-there") is None

    @pytest.mark.parametrize("session_id", ["../abc", "a/b", "*", ""])
    def test_rejects_ids_that_are_not_plain_names(self, transcripts, session_id):
        assert claude_code_session(session_id) is None


class TestSessionDetails:
    def test_recorded_sessions_by_key(self, tmp_path):
        record_agent_session(
            tmp_path, {"host": "lincei", "session_id": "thr_1", "title": "T"}
        )
        details = session_details(
            tmp_path,
            [
                {"host": "lincei", "session_id": "thr_1"},
                {"host": "lincei", "session_id": "unknown"},
                {"host": "unknown", "session_id": None},
                None,
            ],
        )
        assert list(details) == ["lincei/thr_1"]

    def test_claude_code_sessions_come_from_transcripts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        directory = tmp_path / "claude" / "projects" / "p"
        directory.mkdir(parents=True)
        (directory / "abc.jsonl").write_text(
            json.dumps({"type": "custom-title", "customTitle": "From transcript"})
            + "\n"
        )
        details = session_details(
            tmp_path, [{"host": "claude-code", "session_id": "abc"}]
        )
        assert details["claude-code/abc"]["title"] == "From transcript"

    def test_without_a_data_directory(self):
        assert session_details(None, [{"host": "lincei", "session_id": "s"}]) == {}

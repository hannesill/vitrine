"""Tests for vitrine.dispatch — agent dispatch system.

Tests cover:
- DispatchInfo dataclass defaults and field behavior
- _parse_stream_event() for all event types
- build_prompt() with valid/invalid tasks
- _build_agent_preview() preview dict construction
- create_agent_card() card creation and persistence
- _update_agent_card() preview merging and broadcasting
- run_agent() validation and process spawning
- _stream_monitor() output parsing and card finalization
- cancel_agent() cancellation flow
- get_agent_status() status reporting
- reconcile_orphaned_agents() orphan cleanup
- cleanup_dispatches() server shutdown cleanup
- Sandbox creation and cleanup helpers
- _is_pid_alive() PID checks
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from vitrine._types import CardDescriptor, CardType
from vitrine.dispatch import (
    DispatchInfo,
    _build_agent_preview,
    _build_claude_args,
    _build_codex_args,
    _build_gemini_args,
    _cleanup_paper_workspace,
    _cleanup_sandbox,
    _create_paper_workspace,
    _create_sandbox,
    _is_pid_alive,
    _parse_codex_event,
    _parse_gemini_event,
    _parse_stream_event,
    _stream_monitor,
    _update_agent_card,
    _with_diagnostics,
    build_prompt,
    cancel_agent,
    cleanup_dispatches,
    create_agent_card,
    get_agent_status,
    reconcile_orphaned_agents,
    run_agent,
)
from vitrine.study_manager import StudyManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def study_mgr(tmp_path):
    """Create a StudyManager for testing."""
    display_dir = tmp_path / "display"
    display_dir.mkdir()
    return StudyManager(display_dir)


@pytest.fixture
def mock_server(study_mgr):
    """A minimal mock DisplayServer with study_manager and _dispatches."""
    server = MagicMock()
    server.study_manager = study_mgr
    server.dispatches = {}
    server.broadcast = AsyncMock()
    return server


@pytest.fixture
def mock_server_no_mgr():
    """A mock server with no study manager."""
    server = MagicMock()
    server.study_manager = None
    server.dispatches = {}
    server.broadcast = AsyncMock()
    return server


# ---------------------------------------------------------------------------
# DispatchInfo
# ---------------------------------------------------------------------------


class TestDispatchInfo:
    def test_defaults(self):
        info = DispatchInfo(task="reproduce", study="test-study")
        assert info.task == "reproduce"
        assert info.study == "test-study"
        assert info.card_id == ""
        assert info.model == "sonnet"
        assert info.budget is None
        assert info.status == "pending"
        assert info.process is None
        assert info.monitor_task is None
        assert info.pid is None
        assert info.error is None
        assert info.accumulated_output == ""
        assert info.extra == {}

    def test_custom_values(self):
        info = DispatchInfo(
            task="report",
            study="my-study",
            card_id="abc123",
            model="opus",
            budget=50.0,
            status="running",
        )
        assert info.model == "opus"
        assert info.budget == 50.0
        assert info.status == "running"

    def test_extra_dict_independence(self):
        """Each DispatchInfo gets its own extra dict."""
        a = DispatchInfo(task="reproduce", study="s1")
        b = DispatchInfo(task="reproduce", study="s2")
        a.extra["sandbox"] = "/tmp/a"
        assert "sandbox" not in b.extra


# ---------------------------------------------------------------------------
# _parse_stream_event
# ---------------------------------------------------------------------------


class TestParseStreamEvent:
    def test_invalid_json_returns_ignore(self):
        kind, text, usage = _parse_stream_event("not json at all")
        assert kind == "ignore"
        assert text == ""
        assert usage is None

    def test_empty_json_object_returns_ignore(self):
        kind, _text, _usage = _parse_stream_event("{}")
        assert kind == "ignore"

    def test_unknown_event_type_returns_ignore(self):
        event = json.dumps({"type": "system", "message": "starting"})
        kind, _text, _usage = _parse_stream_event(event)
        assert kind == "ignore"

    def test_assistant_text_block(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello world"}],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "text"
        assert text == "Hello world"

    def test_assistant_multiple_text_blocks(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Part 1. "},
                        {"type": "text", "text": "Part 2."},
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "text"
        assert text == "Part 1. Part 2."

    def test_assistant_tool_use_read(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/foo/bar/protocol.md"},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "protocol.md" in text

    def test_assistant_tool_use_glob(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Glob",
                            "input": {"pattern": "**/*.py"},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "**/*.py" in text

    def test_assistant_tool_use_grep(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Grep",
                            "input": {"pattern": "def main"},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "def main" in text

    def test_assistant_tool_use_bash(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "python scripts/01_cohort.py"},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "python scripts/01_cohort.py" in text

    def test_assistant_tool_use_bash_long_command_truncated(self):
        long_cmd = "x" * 200
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": long_cmd},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "..." in text
        # The displayed command should be truncated to 80 chars + "..."
        assert len(text) < len(long_cmd) + 50

    def test_assistant_tool_use_other(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "/tmp/out.txt"},
                        }
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "Write" in text

    def test_assistant_mixed_text_and_tool_use(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check the file."},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/foo/bar.py"},
                        },
                    ],
                },
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "tool_use"
        assert "Let me check the file." in text
        assert "bar.py" in text

    def test_assistant_with_usage(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hi"}],
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 100,
                    },
                },
            }
        )
        kind, _text, usage = _parse_stream_event(event)
        assert kind == "text"
        assert usage is not None
        assert usage["input_tokens"] == 1000
        assert usage["output_tokens"] == 200
        assert usage["cache_read"] == 500
        assert usage["cache_creation"] == 100

    def test_result_event(self):
        event = json.dumps(
            {
                "type": "result",
                "result": "Analysis complete.",
                "total_cost_usd": 0.05,
            }
        )
        kind, text, usage = _parse_stream_event(event)
        assert kind == "result"
        assert text == "Analysis complete."
        assert usage is not None
        assert usage["cost_usd"] == 0.05

    def test_result_event_with_model_usage(self):
        event = json.dumps(
            {
                "type": "result",
                "result": "Done",
                "modelUsage": {
                    "claude-sonnet-4-5-20250929": {
                        "inputTokens": 50000,
                        "outputTokens": 3000,
                        "cacheReadInputTokens": 10000,
                        "cacheCreationInputTokens": 2000,
                        "contextWindow": 200000,
                        "costUSD": 0.12,
                    }
                },
            }
        )
        kind, text, usage = _parse_stream_event(event)
        assert kind == "result"
        assert text == "Done"
        assert usage is not None
        assert usage["input_tokens"] == 50000
        assert usage["output_tokens"] == 3000
        assert usage["cache_read"] == 10000
        assert usage["context_window"] == 200000
        assert usage["cost_usd"] == 0.12

    def test_result_event_no_usage(self):
        event = json.dumps({"type": "result", "result": "OK"})
        kind, text, usage = _parse_stream_event(event)
        assert kind == "result"
        assert text == "OK"
        assert usage is None

    def test_assistant_empty_content(self):
        event = json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
            }
        )
        kind, text, _usage = _parse_stream_event(event)
        assert kind == "text"
        assert text == ""


# ---------------------------------------------------------------------------
# _parse_gemini_event
# ---------------------------------------------------------------------------


def _fold_stream(parse, lines):
    """Mimic _stream_monitor: concatenate displayed text, keep the last usage."""
    shown, usage = "", None
    for line in lines:
        kind, text, event_usage = parse(json.dumps(line))
        if event_usage:
            usage = event_usage
        if kind not in ("ignore", "result"):
            shown += text
    return shown, usage


# Event shapes from the Gemini CLI headless docs and
# packages/core/src/output/types.ts
_GEMINI_STREAM = [
    {"type": "init", "session_id": "abc123", "model": "gemini-2.5-pro"},
    {"type": "message", "role": "user", "content": "<the whole prompt>"},
    {
        "type": "tool_use",
        "tool_name": "read_file",
        "tool_id": "read_file-1",
        "parameters": {"file_path": "RESULTS.md"},
    },
    {"type": "tool_result", "tool_id": "read_file-1", "status": "success"},
    {"type": "message", "role": "assistant", "content": "## Report", "delta": True},
    {"type": "message", "role": "assistant", "content": "\nAll good.", "delta": True},
    {
        "type": "result",
        "status": "success",
        "stats": {
            "total_tokens": 250,
            "input_tokens": 50,
            "output_tokens": 200,
            "cached": 10,
            "duration_ms": 3000,
            "tool_calls": 1,
        },
    },
]


class TestParseGeminiEvent:
    def test_invalid_json_returns_ignore(self):
        kind, text, usage = _parse_gemini_event("not json")
        assert kind == "ignore"
        assert text == ""
        assert usage is None

    def test_stream_shows_assistant_text_and_tools_but_not_the_prompt(self):
        shown, usage = _fold_stream(_parse_gemini_event, _GEMINI_STREAM)
        assert shown == "\n\n> *Using read_file...*\n\n## Report\nAll good."
        assert "<the whole prompt>" not in shown
        assert usage == {"input_tokens": 50, "output_tokens": 200}

    def test_result_has_no_text_so_streamed_output_is_kept(self):
        kind, text, _ = _parse_gemini_event(json.dumps(_GEMINI_STREAM[-1]))
        assert (kind, text) == ("result", "")

    def test_failed_result_is_fatal_with_usage(self):
        line = {
            "type": "result",
            "status": "error",
            "error": {"type": "FatalTurnLimitedError", "message": "Turn limit"},
            "stats": {"input_tokens": 5, "output_tokens": 1},
        }
        kind, text, usage = _parse_gemini_event(json.dumps(line))
        assert (kind, text) == ("fatal", "Turn limit")
        assert usage == {"input_tokens": 5, "output_tokens": 1}

    def test_warning_event(self):
        line = {"type": "error", "severity": "warning", "message": "Loop detected"}
        kind, text, _ = _parse_gemini_event(json.dumps(line))
        assert kind == "error"
        assert "Warning: Loop detected" in text


# ---------------------------------------------------------------------------
# _parse_codex_event
# ---------------------------------------------------------------------------

# Event shapes from the Codex non-interactive docs and
# codex-rs/exec/src/exec_events.rs
_CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "reasoning", "text": "x"},
    },
    {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "aggregated_output": "docs\nsdk",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "file_change",
            "changes": [{"path": "/w/generate_slides.py", "kind": "add"}],
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_3",
            "type": "agent_message",
            "text": "Repo contains docs and sdk.",
        },
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 24763,
            "cached_input_tokens": 24448,
            "output_tokens": 122,
        },
    },
]


class TestParseCodexEvent:
    def test_invalid_json_returns_ignore(self):
        kind, text, usage = _parse_codex_event("not json")
        assert kind == "ignore"
        assert text == ""
        assert usage is None

    def test_stream_shows_agent_message_and_tools(self):
        shown, usage = _fold_stream(_parse_codex_event, _CODEX_STREAM)
        assert shown == (
            "\n\n> *Running `bash -lc ls`...*\n\n"
            "\n\n> *Changed `generate_slides.py`*\n\n"
            "Repo contains docs and sdk.\n\n"
        )
        # input_tokens already includes the cached tokens
        assert usage == {"input_tokens": 24763, "output_tokens": 122}

    def test_turn_completed_has_no_text_so_streamed_output_is_kept(self):
        kind, text, _ = _parse_codex_event(json.dumps(_CODEX_STREAM[-1]))
        assert (kind, text) == ("result", "")

    def test_mcp_tool_call(self):
        line = {
            "type": "item.started",
            "item": {"id": "i", "type": "mcp_tool_call", "server": "m4", "tool": "sql"},
        }
        kind, text, _ = _parse_codex_event(json.dumps(line))
        assert kind == "tool_use"
        assert "Using m4.sql..." in text

    def test_turn_failed_is_fatal(self):
        line = {"type": "turn.failed", "error": {"message": "quota"}}
        assert _parse_codex_event(json.dumps(line)) == ("fatal", "quota", None)

    @pytest.mark.parametrize(
        "line, expected",
        [
            ({"type": "error", "message": "stream lost"}, "Error: stream lost"),
            (
                {"type": "item.completed", "item": {"type": "error", "message": "x"}},
                "Warning: x",
            ),
        ],
    )
    def test_errors(self, line, expected):
        kind, text, _ = _parse_codex_event(json.dumps(line))
        assert kind == "error"
        assert expected in text


@pytest.mark.parametrize(
    "parse", [_parse_stream_event, _parse_gemini_event, _parse_codex_event]
)
@pytest.mark.parametrize("line", ["42", "null", '"text"', "[1, 2]", "true"])
def test_json_that_is_not_an_event_is_ignored(parse, line):
    """stderr shares the stream, so a line can be JSON that is not an object."""
    assert parse(line) == ("ignore", "", None)


# ---------------------------------------------------------------------------
# Backend argument builders
# ---------------------------------------------------------------------------


class TestBuildArgs:
    # Over every per-argument limit: Windows' 32,767-char command line and
    # Linux's 128 KiB MAX_ARG_STRLEN
    _BIG_PROMPT = "x" * 200_000

    @pytest.mark.parametrize(
        "build", [_build_claude_args, _build_gemini_args, _build_codex_args]
    )
    def test_prompt_goes_through_stdin(self, build):
        args, stdin = build("cli", self._BIG_PROMPT, "m", "Read", None)
        assert stdin == self._BIG_PROMPT.encode()
        assert all(len(a) < 1000 for a in args)
        assert not any(a.startswith("@") for a in args)

    def test_codex_reads_stdin_outside_git_repos(self):
        args, _ = _build_codex_args("codex", "p", "gpt-5-codex", "Read", None)
        assert args[:2] == ["codex", "exec"]
        assert args[-1] == "-"
        assert "--skip-git-repo-check" in args
        assert "--json" in args
        # Removed from current Codex CLI, which rejects it
        assert "--full-auto" not in args

    def test_gemini_runs_headless_on_stdin(self):
        args, _ = _build_gemini_args("gemini", "p", "gemini-2.5-pro", "Read", None)
        assert args == [
            "gemini",
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--model",
            "gemini-2.5-pro",
        ]


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_unknown_task_raises(self, study_mgr):
        with pytest.raises(ValueError, match="Unknown dispatch task"):
            build_prompt("nonexistent", "test-study", study_mgr)

    def test_valid_task_includes_skill_content(self, study_mgr):
        # Create a study with a card so there's context
        _, _store = study_mgr.get_or_create_study("test-study")
        prompt = build_prompt("reproduce", "test-study", study_mgr)
        assert "Study:" in prompt
        assert "test-study" in prompt
        assert "Output directory:" in prompt
        assert "scripts/" in prompt

    def test_valid_task_report(self, study_mgr):
        _, _store = study_mgr.get_or_create_study("my-study")
        prompt = build_prompt("report", "my-study", study_mgr)
        assert "my-study" in prompt

    def test_sandbox_note_included(self, study_mgr, tmp_path):
        work_dir = tmp_path / "sandbox"
        work_dir.mkdir()
        prompt = build_prompt("reproduce", "test-study", study_mgr, work_dir=work_dir)
        assert "Sandbox" in prompt
        assert str(work_dir) in prompt

    def test_additional_prompt_included(self, study_mgr):
        prompt = build_prompt(
            "reproduce",
            "test-study",
            study_mgr,
            additional_prompt="Focus on the mortality analysis",
        )
        assert "Additional Instructions" in prompt
        assert "Focus on the mortality analysis" in prompt

    def test_additional_prompt_blank_not_included(self, study_mgr):
        prompt = build_prompt(
            "reproduce", "test-study", study_mgr, additional_prompt="   "
        )
        assert "Additional Instructions" not in prompt

    def test_missing_skill_file_raises(self, study_mgr, monkeypatch):
        """If the skill file is missing on disk, build_prompt raises."""
        monkeypatch.setattr(
            "vitrine.dispatch._SKILLS_DIR",
            Path("/nonexistent/skills"),
        )
        with pytest.raises(ValueError, match="Skill file not found"):
            build_prompt("reproduce", "test-study", study_mgr)

    def test_output_dir_none_when_no_registration(self, study_mgr):
        prompt = build_prompt("reproduce", "test-study", study_mgr)
        assert "(none)" in prompt


# ---------------------------------------------------------------------------
# _build_agent_preview
# ---------------------------------------------------------------------------


class TestBuildAgentPreview:
    def test_default_preview(self):
        preview = _build_agent_preview("reproduce", "pending")
        assert preview["task"] == "reproduce"
        assert preview["status"] == "pending"
        assert preview["model"] == "sonnet"
        assert isinstance(preview["tools"], list)
        assert "Bash" in preview["tools"]
        assert preview["output"] == ""
        assert preview["started_at"] is None
        assert preview["error"] is None
        assert preview["usage"]["input_tokens"] == 0

    def test_custom_model(self):
        preview = _build_agent_preview("report", "running", model="opus")
        assert preview["model"] == "opus"
        assert preview["usage"]["context_window"] == 200_000

    def test_report_task_tools(self):
        preview = _build_agent_preview("report", "pending")
        assert "Read" in preview["tools"]
        assert "Glob" in preview["tools"]
        assert "Grep" in preview["tools"]
        # Report doesn't have Bash
        assert "Bash" not in preview["tools"]

    def test_unknown_task_fallback(self):
        """Unknown task produces empty tools and empty prompt."""
        preview = _build_agent_preview("unknown_task", "pending")
        assert preview["tools"] == []
        assert preview["full_prompt"] == ""

    def test_prompt_preview_truncated(self):
        preview = _build_agent_preview("reproduce", "pending")
        # The full prompt is the SKILL.md content
        assert len(preview["prompt_preview"]) <= 203  # 200 + "..."
        assert len(preview["full_prompt"]) > 200  # Real skill file is larger

    def test_additional_prompt_included(self):
        preview = _build_agent_preview(
            "reproduce", "pending", additional_prompt="Extra instructions"
        )
        assert preview["additional_prompt"] == "Extra instructions"

    def test_budget_included(self):
        preview = _build_agent_preview("reproduce", "pending", budget=25.0)
        assert preview["budget"] == 25.0


# ---------------------------------------------------------------------------
# create_agent_card
# ---------------------------------------------------------------------------


class TestCreateAgentCard:
    async def test_creates_card_and_returns_dispatch_info(self, mock_server):
        info = await create_agent_card("reproduce", "test-study", mock_server)
        assert info.task == "reproduce"
        assert info.study == "test-study"
        assert info.status == "pending"
        assert info.card_id != ""
        assert len(info.card_id) == 12

    async def test_card_persisted_in_store(self, mock_server, study_mgr):
        info = await create_agent_card("reproduce", "test-study", mock_server)
        _, store = study_mgr.get_or_create_study("test-study")
        cards = store.list_cards()
        agent_cards = [c for c in cards if c.card_type == CardType.AGENT]
        assert len(agent_cards) == 1
        assert agent_cards[0].card_id == info.card_id

    async def test_card_broadcast(self, mock_server):
        await create_agent_card("reproduce", "test-study", mock_server)
        mock_server.broadcast.assert_called_once()
        call_args = mock_server.broadcast.call_args[0][0]
        assert call_args["type"] == "display.add"
        assert call_args["card"]["card_type"] == "agent"

    async def test_dispatch_registered(self, mock_server):
        info = await create_agent_card("reproduce", "test-study", mock_server)
        assert info.card_id in mock_server.dispatches
        assert mock_server.dispatches[info.card_id] is info

    async def test_unknown_task_raises(self, mock_server):
        with pytest.raises(ValueError, match="Unknown task"):
            await create_agent_card("unknown", "test-study", mock_server)

    async def test_no_study_manager_raises(self, mock_server_no_mgr):
        with pytest.raises(ValueError, match="No study manager"):
            await create_agent_card("reproduce", "test-study", mock_server_no_mgr)

    async def test_report_task_card_title(self, mock_server):
        await create_agent_card("report", "my-study", mock_server)
        call_args = mock_server.broadcast.call_args[0][0]
        assert call_args["card"]["title"] == "Study Report"

    async def test_reproduce_task_card_title(self, mock_server):
        await create_agent_card("reproduce", "my-study", mock_server)
        call_args = mock_server.broadcast.call_args[0][0]
        assert call_args["card"]["title"] == "Reproducibility Audit"


# ---------------------------------------------------------------------------
# cancel_agent
# ---------------------------------------------------------------------------


class TestCancelAgent:
    async def test_cancel_not_found_returns_false(self, mock_server):
        result = await cancel_agent("nonexistent", mock_server)
        assert result is False

    async def test_cancel_not_running_returns_false(self, mock_server):
        info = DispatchInfo(
            task="reproduce", study="s1", card_id="abc", status="completed"
        )
        mock_server.dispatches["abc"] = info
        result = await cancel_agent("abc", mock_server)
        assert result is False

    async def test_cancel_running_returns_true(self, mock_server, study_mgr):
        # Create a study so the store exists
        _, store = study_mgr.get_or_create_study("s1")

        # Create a real agent card in the store
        card = CardDescriptor(
            card_id="abc",
            card_type=CardType.AGENT,
            title="Reproducibility Audit",
            study="s1",
            preview={"status": "running", "output": "some output"},
        )
        store.store_card(card)
        study_mgr.register_card("abc", study_mgr._label_to_dir["s1"])

        # Set up dispatch info with mock process
        proc = MagicMock(spec=subprocess.Popen)
        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="abc",
            status="running",
            started_at="2024-01-01T00:00:00+00:00",
        )
        info.process = proc
        info.accumulated_output = "partial output"
        mock_server.dispatches["abc"] = info

        result = await cancel_agent("abc", mock_server)
        assert result is True
        assert info.status == "cancelled"
        assert info.completed_at is not None
        proc.terminate.assert_called_once()

    async def test_cancel_preserves_output(self, mock_server, study_mgr):
        """Cancelled agent card shows accumulated output."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="abc",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "running", "output": ""},
        )
        store.store_card(card)
        study_mgr.register_card("abc", study_mgr._label_to_dir["s1"])

        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="abc",
            status="running",
            started_at="2024-01-01T00:00:00+00:00",
        )
        info.process = MagicMock(spec=subprocess.Popen)
        info.accumulated_output = "## Progress\nStep 1 done."
        mock_server.dispatches["abc"] = info

        await cancel_agent("abc", mock_server)

        # Check broadcast includes preserved output
        update_call = None
        for call in mock_server.broadcast.call_args_list:
            msg = call[0][0]
            if msg.get("type") == "display.update":
                update_call = msg
        assert update_call is not None
        preview = update_call["card"]["preview"]
        assert "## Progress" in preview["output"]
        assert "Cancelled by user" in preview["output"]

    async def test_cancel_cleans_sandbox(self, mock_server, study_mgr, tmp_path):
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="xyz",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "running"},
        )
        store.store_card(card)
        study_mgr.register_card("xyz", study_mgr._label_to_dir["s1"])

        sandbox = tmp_path / "sandbox_reproduce"
        sandbox.mkdir()
        (sandbox / "test.txt").write_text("data")

        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="xyz",
            status="running",
            started_at="2024-01-01T00:00:00+00:00",
        )
        info.process = MagicMock(spec=subprocess.Popen)
        info.extra["sandbox"] = str(sandbox)
        mock_server.dispatches["xyz"] = info

        await cancel_agent("xyz", mock_server)
        assert not sandbox.exists()


# ---------------------------------------------------------------------------
# get_agent_status
# ---------------------------------------------------------------------------


class TestGetAgentStatus:
    def test_unknown_card_returns_none(self, mock_server):
        assert get_agent_status("nonexistent", mock_server) is None

    def test_known_card_returns_dict(self, mock_server):
        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="abc",
            status="running",
            pid=12345,
            model="opus",
        )
        mock_server.dispatches["abc"] = info
        result = get_agent_status("abc", mock_server)
        assert result is not None
        assert result["status"] == "running"
        assert result["pid"] == 12345
        assert result["model"] == "opus"
        assert result["task"] == "reproduce"
        assert result["study"] == "s1"


# ---------------------------------------------------------------------------
# reconcile_orphaned_agents
# ---------------------------------------------------------------------------


class TestReconcileOrphanedAgents:
    def test_no_study_manager_returns_zero(self, mock_server_no_mgr):
        assert reconcile_orphaned_agents(mock_server_no_mgr) == 0

    def test_no_agent_cards_returns_zero(self, mock_server):
        assert reconcile_orphaned_agents(mock_server) == 0

    def test_fixes_orphaned_running_agent(self, mock_server, study_mgr):
        """Agent card stuck in 'running' with no dispatch gets marked failed."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="orphan1",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "running", "output": "partial"},
        )
        store.store_card(card)

        fixed = reconcile_orphaned_agents(mock_server)
        assert fixed == 1

        # Verify the card was updated
        cards = store.list_cards()
        updated = next(c for c in cards if c.card_id == "orphan1")
        assert updated.preview["status"] == "failed"
        assert "Server restarted" in updated.preview["error"]

    def test_skips_pending_agent(self, mock_server, study_mgr):
        """Pending agent cards are not touched."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="pending1",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "pending"},
        )
        store.store_card(card)

        fixed = reconcile_orphaned_agents(mock_server)
        assert fixed == 0

    def test_skips_completed_agent(self, mock_server, study_mgr):
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="done1",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "completed"},
        )
        store.store_card(card)

        fixed = reconcile_orphaned_agents(mock_server)
        assert fixed == 0

    def test_skips_running_agent_with_active_dispatch(self, mock_server, study_mgr):
        """Running agent with a live dispatch entry is NOT fixed."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="live1",
            card_type=CardType.AGENT,
            title="Test",
            study="s1",
            preview={"status": "running"},
        )
        store.store_card(card)
        # Register a dispatch for this card
        mock_server.dispatches["live1"] = DispatchInfo(
            task="reproduce", study="s1", card_id="live1", status="running"
        )

        fixed = reconcile_orphaned_agents(mock_server)
        assert fixed == 0

    def test_skips_non_agent_cards(self, mock_server, study_mgr):
        """Non-agent cards are skipped."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="md1",
            card_type=CardType.MARKDOWN,
            title="Test",
            study="s1",
            preview={"text": "hello"},
        )
        store.store_card(card)

        fixed = reconcile_orphaned_agents(mock_server)
        assert fixed == 0


# ---------------------------------------------------------------------------
# cleanup_dispatches
# ---------------------------------------------------------------------------


class TestCleanupDispatches:
    def test_terminates_running_processes(self, mock_server):
        proc = MagicMock(spec=subprocess.Popen)
        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="abc",
            status="running",
        )
        info.process = proc
        mock_server.dispatches["abc"] = info

        cleanup_dispatches(mock_server)
        proc.terminate.assert_called_once()
        assert info.status == "cancelled"
        assert len(mock_server.dispatches) == 0

    def test_skips_non_running(self, mock_server):
        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="abc",
            status="completed",
        )
        info.process = MagicMock(spec=subprocess.Popen)
        mock_server.dispatches["abc"] = info

        cleanup_dispatches(mock_server)
        info.process.terminate.assert_not_called()

    def test_cleans_sandbox(self, mock_server, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("x")

        info = DispatchInfo(
            task="reproduce", study="s1", card_id="abc", status="completed"
        )
        info.extra["sandbox"] = str(sandbox)
        mock_server.dispatches["abc"] = info

        cleanup_dispatches(mock_server)
        assert not sandbox.exists()

    def test_clears_dispatches_dict(self, mock_server):
        mock_server.dispatches["a"] = DispatchInfo(
            task="reproduce", study="s1", card_id="a", status="completed"
        )
        mock_server.dispatches["b"] = DispatchInfo(
            task="report", study="s2", card_id="b", status="failed"
        )
        cleanup_dispatches(mock_server)
        assert len(mock_server.dispatches) == 0

    def test_handles_terminate_oserror(self, mock_server):
        """OSError from terminate is silently caught."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.terminate.side_effect = OSError("Process already dead")
        info = DispatchInfo(
            task="reproduce", study="s1", card_id="abc", status="running"
        )
        info.process = proc
        mock_server.dispatches["abc"] = info

        cleanup_dispatches(mock_server)  # Should not raise
        assert info.status == "cancelled"


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


class TestSandboxHelpers:
    def test_create_sandbox(self, tmp_path):
        output_dir = tmp_path / "study-output"
        output_dir.mkdir()
        (output_dir / "scripts").mkdir()
        (output_dir / "scripts" / "01_cohort.py").write_text("print('hello')")
        (output_dir / "data").mkdir()
        (output_dir / "data" / "cohort.parquet").write_bytes(b"fake-parquet")

        sandbox = _create_sandbox(output_dir, "card1")
        assert sandbox.name == "study-output_reproduce_card1"
        assert sandbox.exists()
        assert (sandbox / "scripts" / "01_cohort.py").exists()
        assert (sandbox / "scripts" / "01_cohort.py").read_text() == "print('hello')"
        assert (sandbox / "data" / "cohort.parquet").exists()

    def test_create_sandbox_replaces_existing(self, tmp_path):
        output_dir = tmp_path / "study-output"
        output_dir.mkdir()
        (output_dir / "file.txt").write_text("new")

        sandbox = output_dir.parent / "study-output_reproduce_card1"
        sandbox.mkdir()
        (sandbox / "old_file.txt").write_text("old")

        result = _create_sandbox(output_dir, "card1")
        assert result == sandbox
        assert not (sandbox / "old_file.txt").exists()
        assert (sandbox / "file.txt").read_text() == "new"

    def test_runs_on_one_study_get_separate_sandboxes(self, tmp_path):
        """A second run neither replaces nor shares the first run's sandbox."""
        output_dir = tmp_path / "study-output"
        output_dir.mkdir()
        (output_dir / "file.txt").write_text("original")

        first = _create_sandbox(output_dir, "card1")
        (first / "result.txt").write_text("first run")
        second = _create_sandbox(output_dir, "card2")

        assert first != second
        assert (first / "result.txt").read_text() == "first run"
        assert not (second / "result.txt").exists()

        _cleanup_sandbox(second)

        assert not second.exists()
        assert (first / "file.txt").read_text() == "original"
        assert (first / "result.txt").read_text() == "first run"

    def test_cleanup_sandbox_removes_dir(self, tmp_path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("data")

        _cleanup_sandbox(sandbox)
        assert not sandbox.exists()

    def test_cleanup_sandbox_nonexistent_is_noop(self, tmp_path):
        sandbox = tmp_path / "nonexistent"
        _cleanup_sandbox(sandbox)  # Should not raise


# ---------------------------------------------------------------------------
# _is_pid_alive
# ---------------------------------------------------------------------------


class TestIsPidAlive:
    def test_current_process_is_alive(self):
        assert _is_pid_alive(os.getpid()) is True

    def test_very_large_pid_is_dead(self):
        assert _is_pid_alive(999_999_999) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
    def test_a_process_that_may_not_be_signalled_is_alive(self, monkeypatch):
        def refuse(pid, signal):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", refuse)
        assert _is_pid_alive(12345) is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
    def test_a_process_that_does_not_exist_is_dead(self, monkeypatch):
        def missing(pid, signal):
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr(os, "kill", missing)
        assert _is_pid_alive(12345) is False


# ---------------------------------------------------------------------------
# _update_agent_card
# ---------------------------------------------------------------------------


class TestUpdateAgentCard:
    """Test that _update_agent_card merges preview and broadcasts."""

    async def test_merges_preview_and_broadcasts(self, mock_server, study_mgr):
        # Create a study and card
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="upd1",
            card_type=CardType.AGENT,
            title="Test Agent",
            study="s1",
            preview={"status": "pending", "output": "", "model": "sonnet"},
        )
        store.store_card(card)
        study_mgr.register_card("upd1", study_mgr._label_to_dir["s1"])

        await _update_agent_card(
            "upd1",
            "s1",
            mock_server,
            {"status": "running", "output": "Starting..."},
        )

        # Verify the preview was merged in the store
        cards = store.list_cards()
        updated = next(c for c in cards if c.card_id == "upd1")
        assert updated.preview["status"] == "running"
        assert updated.preview["output"] == "Starting..."
        assert updated.preview["model"] == "sonnet"  # Preserved

        # Verify broadcast was called
        mock_server.broadcast.assert_called_once()
        msg = mock_server.broadcast.call_args[0][0]
        assert msg["type"] == "display.update"
        assert msg["card_id"] == "upd1"
        assert msg["card"]["preview"]["status"] == "running"

    async def test_title_override(self, mock_server, study_mgr):
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="upd2",
            card_type=CardType.AGENT,
            title="Old Title",
            study="s1",
            preview={"status": "pending"},
        )
        store.store_card(card)
        study_mgr.register_card("upd2", study_mgr._label_to_dir["s1"])

        await _update_agent_card(
            "upd2", "s1", mock_server, {"status": "running"}, title="New Title"
        )

        msg = mock_server.broadcast.call_args[0][0]
        assert msg["card"]["title"] == "New Title"


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------


class TestRunAgent:
    """Test run_agent() validation and error paths."""

    async def test_no_study_manager_raises(self, mock_server_no_mgr):
        with pytest.raises(ValueError, match="No study manager"):
            await run_agent("abc", mock_server_no_mgr)

    async def test_card_not_found_raises(self, mock_server):
        with pytest.raises(ValueError, match="No agent card found"):
            await run_agent("nonexistent", mock_server)

    async def test_not_pending_raises(self, mock_server):
        info = DispatchInfo(
            task="reproduce", study="s1", card_id="abc", status="completed"
        )
        mock_server.dispatches["abc"] = info
        with pytest.raises(RuntimeError, match="not pending"):
            await run_agent("abc", mock_server)

    async def test_concurrency_limit_raises(self, mock_server):
        # Fill up to max concurrent (5)
        for i in range(5):
            cid = f"run{i}"
            mock_server.dispatches[cid] = DispatchInfo(
                task="reproduce", study="s1", card_id=cid, status="running"
            )
        # Add the pending card
        mock_server.dispatches["pending1"] = DispatchInfo(
            task="reproduce", study="s1", card_id="pending1", status="pending"
        )
        with pytest.raises(RuntimeError, match="Maximum"):
            await run_agent("pending1", mock_server)

    async def test_no_claude_cli_raises(self, mock_server, study_mgr, monkeypatch):
        # Create agent card
        info = await create_agent_card("reproduce", "s1", mock_server)
        monkeypatch.setattr("vitrine.dispatch.shutil.which", lambda _b: None)
        with pytest.raises(ValueError, match="not found in PATH"):
            await run_agent(info.card_id, mock_server)

    async def test_happy_path_spawns_process(self, mock_server, study_mgr, monkeypatch):
        info = await create_agent_card("reproduce", "s1", mock_server)

        # Mock shutil.which and subprocess.Popen
        monkeypatch.setattr(
            "vitrine.dispatch.shutil.which", lambda _b: "/usr/local/bin/claude"
        )

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 42
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()

        mock_popen = MagicMock(return_value=mock_proc)
        monkeypatch.setattr("vitrine.dispatch.subprocess.Popen", mock_popen)

        # Mock asyncio.create_task to avoid actual monitoring
        import asyncio

        monkeypatch.setattr(
            asyncio,
            "create_task",
            lambda coro: MagicMock(spec=asyncio.Task),
        )

        result = await run_agent(info.card_id, mock_server)
        assert result.status == "running"
        assert result.pid == 42
        mock_popen.assert_called_once()
        mock_proc.stdin.write.assert_called_once()
        mock_proc.stdin.close.assert_called_once()

    async def test_paper_without_output_dir_runs_in_study_workspace(
        self, mock_server, study_mgr, monkeypatch
    ):
        """A study built from show() alone still gets a workspace the agent runs in."""
        import asyncio

        study_mgr.get_or_create_study("s1")
        assert study_mgr.get_output_dir("s1") is None
        info = await create_agent_card("paper", "s1", mock_server)

        monkeypatch.setattr(
            "vitrine.dispatch.shutil.which", lambda _b: "/usr/local/bin/claude"
        )
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 42
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_popen = MagicMock(return_value=mock_proc)
        monkeypatch.setattr("vitrine.dispatch.subprocess.Popen", mock_popen)

        def _no_monitor(coro):
            coro.close()
            return MagicMock(spec=asyncio.Task)

        monkeypatch.setattr(asyncio, "create_task", _no_monitor)

        await run_agent(info.card_id, mock_server)

        output_dir = study_mgr.get_output_dir("s1")
        workspace = output_dir / "paper" / info.card_id
        assert workspace.is_dir()
        assert mock_popen.call_args.kwargs["cwd"] == workspace
        assert info.extra["paper_workspace"] == str(workspace)
        prompt = mock_proc.stdin.write.call_args.args[0].decode()
        assert str(workspace) in prompt

    async def test_concurrent_reproduce_runs_get_their_own_sandboxes(
        self, mock_server, study_mgr, monkeypatch
    ):
        """Two reproduce runs on one study each get a sandbox; cancelling one
        leaves the other's sandbox in place. Both still run in the server's cwd."""
        import asyncio

        study_mgr.get_or_create_study("s1")
        output_dir = study_mgr.register_output_dir("s1")
        (output_dir / "scripts").mkdir()
        (output_dir / "scripts" / "01_cohort.py").write_text("print('hello')")

        monkeypatch.setattr(
            "vitrine.dispatch.shutil.which", lambda _b: "/usr/local/bin/claude"
        )
        mock_popen = MagicMock(side_effect=lambda *a, **kw: MagicMock(pid=42))
        monkeypatch.setattr("vitrine.dispatch.subprocess.Popen", mock_popen)

        def _no_monitor(coro):
            coro.close()
            return MagicMock(spec=asyncio.Task)

        monkeypatch.setattr(asyncio, "create_task", _no_monitor)

        first = await create_agent_card("reproduce", "s1", mock_server)
        second = await create_agent_card("reproduce", "s1", mock_server)
        await run_agent(first.card_id, mock_server)
        await run_agent(second.card_id, mock_server)

        first_sandbox = Path(first.extra["sandbox"])
        second_sandbox = Path(second.extra["sandbox"])
        assert first_sandbox != second_sandbox
        assert first_sandbox.name.endswith(first.card_id)
        assert second_sandbox.name.endswith(second.card_id)
        for call in mock_popen.call_args_list:
            assert call.kwargs["cwd"] is None

        await cancel_agent(first.card_id, mock_server)

        assert not first_sandbox.exists()
        assert (second_sandbox / "scripts" / "01_cohort.py").exists()
        assert (output_dir / "scripts" / "01_cohort.py").exists()

    @pytest.mark.parametrize("backend", ["gemini", "codex"])
    async def test_max_turns_rejected_for_backends_without_limit(
        self, mock_server, study_mgr, monkeypatch, backend
    ):
        info = await create_agent_card("report", "s1", mock_server)
        monkeypatch.setattr("vitrine.dispatch.shutil.which", lambda b: f"/bin/{b}")
        mock_popen = MagicMock()
        monkeypatch.setattr("vitrine.dispatch.subprocess.Popen", mock_popen)

        with pytest.raises(ValueError, match="cannot limit the number of turns"):
            await run_agent(
                info.card_id, mock_server, {"backend": backend, "budget": 5}
            )
        mock_popen.assert_not_called()

    async def test_cli_exiting_before_reading_prompt_is_still_monitored(
        self, mock_server, study_mgr, monkeypatch
    ):
        """A closed stdin pipe doesn't fail the request; the monitor reports it."""
        import asyncio

        info = await create_agent_card("report", "s1", mock_server)
        monkeypatch.setattr("vitrine.dispatch.shutil.which", lambda b: f"/bin/{b}")
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 42
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError
        mock_proc.stdin.close.side_effect = [BrokenPipeError, None]
        mock_proc.stdout = MagicMock()
        monkeypatch.setattr(
            "vitrine.dispatch.subprocess.Popen", MagicMock(return_value=mock_proc)
        )
        monitored = []

        def _record_monitor(coro):
            monitored.append(coro)
            coro.close()
            return MagicMock(spec=asyncio.Task)

        monkeypatch.setattr(asyncio, "create_task", _record_monitor)

        result = await run_agent(info.card_id, mock_server, {"backend": "gemini"})

        assert result.status == "running"
        assert len(monitored) == 1


# ---------------------------------------------------------------------------
# _stream_monitor
# ---------------------------------------------------------------------------


class TestStreamMonitor:
    """Test _stream_monitor output parsing and card finalization."""

    async def test_fatal_result_fails_run_that_exits_zero(self, mock_server, study_mgr):
        """A Gemini error result fails the card even when the CLI exits 0."""
        _, store = study_mgr.get_or_create_study("s1")
        store.store_card(
            CardDescriptor(
                card_id="mon-fatal",
                card_type=CardType.AGENT,
                title="Monitor Test",
                study="s1",
                preview={"status": "running", "output": ""},
            )
        )
        study_mgr.register_card("mon-fatal", study_mgr._label_to_dir["s1"])
        events = [
            {
                "type": "message",
                "role": "assistant",
                "content": "Partial",
                "delta": True,
            },
            {
                "type": "result",
                "status": "error",
                "error": {"type": "InvalidStream", "message": "Empty response"},
                "stats": {"input_tokens": 7, "output_tokens": 2},
            },
        ]
        line_iter = iter([(json.dumps(e) + "\n").encode() for e in events] + [b""])
        mock_proc = MagicMock()
        mock_proc.stdout.readline = lambda: next(line_iter)
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        info = DispatchInfo(
            task="report",
            study="s1",
            card_id="mon-fatal",
            status="running",
            backend="gemini",
            model="gemini-2.5-pro",
            started_at="2025-01-01T00:00:00+00:00",
        )
        info.process = mock_proc

        await _stream_monitor(info, mock_server)

        assert info.status == "failed"
        assert info.error == "Empty response"
        card = store.list_cards()[0]
        assert card.preview["status"] == "failed"
        assert card.preview["output"].startswith("Partial")
        assert card.preview["usage"]["input_tokens"] == 7
        broadcast_types = [
            c[0][0]["type"] for c in mock_server.broadcast.call_args_list
        ]
        assert "agent.failed" in broadcast_types
        assert "agent.completed" not in broadcast_types

    async def test_successful_completion(self, mock_server, study_mgr):
        """Mocked stdout producing text + result → status=completed."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="mon1",
            card_type=CardType.AGENT,
            title="Monitor Test",
            study="s1",
            preview={"status": "running", "output": ""},
        )
        store.store_card(card)
        study_mgr.register_card("mon1", study_mgr._label_to_dir["s1"])

        # Build canned stream lines
        text_line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Analysis done."}]},
            }
        )
        result_line = json.dumps(
            {"type": "result", "result": "Final output.", "total_cost_usd": 0.01}
        )

        lines = [
            (text_line + "\n").encode(),
            (result_line + "\n").encode(),
            b"",  # EOF
        ]
        line_iter = iter(lines)

        mock_proc = MagicMock()
        mock_proc.stdout.readline = lambda: next(line_iter)
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0

        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="mon1",
            status="running",
            model="sonnet",
            started_at="2025-01-01T00:00:00+00:00",
        )
        info.process = mock_proc

        await _stream_monitor(info, mock_server)

        assert info.status == "completed"
        assert info.completed_at is not None
        # Should have broadcast agent.completed
        broadcast_types = [
            call[0][0]["type"] for call in mock_server.broadcast.call_args_list
        ]
        assert "agent.completed" in broadcast_types

    async def test_failed_process(self, mock_server, study_mgr):
        """Process exits with code 1 → status=failed."""
        _, store = study_mgr.get_or_create_study("s1")
        card = CardDescriptor(
            card_id="mon2",
            card_type=CardType.AGENT,
            title="Fail Test",
            study="s1",
            preview={"status": "running", "output": ""},
        )
        store.store_card(card)
        study_mgr.register_card("mon2", study_mgr._label_to_dir["s1"])

        lines = [b""]  # Immediate EOF
        line_iter = iter(lines)

        mock_proc = MagicMock()
        mock_proc.stdout.readline = lambda: next(line_iter)
        mock_proc.wait.return_value = 1
        mock_proc.poll.return_value = 1

        info = DispatchInfo(
            task="reproduce",
            study="s1",
            card_id="mon2",
            status="running",
            model="sonnet",
            started_at="2025-01-01T00:00:00+00:00",
        )
        info.process = mock_proc

        await _stream_monitor(info, mock_server)

        assert info.status == "failed"
        assert "exit" in info.error.lower()
        broadcast_types = [
            call[0][0]["type"] for call in mock_server.broadcast.call_args_list
        ]
        assert "agent.failed" in broadcast_types

    @staticmethod
    async def _monitor(mock_server, study_mgr, card_id, backend, lines, returncode):
        """Run the monitor over a fake process that prints ``lines`` and exits."""
        _, store = study_mgr.get_or_create_study("s1")
        store.store_card(
            CardDescriptor(
                card_id=card_id,
                card_type=CardType.AGENT,
                title="Monitor Test",
                study="s1",
                preview={"status": "running", "output": ""},
            )
        )
        study_mgr.register_card(card_id, study_mgr._label_to_dir["s1"])
        line_iter = iter([(line + "\n").encode() for line in lines] + [b""])
        mock_proc = MagicMock()
        mock_proc.stdout.readline = lambda: next(line_iter)
        mock_proc.wait.return_value = returncode
        mock_proc.poll.return_value = returncode
        info = DispatchInfo(
            task="report",
            study="s1",
            card_id=card_id,
            status="running",
            backend=backend,
            started_at="2025-01-01T00:00:00+00:00",
        )
        info.process = mock_proc
        await _stream_monitor(info, mock_server)
        card = next(c for c in store.list_cards() if c.card_id == card_id)
        return info, card

    async def test_failed_run_shows_cli_error_output(self, mock_server, study_mgr):
        """A CLI that rejects its arguments and exits 2 shows why on the card."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            "error: unexpected argument '--frobnicate' found",
            "",
            "Usage: codex exec [OPTIONS] [PROMPT]",
        ]
        info, card = await self._monitor(
            mock_server, study_mgr, "mon-diag", "codex", lines, returncode=2
        )

        assert info.status == "failed"
        assert info.error == "Process exited with code 2"
        output = card.preview["output"]
        assert "**Error:** Process exited with code 2" in output
        assert (
            "**Last output from Codex CLI:**\n\n```\n"
            "error: unexpected argument '--frobnicate' found\n"
            "Usage: codex exec [OPTIONS] [PROMPT]\n```"
        ) in output
        assert "thread.started" not in output
        assert card.preview["error"] == "Process exited with code 2"

    async def test_successful_run_hides_cli_output(self, mock_server, study_mgr):
        lines = [
            "Loaded cached credentials.",
            json.dumps({"type": "message", "role": "assistant", "content": "Done."}),
            json.dumps({"type": "result", "status": "success", "stats": {}}),
        ]
        info, card = await self._monitor(
            mock_server, study_mgr, "mon-ok", "gemini", lines, returncode=0
        )

        assert info.status == "completed"
        assert card.preview["output"] == "Done."

    async def test_cli_output_tail_is_bounded(self, mock_server, study_mgr):
        lines = [f"\x1b[31mtrace line {i}\x1b[0m" for i in range(100)]
        _, card = await self._monitor(
            mock_server, study_mgr, "mon-tail", "claude", lines, returncode=1
        )

        output = card.preview["output"]
        assert "trace line 59\n" not in output
        assert "trace line 60\n" in output
        assert "trace line 99\n```" in output
        assert "\x1b" not in output

    async def test_malformed_events_do_not_stop_monitoring(
        self, mock_server, study_mgr
    ):
        """An event the parser chokes on is skipped; the run still completes."""
        lines = [
            json.dumps({"type": "item.completed", "item": "not an object"}),
            "null",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done."},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 3}}),
        ]
        info, card = await self._monitor(
            mock_server, study_mgr, "mon-bad", "codex", lines, returncode=0
        )

        assert info.status == "completed"
        assert card.preview["output"].strip() == "Done."
        assert card.preview["usage"]["output_tokens"] == 3

    async def test_non_object_json_counts_as_cli_output(self, mock_server, study_mgr):
        lines = ["null", "Error: not logged in"]
        _, card = await self._monitor(
            mock_server, study_mgr, "mon-null", "gemini", lines, returncode=1
        )

        assert "```\nnull\nError: not logged in\n```" in card.preview["output"]

    def test_cli_output_fence_survives_backticks_in_output(self):
        output = _with_diagnostics("Body", ["```", "``code``"], "Claude Code")
        assert output.endswith("\n````\n```\n``code``\n````")

    def test_no_cli_output_leaves_output_unchanged(self):
        assert _with_diagnostics("Body", [], "Claude Code") == "Body"


# ---------------------------------------------------------------------------
# Paper task — build_prompt, preview, card creation
# ---------------------------------------------------------------------------


class TestPaperTask:
    def test_valid_task_paper(self, study_mgr):
        _, _store = study_mgr.get_or_create_study("paper-study")
        prompt = build_prompt("paper", "paper-study", study_mgr)
        assert "paper-study" in prompt
        assert "Output directory:" in prompt

    def test_paper_task_tools(self):
        preview = _build_agent_preview("paper", "pending")
        assert "Bash" in preview["tools"]
        assert "Read" in preview["tools"]
        assert "Glob" in preview["tools"]
        assert "Grep" in preview["tools"]
        assert "Write" in preview["tools"]

    async def test_paper_task_card_title(self, mock_server):
        await create_agent_card("paper", "my-study", mock_server)
        call_args = mock_server.broadcast.call_args[0][0]
        assert call_args["card"]["title"] == "Paper Draft"


# ---------------------------------------------------------------------------
# Paper workspace helpers
# ---------------------------------------------------------------------------


class TestPaperWorkspace:
    def test_create_paper_workspace(self, tmp_path):
        output_dir = tmp_path / "study-output"
        output_dir.mkdir()
        (output_dir / "scripts").mkdir()
        (output_dir / "scripts" / "01_cohort.py").write_text("print('hello')")
        (output_dir / "data").mkdir()
        (output_dir / "data" / "cohort.parquet").write_bytes(b"fake-parquet")
        (output_dir / "plots").mkdir()
        (output_dir / "plots" / "fig1.png").write_bytes(b"fake-png")
        (output_dir / "PROTOCOL.md").write_text("# Protocol")
        (output_dir / "RESULTS.md").write_text("# Results")

        paper_dir, copied = _create_paper_workspace(output_dir, "paper", "card1")

        assert paper_dir == output_dir / "paper" / "card1"
        assert paper_dir.exists()
        assert set(copied) == {"scripts", "data", "plots", "PROTOCOL.md", "RESULTS.md"}
        assert (paper_dir / "scripts" / "01_cohort.py").read_text() == "print('hello')"
        assert (paper_dir / "data" / "cohort.parquet").exists()
        assert (paper_dir / "plots" / "fig1.png").exists()
        assert (paper_dir / "PROTOCOL.md").read_text() == "# Protocol"
        assert (paper_dir / "RESULTS.md").read_text() == "# Results"

    def test_cleanup_paper_workspace(self, tmp_path):
        """Copied items are removed; agent-generated files are preserved."""
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()

        # Simulate copied study files
        (paper_dir / "scripts").mkdir()
        (paper_dir / "scripts" / "01.py").write_text("code")
        (paper_dir / "PROTOCOL.md").write_text("protocol")

        # Simulate agent-generated files
        (paper_dir / "paper.md").write_text("# My Paper")
        (paper_dir / "abstract.md").write_text("Abstract text")
        (paper_dir / "references.bib").write_text("@article{}")
        (paper_dir / "figures").mkdir()
        (paper_dir / "figures" / "fig1.png").write_bytes(b"png")

        _cleanup_paper_workspace(paper_dir, ["scripts", "PROTOCOL.md"])

        # Copied items should be gone
        assert not (paper_dir / "scripts").exists()
        assert not (paper_dir / "PROTOCOL.md").exists()

        # Agent-generated files should remain
        assert (paper_dir / "paper.md").read_text() == "# My Paper"
        assert (paper_dir / "abstract.md").exists()
        assert (paper_dir / "references.bib").exists()
        assert (paper_dir / "figures" / "fig1.png").exists()

    def test_create_workspace_skips_missing(self, tmp_path):
        """Gracefully handles missing scripts/plots/etc."""
        output_dir = tmp_path / "sparse-study"
        output_dir.mkdir()
        # Only create PROTOCOL.md — scripts, data, plots, RESULTS.md, REPORT.md missing
        (output_dir / "PROTOCOL.md").write_text("# Protocol")

        paper_dir, copied = _create_paper_workspace(output_dir, "paper", "card1")

        assert paper_dir.exists()
        assert copied == ["PROTOCOL.md"]
        assert (paper_dir / "PROTOCOL.md").exists()
        assert not (paper_dir / "scripts").exists()
        assert not (paper_dir / "plots").exists()

    def test_create_workspace_refreshes_leftover_copies(self, tmp_path):
        """Copies a crashed run left behind are replaced and cleaned up later."""
        output_dir = tmp_path / "study-output"
        output_dir.mkdir()
        (output_dir / "PROTOCOL.md").write_text("new version")

        paper_dir = output_dir / "paper" / "card1"
        paper_dir.mkdir(parents=True)
        (paper_dir / "PROTOCOL.md").write_text("old version")

        _, copied = _create_paper_workspace(output_dir, "paper", "card1")

        assert copied == ["PROTOCOL.md"]
        assert (paper_dir / "PROTOCOL.md").read_text() == "new version"

    def test_concurrent_runs_get_separate_workspaces(self, tmp_path):
        """One run's cleanup leaves another run's inputs and outputs alone."""
        output_dir = tmp_path / "study-output"
        (output_dir / "plots").mkdir(parents=True)
        (output_dir / "plots" / "fig1.png").write_bytes(b"png")

        first, first_copied = _create_paper_workspace(
            output_dir, "presentation", "card1"
        )
        second, second_copied = _create_paper_workspace(
            output_dir, "presentation", "card2"
        )
        assert first != second
        assert first_copied == second_copied == ["plots"]
        (first / "presentation.pptx").write_bytes(b"first")
        (second / "presentation.pptx").write_bytes(b"second")

        _cleanup_paper_workspace(first, first_copied)

        assert (second / "plots" / "fig1.png").exists()
        assert (first / "presentation.pptx").read_bytes() == b"first"
        assert (second / "presentation.pptx").read_bytes() == b"second"

    def test_cleanup_missing_items_is_noop(self, tmp_path):
        """Cleanup doesn't fail if items are already gone."""
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        # Items don't exist — should not raise
        _cleanup_paper_workspace(paper_dir, ["scripts", "PROTOCOL.md", "nonexistent"])

"""Dispatch: spawn headless Claude Code agents for study operations.

The agent lives inside a dedicated AGENT card. The researcher sees a config
form first, can tune parameters (model, budget, instructions), then
explicitly runs the agent. Output streams inline (collapsible), and
completed cards auto-collapse to a compact summary row.

Flow:
    1. ``create_agent_card()`` creates an AGENT card with config preview
    2. Researcher reviews/tweaks config in the browser
    3. ``run_agent()`` spawns ``claude -p`` and streams output into the card
    4. On completion/failure, the card is finalized with status + duration

Up to ``_MAX_CONCURRENT`` agents can run simultaneously.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vitrine._types import DispatchHost
    from vitrine.study_manager import StudyManager

logger = logging.getLogger(__name__)

_SKILLS_DIR: Path | None = None
_DISPATCH_TIMEOUT = 1800  # 30 minutes
_UPDATE_INTERVAL = 0.5  # seconds between card updates (debounce)
_SANDBOX_SUFFIX = "_reproduce"  # suffix for sandboxed output directory copies
_MAX_CONCURRENT = 5  # global running agent limit
@dataclass
class BackendConfig:
    """Configuration for a CLI agent backend (Claude Code, Gemini CLI, Codex CLI)."""

    name: str  # "claude", "gemini", "codex"
    display_name: str  # "Claude Code", "Gemini CLI", "Codex CLI"
    binary: str  # "claude", "gemini", "codex"
    default_model: str
    models: dict[str, str]  # {value: display_name}
    context_windows: dict[str, int]
    env_strip: list[str]  # env vars to strip from child process
    # Returns (args, stdin_payload, temp_files_to_cleanup)
    build_args: Callable[..., tuple[list[str], bytes | None, list[str]]] = None  # type: ignore[assignment]
    parse_event: Callable[[str], tuple[str, str, dict[str, Any] | None]] = None  # type: ignore[assignment]


_PROMPT_FILE_THRESHOLD = 100_000  # bytes — use temp file above this


def _prompt_as_arg_or_file(prompt: str) -> tuple[str, str | None]:
    """Return (arg_value, tmp_path|None).

    If the prompt is short enough for a positional CLI arg, returns
    (prompt, None).  Otherwise writes it to a temp file and returns
    (@file_path, file_path) so the caller can clean up the file later.
    """
    encoded = prompt.encode()
    if len(encoded) <= _PROMPT_FILE_THRESHOLD:
        return prompt, None
    f = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".md", prefix="vitrine-prompt-", delete=False,
    )
    f.write(encoded)
    f.close()
    return f"@{f.name}", f.name


def _build_claude_args(
    binary: str,
    prompt: str,
    model: str,
    allowed_tools: str,
    budget: float | None,
) -> tuple[list[str], bytes | None, list[str]]:
    """Build CLI args for Claude Code. Prompt is piped via stdin."""
    args = [
        binary,
        "-p", "-",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--allowedTools", allowed_tools,
    ]
    if model and model != "sonnet":
        args.extend(["--model", model])
    if budget is not None:
        args.extend(["--max-turns", str(int(budget))])
    return args, prompt.encode(), []


def _build_gemini_args(
    binary: str,
    prompt: str,
    model: str,
    allowed_tools: str,
    budget: float | None,
) -> tuple[list[str], bytes | None, list[str]]:
    """Build CLI args for Gemini CLI. Prompt is a positional arg."""
    args = [
        binary,
        "--approval-mode", "yolo",
        "--output-format", "stream-json",
    ]
    if model:
        args.extend(["--model", model])
    prompt_arg, tmp_path = _prompt_as_arg_or_file(prompt)
    args.append(prompt_arg)
    return args, None, [tmp_path] if tmp_path else []


def _build_codex_args(
    binary: str,
    prompt: str,
    model: str,
    allowed_tools: str,
    budget: float | None,
) -> tuple[list[str], bytes | None, list[str]]:
    """Build CLI args for Codex CLI. Prompt is a positional arg to exec."""
    args = [
        binary,
        "exec",
        "--full-auto",
        "--sandbox", "danger-full-access",
        "--json",
    ]
    if model:
        args.extend(["--model", model])
    prompt_arg, tmp_path = _prompt_as_arg_or_file(prompt)
    args.append(prompt_arg)
    return args, None, [tmp_path] if tmp_path else []


def _parse_claude_event(line: str) -> tuple[str, str, dict[str, Any] | None]:
    """Parse a Claude Code stream-json line."""
    return _parse_stream_event(line)


def _parse_gemini_event(line: str) -> tuple[str, str, dict[str, Any] | None]:
    """Parse a Gemini CLI stream-json line.

    Based on documented Gemini CLI output format. May need tuning
    once tested against actual CLI output.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ("ignore", "", None)

    evt_type = obj.get("type", "")

    if evt_type == "text":
        return ("text", obj.get("content", ""), None)

    if evt_type == "tool_call":
        name = obj.get("name", "?")
        return ("tool_use", f"\n\n> *Using {name}...*\n\n", None)

    if evt_type == "result":
        usage_data = None
        usage = obj.get("usage", {})
        if usage:
            usage_data = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": usage.get("cost_usd"),
            }
        return ("result", obj.get("text", obj.get("result", "")), usage_data)

    if evt_type == "error":
        return ("error", obj.get("message", "Unknown error"), None)

    return ("ignore", "", None)


def _parse_codex_event(line: str) -> tuple[str, str, dict[str, Any] | None]:
    """Parse a Codex CLI JSON output line.

    Based on documented Codex CLI output format. May need tuning
    once tested against actual CLI output.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ("ignore", "", None)

    evt_type = obj.get("type", "")

    if evt_type == "message":
        content = obj.get("content", "")
        if isinstance(content, list):
            parts = [
                b.get("text", "") for b in content if b.get("type") == "text"
            ]
            return ("text", "".join(parts), None)
        return ("text", str(content), None)

    if evt_type == "function_call":
        name = obj.get("name", "?")
        return ("tool_use", f"\n\n> *Using {name}...*\n\n", None)

    if evt_type in ("result", "completed"):
        usage_data = None
        usage = obj.get("usage", {})
        if usage:
            usage_data = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": usage.get("cost_usd"),
            }
        return ("result", obj.get("output", obj.get("result", "")), usage_data)

    if evt_type == "error":
        return ("error", obj.get("message", "Unknown error"), None)

    return ("ignore", "", None)


_BACKENDS: dict[str, BackendConfig] = {
    "claude": BackendConfig(
        name="claude",
        display_name="Claude Code",
        binary="claude",
        default_model="sonnet",
        models={
            "sonnet": "Sonnet 4.5",
            "opus": "Opus 4.6",
            "haiku": "Haiku 4.5",
        },
        context_windows={
            "sonnet": 200_000,
            "opus": 200_000,
            "haiku": 200_000,
        },
        env_strip=["CLAUDECODE"],
        build_args=_build_claude_args,
        parse_event=_parse_claude_event,
    ),
    "gemini": BackendConfig(
        name="gemini",
        display_name="Gemini CLI",
        binary="gemini",
        default_model="gemini-2.5-pro",
        models={
            "gemini-2.5-pro": "2.5 Pro",
            "gemini-2.5-flash": "2.5 Flash",
        },
        context_windows={
            "gemini-2.5-pro": 1_000_000,
            "gemini-2.5-flash": 1_000_000,
        },
        env_strip=[],
        build_args=_build_gemini_args,
        parse_event=_parse_gemini_event,
    ),
    "codex": BackendConfig(
        name="codex",
        display_name="Codex CLI",
        binary="codex",
        default_model="gpt-5-codex",
        models={
            "gpt-5-codex": "GPT-5 Codex",
        },
        context_windows={
            "gpt-5-codex": 200_000,
        },
        env_strip=[],
        build_args=_build_codex_args,
        parse_event=_parse_codex_event,
    ),
}

# task name -> (skill directory, card title, allowed tools)
_TASK_CONFIG: dict[str, tuple[str, str, str]] = {}

_BUILTIN_SKILLS_DIR: Path = Path(__file__).parent / "skills"

_DEFAULT_TASK_CONFIG: dict[str, tuple[str, str, str]] = {
    "reproduce": ("reproduce-study", "Reproducibility Audit", "Bash,Read,Glob,Grep"),
    "report": ("export-report", "Study Report", "Read,Glob,Grep"),
    "paper": ("draft-paper", "Paper Draft", "Bash,Read,Glob,Grep,Write"),
}


def configure(
    skills_dir: Path | str | None = None,
    task_config: dict | None = None,
) -> None:
    """Configure the dispatch module with skills directory and task config.

    Must be called before using dispatch features (build_prompt,
    create_agent_card, run_agent).

    Args:
        skills_dir: Path to the directory containing skill subdirectories.
        task_config: Dict mapping task name to (skill_dir_name, card_title,
            allowed_tools) tuples.
    """
    global _SKILLS_DIR, _TASK_CONFIG
    if skills_dir is not None:
        _SKILLS_DIR = Path(skills_dir)
    if task_config is not None:
        _TASK_CONFIG = dict(task_config)


def _require_config() -> None:
    """Ensure dispatch is configured, loading built-in defaults if needed."""
    global _SKILLS_DIR, _TASK_CONFIG
    if _SKILLS_DIR is None:
        _SKILLS_DIR = _BUILTIN_SKILLS_DIR
    if not _TASK_CONFIG:
        _TASK_CONFIG = dict(_DEFAULT_TASK_CONFIG)


@dataclass
class DispatchInfo:
    """Metadata for an agent dispatch (pending, running, or completed)."""

    task: str
    study: str
    card_id: str = ""
    # Config (set at creation, user can override before run)
    backend: str = "claude"
    model: str = "sonnet"
    budget: float | None = None
    additional_prompt: str = ""
    # Runtime
    process: subprocess.Popen | None = None
    monitor_task: asyncio.Task | None = None
    pid: int | None = None
    status: str = "pending"  # pending -> running -> completed/failed/cancelled
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    accumulated_output: str = ""  # live output buffer for cancel recovery
    last_activity_at: str | None = None  # ISO — last output line received
    extra: dict[str, Any] = field(default_factory=dict)


def build_prompt(
    task: str,
    study: str,
    study_manager: StudyManager,
    work_dir: Path | None = None,
    additional_prompt: str = "",
) -> str:
    """Build a prompt for the dispatched agent.

    Includes skill instructions and study context. Points the agent at the
    study's output directory so it can use Read/Glob/Grep to explore files.

    Args:
        task: Dispatch task name (e.g. "reproduce", "report").
        study: Study label.
        study_manager: Active study manager.
        work_dir: If given, the agent is pointed at this directory instead of
            the original output directory. Used for sandboxed reproduce runs.
        additional_prompt: Extra instructions from the researcher.
    """
    _require_config()

    config = _TASK_CONFIG.get(task)
    if config is None:
        raise ValueError(
            f"Unknown dispatch task: {task!r} (expected one of {list(_TASK_CONFIG)})"
        )

    skill_dir_name, _, _ = config
    skill_path = _SKILLS_DIR / skill_dir_name / "SKILL.md"
    if not skill_path.exists():
        raise ValueError(f"Skill file not found: {skill_path}")

    skill_content = skill_path.read_text()

    # Study context (card summaries, decisions, annotations)
    study_manager.refresh()
    ctx = study_manager.build_context(study)
    ctx_json = json.dumps(ctx, indent=2, default=str)

    # Output directory path for the agent to explore
    if work_dir is not None:
        output_dir_str = str(work_dir)
    else:
        output_dir = study_manager.get_output_dir(study)
        output_dir_str = (
            str(output_dir) if output_dir and output_dir.exists() else "(none)"
        )

    sandbox_note = ""
    if work_dir is not None:
        sandbox_note = (
            "\n> **Sandbox:** This is a copy of the original study output. "
            "You may freely run scripts and modify files here — the original "
            "study data is untouched.\n"
        )

    additional_section = ""
    if additional_prompt.strip():
        additional_section = (
            f"\n### Additional Instructions\n\n{additional_prompt.strip()}\n"
        )

    return f"""{skill_content}

---

## Dispatch Context

**Study:** {study}
**Output directory:** `{output_dir_str}`
{sandbox_note}
Use Glob, Read, and Grep to explore the output directory. Key locations:
- `scripts/` — analysis scripts (numbered .py files)
- `data/` — saved DataFrames (.parquet)
- `plots/` — figures (.png, .html)
- `PROTOCOL.md` — research protocol
- `STUDY.md` — study description
- `RESULTS.md` — findings (if completed)
{additional_section}
### Study Context (cards, decisions, annotations)

```json
{ctx_json}
```

---

## Output Instructions

Your output is streamed directly into a single vitrine card as markdown.
Write your analysis as markdown to stdout — that IS the card content.
Do NOT use any vitrine API calls or `show()`.
Structure your output with clear headings. Start writing immediately so the
user sees progress.
"""


def _build_agent_preview(
    task: str,
    status: str,
    backend: str = "claude",
    model: str = "sonnet",
    additional_prompt: str = "",
    budget: float | None = None,
) -> dict[str, Any]:
    """Build the preview dict for an agent card."""
    _require_config()

    config = _TASK_CONFIG.get(task, ("", "", ""))
    _, _, allowed_tools = config

    # Build full prompt preview
    skill_dir_name = _TASK_CONFIG.get(task, ("", "", ""))[0]
    skill_path = _SKILLS_DIR / skill_dir_name / "SKILL.md"
    full_prompt = ""
    if skill_path.exists():
        full_prompt = skill_path.read_text()

    backend_cfg = _BACKENDS.get(backend, _BACKENDS["claude"])

    return {
        "task": task,
        "status": status,
        "backend": backend,
        "model": model,
        "tools": allowed_tools.split(",") if allowed_tools else [],
        "prompt_preview": full_prompt[:200] + ("..." if len(full_prompt) > 200 else ""),
        "full_prompt": full_prompt,
        "additional_prompt": additional_prompt,
        "budget": budget,
        "output": "",
        "started_at": None,
        "completed_at": None,
        "duration": None,
        "error": None,
        "last_activity_at": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "context_window": backend_cfg.context_windows.get(model, 200_000),
            "cost_usd": None,
        },
    }


async def create_agent_card(
    task: str,
    study: str,
    server: DispatchHost,
) -> DispatchInfo:
    """Create an AGENT card with config preview. Does not start the agent.

    The card appears in the browser with a config form. The researcher
    can adjust model, budget, and instructions before clicking "Run Agent".
    """
    from vitrine._types import CardDescriptor, CardType
    from vitrine.artifacts import _serialize_card

    _require_config()

    if not server.study_manager:
        raise ValueError("No study manager available")

    config = _TASK_CONFIG.get(task)
    if config is None:
        raise ValueError(f"Unknown task: {task!r}")
    _, card_title, _ = config

    card_id = uuid.uuid4().hex[:12]
    preview = _build_agent_preview(task, "pending")

    card = CardDescriptor(
        card_id=card_id,
        card_type=CardType.AGENT,
        title=card_title,
        study=study,
        timestamp=datetime.now(timezone.utc).isoformat(),
        preview=preview,
    )

    # Persist in the study's artifact store
    _, store = server.study_manager.get_or_create_study(study)
    if store:
        store.store_card(card)
        server.study_manager.register_card(
            card_id, server.study_manager.get_dir_for_label(study) or ""
        )

    # Broadcast to browser
    await server.broadcast({"type": "display.add", "card": _serialize_card(card)})

    info = DispatchInfo(
        task=task,
        study=study,
        card_id=card_id,
        status="pending",
    )
    server.dispatches[card_id] = info

    logger.info(f"Created agent card '{task}' for '{study}' (card={card_id})")
    return info


async def _update_agent_card(
    card_id: str,
    study: str,
    server: DispatchHost,
    preview_updates: dict[str, Any],
    title: str | None = None,
) -> None:
    """Update an agent card's preview and broadcast the change."""
    from vitrine._types import CardType

    # Merge updates into existing preview in store, then broadcast full card
    full_preview = dict(preview_updates)
    stored_title = title
    if server.study_manager:
        _, store = server.study_manager.get_or_create_study(study)
        if store:
            cards = store.list_cards()
            for c in cards:
                if c.card_id == card_id:
                    new_preview = dict(c.preview)
                    new_preview.update(preview_updates)
                    store.update_card(card_id, preview=new_preview)
                    full_preview = new_preview
                    if stored_title is None:
                        stored_title = c.title
                    break

    # Build broadcast payload with full card data (not partial)
    card_data: dict[str, Any] = {
        "card_id": card_id,
        "card_type": CardType.AGENT.value,
        "study": study,
        "preview": full_preview,
    }
    if stored_title is not None:
        card_data["title"] = stored_title

    await server.broadcast(
        {"type": "display.update", "card_id": card_id, "card": card_data}
    )


async def run_agent(
    card_id: str,
    server: DispatchHost,
    config: dict[str, Any] | None = None,
) -> DispatchInfo:
    """Start the agent for an existing AGENT card.

    Validates the global concurrency limit, applies config overrides,
    spawns the process, and starts the output monitor.

    Args:
        card_id: ID of the AGENT card to run.
        server: The DispatchHost instance.
        config: Optional overrides (model, budget, additional_prompt).
    """
    _require_config()

    if not server.study_manager:
        raise ValueError("No study manager available")

    info = server.dispatches.get(card_id)
    if info is None:
        raise ValueError(f"No agent card found: {card_id}")
    if info.status != "pending":
        raise RuntimeError(
            f"Agent card {card_id} is not pending (status={info.status})"
        )

    # Check global concurrency limit
    running = sum(1 for d in server.dispatches.values() if d.status == "running")
    if running >= _MAX_CONCURRENT:
        raise RuntimeError(f"Maximum {_MAX_CONCURRENT} concurrent agents reached")

    # Apply config overrides
    if config:
        if "backend" in config:
            info.backend = config["backend"]
        if "model" in config:
            info.model = config["model"]
        if "budget" in config:
            info.budget = config["budget"]
        if "additional_prompt" in config:
            info.additional_prompt = config["additional_prompt"]

    backend_cfg = _BACKENDS.get(info.backend)
    if backend_cfg is None:
        raise ValueError(
            f"Unknown backend: {info.backend!r} (expected one of {list(_BACKENDS)})"
        )

    binary_path = shutil.which(backend_cfg.binary)
    if binary_path is None:
        raise ValueError(
            f"{backend_cfg.display_name} CLI ({backend_cfg.binary!r}) not found in PATH."
        )

    task_config = _TASK_CONFIG.get(info.task)
    if task_config is None:
        raise ValueError(f"Unknown task: {info.task!r}")
    _, card_title, allowed_tools = task_config

    # For reproduce tasks, sandbox the output directory
    work_dir: Path | None = None
    if info.task == "reproduce":
        output_dir = server.study_manager.get_output_dir(info.study)
        if output_dir and output_dir.exists():
            work_dir = _create_sandbox(output_dir)
            info.extra["sandbox"] = str(work_dir)
    elif info.task == "paper":
        output_dir = server.study_manager.get_output_dir(info.study)
        if output_dir and output_dir.exists():
            paper_dir, copied = _create_paper_workspace(output_dir)
            work_dir = paper_dir
            info.extra["paper_workspace"] = str(paper_dir)
            info.extra["paper_copies"] = copied

    prompt = build_prompt(
        info.task,
        info.study,
        server.study_manager,
        work_dir=work_dir,
        additional_prompt=info.additional_prompt,
    )

    # Build CLI args using the backend's arg builder
    cli_args, stdin_payload, tmp_files = backend_cfg.build_args(
        binary_path, prompt, info.model, allowed_tools, info.budget
    )
    if tmp_files:
        info.extra["prompt_tmp_files"] = tmp_files

    # Strip backend-specific env vars from child process
    env = {
        k: v for k, v in os.environ.items()
        if k not in backend_cfg.env_strip
    }

    from vitrine._utils import detached_popen_kwargs

    # Spawn headless agent
    proc = subprocess.Popen(
        cli_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **detached_popen_kwargs(),
        env=env,
    )

    # Feed prompt via stdin if the builder provided a payload, then close
    if stdin_payload and proc.stdin:
        proc.stdin.write(stdin_payload)
    if proc.stdin:
        proc.stdin.close()

    info.process = proc
    info.pid = proc.pid
    info.status = "running"
    info.started_at = datetime.now(timezone.utc).isoformat()

    # Update card preview to running
    await _update_agent_card(
        card_id,
        info.study,
        server,
        {
            "status": "running",
            "backend": info.backend,
            "model": info.model,
            "additional_prompt": info.additional_prompt,
            "budget": info.budget,
            "started_at": info.started_at,
            "last_activity_at": info.started_at,
            "output": "*Agent starting...*",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "context_window": backend_cfg.context_windows.get(info.model, 200_000),
                "cost_usd": None,
            },
        },
        title=card_title,
    )

    # Broadcast started event (for toast)
    await server.broadcast(
        {
            "type": "agent.started",
            "study": info.study,
            "task": info.task,
            "card_id": card_id,
            "pid": proc.pid,
        }
    )

    # Start the streaming monitor
    info.monitor_task = asyncio.create_task(_stream_monitor(info, server))

    logger.info(
        f"Dispatched '{info.task}' for '{info.study}' (pid={proc.pid}, card={card_id})"
    )
    return info


def _parse_stream_event(line: str) -> tuple[str, str, dict[str, Any] | None]:
    """Parse a stream-json line and return (event_kind, display_text, usage).

    event_kind is one of: "text", "tool_use", "tool_result", "error",
    "result", "ignore".
    display_text is the content to show for that event (may be empty).
    usage is a dict with token/cost info if available, else None.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return ("ignore", "", None)

    evt_type = obj.get("type", "")

    if evt_type == "assistant":
        # Agent message — may contain text blocks and/or tool_use blocks
        msg = obj.get("message", {})
        parts: list[str] = []
        kind = "text"
        for block in msg.get("content", []):
            bt = block.get("type", "")
            if bt == "text":
                parts.append(block.get("text", ""))
            elif bt == "tool_use":
                kind = "tool_use"
                name = block.get("name", "?")
                inp = block.get("input", {})
                # Short summary of what the tool is doing
                if name == "Read":
                    path = inp.get("file_path", "")
                    parts.append(f"\n\n> *Reading `{Path(path).name}`...*\n\n")
                elif name == "Glob":
                    parts.append(
                        f"\n\n> *Searching for `{inp.get('pattern', '?')}`...*\n\n"
                    )
                elif name == "Grep":
                    parts.append(
                        f'\n\n> *Searching for "{inp.get("pattern", "?")}"...*\n\n'
                    )
                elif name == "Bash":
                    cmd = inp.get("command", "")
                    short = cmd[:80] + ("..." if len(cmd) > 80 else "")
                    parts.append(f"\n\n> *Running `{short}`...*\n\n")
                else:
                    parts.append(f"\n\n> *Using {name}...*\n\n")

        # Extract usage from assistant message
        usage_data = None
        raw_usage = msg.get("usage")
        if raw_usage:
            usage_data = {
                "input_tokens": raw_usage.get("input_tokens", 0),
                "output_tokens": raw_usage.get("output_tokens", 0),
                "cache_read": raw_usage.get("cache_read_input_tokens", 0),
                "cache_creation": raw_usage.get("cache_creation_input_tokens", 0),
            }
        return (kind, "".join(parts), usage_data)

    if evt_type == "result":
        # Extract final usage from result event
        usage_data: dict[str, Any] | None = None
        model_usage = obj.get("modelUsage", {})
        if model_usage:
            for _model_name, mu in model_usage.items():
                usage_data = {
                    "input_tokens": mu.get("inputTokens", 0),
                    "output_tokens": mu.get("outputTokens", 0),
                    "cache_read": mu.get("cacheReadInputTokens", 0),
                    "cache_creation": mu.get("cacheCreationInputTokens", 0),
                    "context_window": mu.get("contextWindow"),
                    "cost_usd": mu.get("costUSD"),
                }
                break
        elif obj.get("total_cost_usd") is not None:
            usage_data = {"cost_usd": obj["total_cost_usd"]}
        return ("result", obj.get("result", ""), usage_data)

    return ("ignore", "", None)


def _create_sandbox(output_dir: Path) -> Path:
    """Copy the study output directory into a sibling sandbox for safe execution."""
    sandbox = output_dir.parent / (output_dir.name + _SANDBOX_SUFFIX)
    if sandbox.exists():
        shutil.rmtree(sandbox)
    shutil.copytree(output_dir, sandbox)
    logger.info(f"Created sandbox copy: {sandbox}")
    return sandbox


def _cleanup_sandbox(sandbox: Path) -> None:
    """Remove a sandbox directory if it exists."""
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
        logger.info(f"Cleaned up sandbox: {sandbox}")


def _create_paper_workspace(output_dir: Path) -> tuple[Path, list[str]]:
    """Create a paper workspace with copies of study artifacts."""
    paper_dir = output_dir / "paper"
    paper_dir.mkdir(exist_ok=True)
    copied: list[str] = []
    for item in ("scripts", "data", "plots", "PROTOCOL.md", "RESULTS.md", "REPORT.md"):
        src = output_dir / item
        dst = paper_dir / item
        if src.is_dir() and not dst.exists():
            shutil.copytree(src, dst)
            copied.append(item)
        elif src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append(item)
    logger.info(f"Created paper workspace: {paper_dir} (copied: {copied})")
    return paper_dir, copied


def _cleanup_paper_workspace(paper_dir: Path, copied_items: list[str]) -> None:
    """Remove copied study files from the paper workspace."""
    for item in copied_items:
        path = paper_dir / item
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
    logger.info(f"Cleaned up paper workspace copies: {paper_dir}")


async def _stream_monitor(info: DispatchInfo, server: DispatchHost) -> None:
    """Parse stream-json events from the agent and update the card."""
    proc = info.process
    if proc is None or proc.stdout is None:
        return

    loop = asyncio.get_event_loop()
    accumulated = ""
    final_result: str | None = None
    last_update = 0.0
    config = _TASK_CONFIG.get(info.task, ("", "", ""))
    _, card_title, _ = config

    # Look up backend config for parser and context windows
    backend_cfg = _BACKENDS.get(info.backend, _BACKENDS["claude"])
    parse_event = backend_cfg.parse_event

    # Usage tracking
    usage: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "context_window": backend_cfg.context_windows.get(info.model, 200_000),
        "cost_usd": None,
    }

    try:

        def _read_line() -> bytes:
            return proc.stdout.readline()

        while True:
            line_bytes = await asyncio.wait_for(
                loop.run_in_executor(None, _read_line),
                timeout=_DISPATCH_TIMEOUT,
            )

            if not line_bytes:
                break

            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue

            kind, text, event_usage = parse_event(line)

            if kind == "result":
                final_result = text
                if event_usage:
                    if event_usage.get("cost_usd") is not None:
                        usage["cost_usd"] = event_usage["cost_usd"]
                    if event_usage.get("context_window"):
                        usage["context_window"] = event_usage["context_window"]
                    usage["input_tokens"] = (
                        event_usage.get("input_tokens", 0)
                        + event_usage.get("cache_read", 0)
                        + event_usage.get("cache_creation", 0)
                    )
                    usage["output_tokens"] = event_usage.get("output_tokens", 0)
                continue

            if event_usage and kind != "ignore":
                usage["input_tokens"] = (
                    event_usage.get("input_tokens", 0)
                    + event_usage.get("cache_read", 0)
                    + event_usage.get("cache_creation", 0)
                )
                usage["output_tokens"] += event_usage.get("output_tokens", 0)

            if kind == "ignore" or not text:
                continue

            accumulated += text
            info.accumulated_output = accumulated
            info.last_activity_at = datetime.now(timezone.utc).isoformat()

            now = loop.time()
            if now - last_update >= _UPDATE_INTERVAL:
                await _update_agent_card(
                    info.card_id,
                    info.study,
                    server,
                    {
                        "output": accumulated,
                        "last_activity_at": info.last_activity_at,
                        "usage": usage,
                    },
                    title=card_title,
                )
                last_update = now

        returncode = await loop.run_in_executor(None, proc.wait)
        completed_at = datetime.now(timezone.utc).isoformat()
        info.completed_at = completed_at

        duration: float | None = None
        if info.started_at:
            start_dt = datetime.fromisoformat(info.started_at)
            end_dt = datetime.fromisoformat(completed_at)
            duration = (end_dt - start_dt).total_seconds()

        if returncode == 0:
            info.status = "completed"
            display = final_result or accumulated
            if not display.strip():
                display = "*Agent completed with no output.*"
            await _update_agent_card(
                info.card_id, info.study, server,
                {"status": "completed", "output": display, "completed_at": completed_at, "duration": duration, "usage": usage},
                title=card_title,
            )
            await server.broadcast({"type": "agent.completed", "study": info.study, "task": info.task, "card_id": info.card_id})
        else:
            info.status = "failed"
            info.error = f"Process exited with code {returncode}"
            error_output = accumulated + f"\n\n---\n**Error:** {info.error}"
            await _update_agent_card(
                info.card_id, info.study, server,
                {"status": "failed", "output": error_output, "completed_at": completed_at, "duration": duration, "error": info.error, "usage": usage},
                title=card_title,
            )
            await server.broadcast({"type": "agent.failed", "study": info.study, "task": info.task, "card_id": info.card_id, "error": info.error})

    except asyncio.TimeoutError:
        try:
            proc.terminate()
            await asyncio.sleep(2)
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        completed_at = datetime.now(timezone.utc).isoformat()
        info.status = "failed"
        info.error = f"Timed out after {_DISPATCH_TIMEOUT}s"
        info.completed_at = completed_at

        duration = None
        if info.started_at:
            start_dt = datetime.fromisoformat(info.started_at)
            end_dt = datetime.fromisoformat(completed_at)
            duration = (end_dt - start_dt).total_seconds()

        timeout_output = accumulated + f"\n\n---\n**Timed out** after {_DISPATCH_TIMEOUT}s"
        await _update_agent_card(info.card_id, info.study, server, {"status": "failed", "output": timeout_output, "completed_at": completed_at, "duration": duration, "error": info.error, "usage": usage}, title=card_title)
        await server.broadcast({"type": "agent.failed", "study": info.study, "task": info.task, "card_id": info.card_id, "error": info.error})

    except Exception as e:
        info.status = "failed"
        info.error = str(e)
        logger.exception(f"Monitor error for dispatch '{info.task}'")
        completed_at = datetime.now(timezone.utc).isoformat()
        info.completed_at = completed_at
        duration: float | None = None
        if info.started_at:
            start_dt = datetime.fromisoformat(info.started_at)
            end_dt = datetime.fromisoformat(completed_at)
            duration = (end_dt - start_dt).total_seconds()
        try:
            await _update_agent_card(info.card_id, info.study, server, {"status": "failed", "output": accumulated + f"\n\n---\n**Error:** {info.error}", "completed_at": completed_at, "duration": duration, "error": info.error, "usage": usage}, title=card_title)
        except Exception:
            logger.debug("Failed to update card after monitor error")

    finally:
        paper_ws = info.extra.get("paper_workspace")
        paper_copies = info.extra.get("paper_copies")
        if paper_ws and paper_copies:
            _cleanup_paper_workspace(Path(paper_ws), paper_copies)
        sandbox = info.extra.get("sandbox")
        if sandbox:
            _cleanup_sandbox(Path(sandbox))
        for tmp in info.extra.get("prompt_tmp_files", []):
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def cancel_agent(card_id: str, server: DispatchHost) -> bool:
    """Cancel a running agent by card_id."""
    info = server.dispatches.get(card_id)
    if info is None or info.status != "running":
        return False

    proc = info.process
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass

    if info.monitor_task is not None:
        info.monitor_task.cancel()

    completed_at = datetime.now(timezone.utc).isoformat()
    info.status = "cancelled"
    info.completed_at = completed_at

    duration: float | None = None
    if info.started_at:
        start_dt = datetime.fromisoformat(info.started_at)
        end_dt = datetime.fromisoformat(completed_at)
        duration = (end_dt - start_dt).total_seconds()

    paper_ws = info.extra.get("paper_workspace")
    paper_copies = info.extra.get("paper_copies")
    if paper_ws and paper_copies:
        _cleanup_paper_workspace(Path(paper_ws), paper_copies)
    sandbox = info.extra.get("sandbox")
    if sandbox:
        _cleanup_sandbox(Path(sandbox))
    for tmp in info.extra.get("prompt_tmp_files", []):
        try:
            os.unlink(tmp)
        except OSError:
            pass

    config = _TASK_CONFIG.get(info.task, ("", "", ""))
    _, card_title, _ = config
    preserved = info.accumulated_output or ""
    if preserved.strip():
        cancel_output = preserved + "\n\n---\n*Cancelled by user.*"
    else:
        cancel_output = "*Cancelled by user.*"
    await _update_agent_card(card_id, info.study, server, {"status": "failed", "output": cancel_output, "completed_at": completed_at, "duration": duration, "error": "Cancelled by user"}, title=card_title)
    await server.broadcast({"type": "agent.failed", "study": info.study, "task": info.task, "card_id": card_id, "error": "Cancelled by user"})
    return True


def get_agent_status(card_id: str, server: DispatchHost) -> dict[str, Any] | None:
    """Get the status of an agent by card_id."""
    info = server.dispatches.get(card_id)
    if info is None:
        return None
    return {
        "status": info.status,
        "card_id": info.card_id,
        "study": info.study,
        "task": info.task,
        "backend": info.backend,
        "model": info.model,
        "pid": info.pid,
        "error": info.error,
        "started_at": info.started_at,
        "completed_at": info.completed_at,
        "last_activity_at": info.last_activity_at,
    }


def reconcile_orphaned_agents(server: DispatchHost) -> int:
    """Fix agent cards stuck in 'running' state with no backing process."""
    from vitrine._types import CardType

    if not server.study_manager:
        return 0

    fixed = 0
    for card in server.study_manager.list_all_cards():
        if card.card_type != CardType.AGENT:
            continue
        status = card.preview.get("status", "pending") if card.preview else "pending"
        if status != "running":
            continue
        if card.card_id in server.dispatches:
            continue
        new_preview = dict(card.preview)
        new_preview["status"] = "failed"
        new_preview["error"] = "Server restarted while agent was running"
        _, store = server.study_manager.get_or_create_study(card.study)
        if store:
            store.update_card(card.card_id, preview=new_preview)
        fixed += 1
        logger.info(f"Reconciled orphaned agent card: {card.card_id}")
    return fixed


def cleanup_dispatches(server: DispatchHost) -> None:
    """Terminate all running dispatches. Called on server shutdown."""
    for card_id, info in server.dispatches.items():
        if info.status == "running" and info.process is not None:
            try:
                info.process.terminate()
            except OSError:
                pass
            info.status = "cancelled"
        paper_ws = info.extra.get("paper_workspace")
        paper_copies = info.extra.get("paper_copies")
        if paper_ws and paper_copies:
            _cleanup_paper_workspace(Path(paper_ws), paper_copies)
        sandbox = info.extra.get("sandbox")
        if sandbox:
            _cleanup_sandbox(Path(sandbox))
        for tmp in info.extra.get("prompt_tmp_files", []):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    server.dispatches.clear()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    from vitrine._utils import is_pid_alive

    return is_pid_alive(pid)


_WATCHDOG_INTERVAL = 30  # seconds


async def _dispatch_watchdog(server: DispatchHost) -> None:
    """Periodic safety net: detect dead PIDs that the stream monitor missed."""
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL)
        for info in list(server.dispatches.values()):
            if info.status != "running" or info.pid is None:
                continue
            if _is_pid_alive(info.pid):
                continue
            if info.monitor_task and not info.monitor_task.done():
                continue
            info.status = "failed"
            info.error = "Process exited unexpectedly"
            completed_at = datetime.now(timezone.utc).isoformat()
            info.completed_at = completed_at
            duration = None
            if info.started_at:
                start_dt = datetime.fromisoformat(info.started_at)
                end_dt = datetime.fromisoformat(completed_at)
                duration = (end_dt - start_dt).total_seconds()
            config = _TASK_CONFIG.get(info.task, ("", "", ""))
            _, card_title, _ = config
            output = info.accumulated_output + "\n\n---\n**Error:** Process exited unexpectedly"
            await _update_agent_card(info.card_id, info.study, server, {"status": "failed", "output": output, "completed_at": completed_at, "duration": duration, "error": info.error}, title=card_title)
            await server.broadcast({"type": "agent.failed", "study": info.study, "task": info.task, "card_id": info.card_id, "error": info.error})
            logger.warning(f"Watchdog: agent {info.card_id} PID {info.pid} dead, marked failed")

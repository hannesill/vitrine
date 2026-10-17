"""Prompts that hand a study task to the researcher's own coding agent.

The board's study actions (Reproduce, Report, Paper, Presentation) copy one
of these prompts to the clipboard for the researcher to paste into Claude
Code, Codex or any other agent. Each prompt is the task's skill from
``skills/`` headed by the study's label and output directory, so it works
whether or not the agent has the skill installed.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"

# task name -> skill directory under SKILLS_DIR
TASK_SKILLS: dict[str, str] = {
    "reproduce": "reproduce-study",
    "report": "export-report",
    "paper": "draft-paper",
    "presentation": "create-presentation",
}


def _strip_frontmatter(text: str) -> str:
    """Drop a skill's YAML frontmatter; it is metadata for skill loaders."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + len("\n---\n") :].lstrip("\n")
    return text


def build_task_prompt(task: str, study: str, output_dir: Path | None) -> str:
    """Build the prompt for running ``task`` on ``study`` in a coding agent.

    Args:
        task: One of ``TASK_SKILLS``.
        study: Study label.
        output_dir: The study's output directory, or None if none is
            registered yet (the skills then register one).

    Raises:
        ValueError: If ``task`` is unknown.
    """
    skill = TASK_SKILLS.get(task)
    if skill is None:
        expected = ", ".join(TASK_SKILLS)
        raise ValueError(f"Unknown task: {task!r} (expected one of: {expected})")
    body = _strip_frontmatter((SKILLS_DIR / skill / "SKILL.md").read_text())

    header = [
        f"Follow the procedure below for the Vitrine study `{study}`.",
        "",
        f"**Study:** `{study}`",
    ]
    if output_dir is not None:
        header.append(f"**Output directory:** `{output_dir}`")
    else:
        header.append("**Output directory:** none yet")
    return "\n".join(header) + "\n\n---\n\n" + body

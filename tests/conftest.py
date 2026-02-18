"""Shared test fixtures for the vitrine test suite."""

from pathlib import Path

import pytest

from vitrine.dispatch import configure


@pytest.fixture(autouse=True)
def _configure_dispatch(tmp_path):
    """Set up dispatch with test skill directories and task config.

    Creates minimal SKILL.md files for the three dispatch tasks so that
    build_prompt() and _build_agent_preview() work without the real
    skills directory.
    """
    import vitrine.dispatch as _mod

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Create minimal skill files for each task
    for skill_name, content in [
        (
            "reproduce-study",
            "# Reproducibility Audit\n\nAudit the study for reproducibility.\n"
            "Check that scripts/ contain runnable code and outputs match.\n"
            "Verify data pipeline integrity and document any discrepancies.\n"
            "This skill ensures the study can be independently reproduced.\n"
            "Run all numbered scripts in order and compare outputs.\n"
            "Report findings as vitrine cards.\n"
            "## Steps\n1. Read PROTOCOL.md\n2. Run scripts\n3. Compare outputs\n",
        ),
        (
            "export-report",
            "# Study Report\n\nGenerate a study report from the vitrine cards.\n"
            "Summarize findings, methods, and conclusions.\n"
            "Include all relevant tables and figures.\n"
            "Format as a structured markdown document.\n"
            "This skill creates a comprehensive report of the study.\n"
            "## Steps\n1. Read study context\n2. Summarize\n3. Export\n",
        ),
        (
            "draft-paper",
            "# Paper Draft\n\nDraft an academic paper from the study.\n"
            "Follow standard paper structure: abstract, intro, methods, results.\n"
            "Include statistical results and figures.\n"
            "Use the study protocol and results as source material.\n"
            "This skill drafts a publication-ready manuscript.\n"
            "## Steps\n1. Read protocol\n2. Draft sections\n3. Compile\n",
        ),
    ]:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(content)

    task_config = {
        "reproduce": (
            "reproduce-study",
            "Reproducibility Audit",
            "Bash,Read,Glob,Grep",
        ),
        "report": ("export-report", "Study Report", "Read,Glob,Grep"),
        "paper": (
            "draft-paper",
            "Paper Draft",
            "Bash,Read,Glob,Grep,Write",
        ),
    }

    configure(skills_dir=skills_dir, task_config=task_config)

    yield

    # Reset to unconfigured state after each test
    _mod._SKILLS_DIR = None
    _mod._TASK_CONFIG = {}
